"""离线标准观测质量复核（只读）。

同一测点同一时刻的多站一致读数被归档合为一条标准观测。逐条观测都通过
发布检查，并不保证它们能在同一段接线上共同解释一组节点电位；本模块对
调用方指定的历史版本与采样窗口做一次只读复核：

* 每个有观测的 tick 只要求存在一套**共同**节点电位，使该 tick 每条标准
  观测的 ``V(a)-V(b)`` 落在归档值加减其对称半宽的闭区间内；
* 网络只提供测点与有序端点 ``a/b``，不强制使用标称电位、电阻值，也不
  要求没有实测的支路参与；
* 初始窗口全部自洽即判定原样可用；否则逐一假设隔离窗口内出现过的某座
  记录站（移除该站在所有行上的全部来源），只有让每个 tick 都自洽的站
  才是合格候选。候选可以不止一个，且“候选”只是可能的解释，不是已经
  证实的故障。

任何数值只用实测标准观测与调用方给出的半宽，精确有理数判断，等于区间
边界算满足；不导入电路求解器，不推进版本，不写归档。完整约定见
``docs/review.md``。
"""

import json
import os
import re
from fractions import Fraction

from . import checks
from .measurements import TICK_MAX, TICK_MIN
from .model import parse as parse_network

# 裁决状态。前两种可交付数据，后两种不交付任何行或补测区间。
STATUS_AS_PUBLISHED = 'as_published'
STATUS_ISOLATED = 'isolated_single_station'
STATUS_NO_CANDIDATE = 'no_qualified_station'
STATUS_AMBIGUOUS = 'ambiguous_station'

# 补测区间某一端没有有限界时的显式标识（与有理数字符串区分）。
UNBOUNDED = 'unbounded'

_RATIONAL = re.compile(r'-?(?:0|[1-9][0-9]*)(?:/[1-9][0-9]*)?')


class ReviewInputError(ValueError):
    """复核参数或归档读取不合法；不会返回任何裁决或数据。"""


# --------------------------------------------------------------------------
# 输入校验
# --------------------------------------------------------------------------

def _error(message):
    raise ReviewInputError(message)


def _ident(value, what):
    try:
        return checks.ident(value)
    except ValueError:
        _error('invalid %s' % what)


def _integer(value, low, high, what):
    if type(value) is not int or not low <= value <= high:
        _error('%s must be an integer in %d..%d' % (what, low, high))
    return value


def _rational_string(value, what):
    if not isinstance(value, str) or _RATIONAL.fullmatch(value) is None:
        _error('%s must be an exact rational string' % what)
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError):
        _error('%s must be an exact rational string' % what)


def _window(raw):
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        _error('window must be a [start_tick, end_tick] pair')
    start = _integer(raw[0], TICK_MIN, TICK_MAX, 'window start_tick')
    end = _integer(raw[1], TICK_MIN, TICK_MAX + 1, 'window end_tick')
    if start >= end:
        _error('window must be non-empty')
    return start, end


def _half_widths(raw):
    """半宽表：电阻 ID -> 有界十进制字符串，不得为负；零表示精确。"""
    if not isinstance(raw, dict):
        _error('half-widths must be an object keyed by resistor id')
    result = {}
    for key, value in raw.items():
        resistor_id = _ident(key, 'half-width resistor id')
        try:
            parsed = checks.number(value)
        except ValueError:
            _error('half-width for %r must be a bounded decimal string'
                   % resistor_id)
        if parsed < 0:
            _error('half-width for %r must not be negative' % resistor_id)
        result[resistor_id] = parsed
    return result


# --------------------------------------------------------------------------
# 从归档读取窗口内的标准观测
# --------------------------------------------------------------------------

def _run_dir(store_dir, run_id):
    if not isinstance(store_dir, (str, os.PathLike)):
        _error('store_dir must be a filesystem path')
    store_dir = os.fspath(store_dir)
    run_dir = os.path.join(store_dir, run_id)
    if not os.path.isdir(run_dir):
        _error('unknown run %r' % run_id)
    return store_dir, run_dir


def _archive_endpoints(run_dir, run_id):
    """读取该运行首版固定的网络，只取电阻 ID 与有序端点 a/b。"""
    path = os.path.join(run_dir, 'network.json')
    try:
        with open(path, 'rb') as handle:
            network = json.loads(handle.read())
    except (OSError, ValueError) as exc:
        raise ReviewInputError('run %r has no readable archived network: %s'
                               % (run_id, exc)) from exc
    try:
        _nodes, edges = parse_network(network)
    except ValueError as exc:
        raise ReviewInputError('run %r has an invalid archived network: %s'
                               % (run_id, exc)) from exc
    return {edge['id']: (edge['a'], edge['b']) for edge in edges}


def _load_snapshot(store_dir, run_id, version):
    """只读快照；只取该历史版的标准观测，不回落也不读原始/冲突行。"""
    from .measurements import MeasurementError, read_version
    try:
        return read_version(store_dir, run_id, version)
    except MeasurementError as exc:
        raise ReviewInputError(str(exc)) from exc


def _window_rows(snapshot, run_id, version, start, end, endpoints):
    """把窗口内的标准观测转成内部行，按归档值原字符串保留。"""
    rows = []
    seen = set()
    for observation in snapshot['observations']:
        if not isinstance(observation, dict):
            _error('malformed observation in run %r version %d'
                   % (run_id, version))
        try:
            tick = observation['tick']
            resistor_id = observation['resistor_id']
            volts = observation['volts']
            sources = observation['sources']
        except KeyError as exc:
            raise ReviewInputError(
                'malformed observation in run %r version %d'
                % (run_id, version)) from exc
        tick = _integer(tick, TICK_MIN, TICK_MAX, 'observation tick')
        if not start <= tick < end:
            continue
        resistor_id = _ident(resistor_id, 'observation resistor_id')
        if resistor_id not in endpoints:
            _error('observation resistor %r is not in the archived network'
                   % resistor_id)
        value = _rational_string(volts, 'observation volts')
        if not isinstance(sources, list) or not sources:
            _error('observation sources must be a non-empty array')
        source_keys = []
        for source in sources:
            if not isinstance(source, (list, tuple)) or len(source) != 2:
                _error('observation source must be a (station, row_id) pair')
            source_keys.append((_ident(source[0], 'source station'),
                                _ident(source[1], 'source row_id')))
        key = (tick, resistor_id)
        if key in seen:
            _error('duplicate observation key %r in version' % (key,))
        seen.add(key)
        rows.append({
            'tick': tick,
            'resistor_id': resistor_id,
            'volts': volts,
            'value': value,
            'sources': source_keys,
            'stations': frozenset(source[0] for source in source_keys),
        })
    rows.sort(key=lambda item: (item['tick'], item['resistor_id']))
    return rows


# --------------------------------------------------------------------------
# 差约束：V(a)-V(b) 落在 [lo, hi]
#
# 可行性用负环检测：V(a)-V(b) <= c 对应有向边 b -> a（权 c），
# V(a)-V(b) >= lo 对应 a -> b（权 -lo）。全为等式时用带权并查集。
# --------------------------------------------------------------------------

def _make_find(parent, weight):
    def find(index):
        root = index
        total = Fraction(0)
        while parent[root] != root:
            total += weight[root]
            root = parent[root]
        current = index
        consumed = Fraction(0)
        while parent[current] != current:
            nxt = parent[current]
            step = weight[current]
            parent[current] = root
            weight[current] = total - consumed
            consumed += step
            current = nxt
        return root, total
    return find


def _tight_feasible(edges, count):
    parent = list(range(count))
    weight = [Fraction(0)] * count
    find = _make_find(parent, weight)
    for edge in edges:
        root_a, pot_a = find(edge['ai'])
        root_b, pot_b = find(edge['bi'])
        value = edge['lo']
        if root_a == root_b:
            if pot_a - pot_b != value:
                return False
        else:
            parent[root_a] = root_b
            weight[root_a] = value - pot_a + pot_b
    return True


def _directed_edges(edges):
    result = []
    for edge in edges:
        # V(a)-V(b) <= hi  =>  b -> a (hi)
        result.append((edge['bi'], edge['ai'], edge['hi']))
        # V(a)-V(b) >= lo  =>  V(b)-V(a) <= -lo  =>  a -> b (-lo)
        result.append((edge['ai'], edge['bi'], -edge['lo']))
    return result


def _general_feasible(edges):
    count = _node_span(edges)
    directed = _directed_edges(edges)
    dist = [Fraction(0)] * count
    for _ in range(count):
        changed = False
        for tail, head, cost in directed:
            candidate = dist[tail] + cost
            if candidate < dist[head]:
                dist[head] = candidate
                changed = True
        if not changed:
            return True
    return False


def _component_feasible(edges):
    if all(edge['lo'] == edge['hi'] for edge in edges):
        return _tight_feasible(edges, _node_span(edges))
    return _general_feasible(edges)


def _node_span(edges):
    # 节点索引取自全局编号；并查集数组按最大索引 + 1 分配即可复用。
    span = 0
    for edge in edges:
        if edge['ai'] + 1 > span:
            span = edge['ai'] + 1
        if edge['bi'] + 1 > span:
            span = edge['bi'] + 1
    return span


def _components(edges, count):
    parent = list(range(count))
    weight = [Fraction(0)] * count  # 仅为复用 find；不用于等式判断
    find = _make_find(parent, weight)
    for edge in edges:
        root_a, _ = find(edge['ai'])
        root_b, _ = find(edge['bi'])
        if root_a != root_b:
            parent[root_a] = root_b
    groups = {}
    for edge in edges:
        root, _ = find(edge['ai'])
        groups.setdefault(root, []).append(edge)
    return list(groups.values())


def _all_feasible(edges, count):
    return all(_component_feasible(group)
               for group in _components(edges, count))


# --------------------------------------------------------------------------
# 候选站与补测区间
# --------------------------------------------------------------------------

def _sole_source_groups(edges):
    groups = {}
    for edge in edges:
        if len(edge['stations']) == 1:
            station = next(iter(edge['stations']))
            groups.setdefault(station, set()).add(edge['seq'])
    return groups


def _fix_set(edges, count):
    """在单个连通分量内，隔离哪些站可使剩余约束重新可行。

    隔离一座站后，它独自支持的行整行撤下、它与其他站共同支持的行去掉该站
    来源但保留原值原半宽。二者都只**减少或不变**约束集合，因此原本自洽的
    tick 不会因为隔离而变得不自洽；合格候选只需修好每个不自洽的分量。
    """
    groups = _sole_source_groups(edges)
    result = set()
    for station, sole in groups.items():
        remaining = [edge for edge in edges if edge['seq'] not in sole]
        if _all_feasible(remaining, count):
            result.add(station)
    return result


def _tight_potentials(edges, count):
    parent = list(range(count))
    weight = [Fraction(0)] * count
    find = _make_find(parent, weight)
    for edge in edges:
        root_a, pot_a = find(edge['ai'])
        root_b, pot_b = find(edge['bi'])
        if root_a == root_b:
            continue  # 该 tick 已判可行，等式必一致
        parent[root_a] = root_b
        weight[root_a] = edge['lo'] - pot_a + pot_b
    return find


def _shortest(directed, source, count):
    dist = [None] * count
    dist[source] = Fraction(0)
    for _ in range(count - 1):
        changed = False
        for tail, head, cost in directed:
            from_tail = dist[tail]
            if from_tail is None:
                continue
            candidate = from_tail + cost
            if dist[head] is None or candidate < dist[head]:
                dist[head] = candidate
                changed = True
        if not changed:
            break
    return dist


def _range_for_tick(remaining, queries, count):
    """在某个 tick 的剩余观测下，给出每个被撤键的精确可达区间。"""
    groups = _components(remaining, count)
    root_of = {}
    for root_index, group in enumerate(groups):
        for edge in group:
            root_of[edge['ai']] = root_index
            root_of[edge['bi']] = root_index
    grouped_queries = {}
    results = {}
    for query in queries:
        ai, bi = query
        root_a = root_of.get(ai)
        root_b = root_of.get(bi)
        if root_a is None or root_a != root_b:
            results[query] = (UNBOUNDED, UNBOUNDED)
        else:
            grouped_queries.setdefault(root_a, []).append(query)
    for root_index, items in grouped_queries.items():
        edges = groups[root_index]
        tight = all(edge['lo'] == edge['hi'] for edge in edges)
        if tight:
            find = _tight_potentials(edges, count)
            for ai, bi in items:
                root_a, pot_a = find(ai)
                root_b, pot_b = find(bi)
                value = pot_a - pot_b  # V(a)-V(b)
                results[(ai, bi)] = (str(value), str(value))
        else:
            directed = _directed_edges(edges)
            cache = {}
            for ai, bi in items:
                if bi not in cache:
                    cache[bi] = _shortest(directed, bi, count)
                if ai not in cache:
                    cache[ai] = _shortest(directed, ai, count)
                upper = cache[bi][ai]  # min V(a)-V(b)
                lower_source = cache[ai][bi]
                low = (UNBOUNDED if lower_source is None
                       else str(-lower_source))
                high = UNBOUNDED if upper is None else str(upper)
                results[(ai, bi)] = (low, high)
    return results


# --------------------------------------------------------------------------
# 公共接口
# --------------------------------------------------------------------------

def _output_sources(sources):
    return [[station, row_id] for station, row_id in sources]


def _deliverables(rows, candidate, by_tick, count):
    supported = []
    removed_by_tick = {}
    for row in rows:
        if row['stations'] == frozenset((candidate,)):
            removed_by_tick.setdefault(row['tick'], []).append(row)
            continue
        if candidate in row['stations']:
            sources = [source for source in row['sources']
                       if source[0] != candidate]
        else:
            sources = list(row['sources'])
        supported.append({
            'tick': row['tick'],
            'resistor_id': row['resistor_id'],
            'volts': row['volts'],
            'sources': _output_sources(sources),
        })

    retests = []
    for tick in sorted(removed_by_tick):
        removed = removed_by_tick[tick]
        removed_seq = {row['seq'] for row in removed}
        remaining = [row for row in by_tick[tick]
                     if row['seq'] not in removed_seq]
        queries = [(row['ai'], row['bi']) for row in removed]
        bounds = _range_for_tick(remaining, queries, count)
        for row in removed:
            low, high = bounds[(row['ai'], row['bi'])]
            isolated = [source for source in row['sources']
                        if source[0] == candidate]
            retests.append({
                'tick': row['tick'],
                'resistor_id': row['resistor_id'],
                'isolated_sources': _output_sources(isolated),
                'inferred': True,
                'min_volts': low,
                'max_volts': high,
            })
    retests.sort(key=lambda item: (item['tick'], item['resistor_id']))
    return supported, retests


def review(store_dir, run_id, version, window, half_widths):
    """只读复核一个运行的历史版本在窗口内的标准观测。

    参数：归档目录 ``store_dir``、运行 ``run_id``、已发布正整数 ``version``、
    非空窗口 ``window``（``[start_tick, end_tick)``）以及按电阻 ID 给出的
    对称绝对半宽 ``half_widths``（有界十进制字符串，``"0"`` 表示精确）。
    半宽表必须恰好覆盖窗口内标准观测出现的电阻 ID。

    成功返回::

        {
          'run_id': ..., 'version': ..., 'window': [start, end],
          'status': 'as_published' | 'isolated_single_station'
                    | 'no_qualified_station' | 'ambiguous_station',
          'candidates': [...],           # 排序后的候选站 ID
          # 仅前两种状态：
          'rows':    [{'tick','resistor_id','volts','sources'}, ...],
          # 仅唯一候选状态：
          'retests': [{'tick','resistor_id','isolated_sources',
                       'inferred': True,'min_volts','max_volts'}, ...],
        }

    ``rows`` 与 ``retests`` 分别按数值 tick、电阻 ID 升序；来源保留归档顺序。
    后两种状态不返回任何行或补测区间。参数非法、半宽表不恰好覆盖、未知运行
    或历史版抛 ``ReviewInputError``，不返回任何结果。复核只读归档。
    """
    run_id = _ident(run_id, 'run_id')
    if type(version) is not int or version < 1:
        _error('version must be a published positive integer')
    start, end = _window(window)
    widths = _half_widths(half_widths)
    store_dir, run_dir = _run_dir(store_dir, run_id)
    endpoints = _archive_endpoints(run_dir, run_id)
    snapshot = _load_snapshot(store_dir, run_id, version)
    rows = _window_rows(snapshot, run_id, version, start, end, endpoints)

    expected = {row['resistor_id'] for row in rows}
    supplied = set(widths)
    if supplied != expected:
        missing = expected - supplied
        extra = supplied - expected
        details = []
        if missing:
            details.append('%d missing (e.g. %r)'
                           % (len(missing), min(missing)))
        if extra:
            details.append('%d extra (e.g. %r)' % (len(extra), min(extra)))
        _error('half-widths must exactly cover the window observations: %s'
               % '; '.join(details))

    base = {
        'run_id': run_id,
        'version': version,
        'window': [start, end],
    }
    if not rows:
        return dict(base, status=STATUS_AS_PUBLISHED, candidates=[], rows=[])

    node_index = {}
    for node_a, node_b in endpoints.values():
        node_index.setdefault(node_a, len(node_index))
        node_index.setdefault(node_b, len(node_index))
    count = len(node_index)
    by_tick = {}
    for seq, row in enumerate(rows):
        height = widths[row['resistor_id']]
        node_a, node_b = endpoints[row['resistor_id']]
        row.update({
            'seq': seq,
            'ai': node_index[node_a],
            'bi': node_index[node_b],
            'lo': row['value'] - height,
            'hi': row['value'] + height,
        })
        by_tick.setdefault(row['tick'], []).append(row)

    # 初始窗口：逐 tick 逐连通分量判可行，记下不自洽的分量。
    infeasible = []
    for tick in sorted(by_tick):
        for group in _components(by_tick[tick], count):
            if not _component_feasible(group):
                infeasible.append((tick, group))

    if not infeasible:
        published = [{
            'tick': row['tick'],
            'resistor_id': row['resistor_id'],
            'volts': row['volts'],
            'sources': _output_sources(row['sources']),
        } for row in rows]
        return dict(base, status=STATUS_AS_PUBLISHED, candidates=[],
                    rows=published)

    candidates = None
    for tick, group in infeasible:
        fixed = _fix_set(group, count)
        candidates = fixed if candidates is None else candidates & fixed
        if not candidates:
            break
    candidates = sorted(candidates or ())

    if not candidates:
        return dict(base, status=STATUS_NO_CANDIDATE, candidates=[])
    if len(candidates) > 1:
        return dict(base, status=STATUS_AMBIGUOUS, candidates=candidates)

    supported, retests = _deliverables(
        rows, candidates[0], by_tick, count)
    return dict(base, status=STATUS_ISOLATED, candidates=candidates,
                rows=supported, retests=retests)
