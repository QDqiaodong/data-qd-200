"""离线数据集构建：从实测归档的指定版本构造拟合集与独立复验集。

本模块只读取实测归档已经发布的标准观测（``measurements.read_version``），
不导入电路求解器，也不向归档写入任何内容。调用方用清单显式指定每个运行
的一个已发布版本，并补齐物理样件、参考条件与逐来源的仪表校准批次；构建
结果把候选行划分为两个互不泄漏、合计覆盖全部候选行的数据集：

* ``fit``：用于估计仪器修正参数；
* ``holdout``：独立复验集，按不可拆组整组选取，两种条件行数精确命中。

同一物理样件或同一校准批次的候选行（经其他行传递、跨运行）必须同侧；
组标识取组内最小行键。多个精确解之间取排序后复验组标识序列字典序最小者。
完整约定见 ``docs/datasets.md``。
"""

import os

from . import checks
from .measurements import MeasurementError, read_version

# 业务上限（模块级常量，测试可 monkeypatch 缩小后验证边界）。
MAX_RUNS = 100
MAX_ROWS = 30_000
MAX_SOURCE_KEYS = 90_000
MAX_GROUPS = 3_000
HOLDOUT_MIN = 1
HOLDOUT_MAX = 1_500
TICK_MIN = 0
TICK_MAX = 9_999_999

_CONDITIONS = ('L', 'H')
_MANIFEST_FIELDS = ('versions', 'specimens', 'calibration_batches',
                    'holdout_counts')
_VERSION_FIELDS = ('run_id', 'version')
_SPECIMEN_FIELDS = ('run_id', 'tick', 'resistor_id', 'specimen_id',
                    'condition')
_BATCH_FIELDS = ('run_id', 'station', 'row_id', 'batch_id')

# 选组后缀动态规划的块大小：每块按块尾快照重建局部后缀表。
_BLOCK = 64


class DatasetError(ValueError):
    """数据集构建失败的基类。"""


class DatasetInputError(DatasetError):
    """清单或归档输入不合法；不会返回任何一侧数据。"""


class HoldoutInfeasible(DatasetError):
    """输入合法，但不存在精确命中两种条件槽位的完整组选择。"""


# --------------------------------------------------------------------------
# 清单结构校验
# --------------------------------------------------------------------------

def _error(message):
    raise DatasetInputError(message)


def _object(value, fields, what):
    if not isinstance(value, dict) or set(value) != set(fields):
        _error('%s must be an object with exactly fields: %s'
               % (what, ', '.join(fields)))
    return value


def _array(value, what):
    if not isinstance(value, list):
        _error('%s must be an array' % what)
    return value


def _ident(value, what):
    try:
        return checks.ident(value)
    except ValueError:
        _error('invalid %s' % what)


def _tick(value, what):
    try:
        return checks.integer(value, TICK_MIN, TICK_MAX)
    except ValueError:
        _error('invalid %s tick' % what)


def _condition(value, what):
    if not isinstance(value, str) or value not in _CONDITIONS:
        _error('%s condition must be "L" or "H"' % what)
    return value


def _positive_int(value, what):
    # bool 是 int 的子类，但契约要求布尔值不视为整数。
    if type(value) is not int or value < 1:
        _error('%s must be a positive integer' % what)
    return value


def _parse_versions(raw):
    items = _array(raw, 'versions')
    if len(items) > MAX_RUNS:
        _error('at most %d runs may be selected' % MAX_RUNS)
    selected = {}
    for item in items:
        if isinstance(item, (list, tuple)):
            if len(item) != 2:
                _error('version entries must be (run_id, version) pairs')
            run_id, version = item
        else:
            _object(item, _VERSION_FIELDS, 'version entry')
            run_id, version = item['run_id'], item['version']
        run_id = _ident(run_id, 'run_id')
        version = _positive_int(version, 'version')
        if run_id in selected:
            _error('run %r selected more than once' % run_id)
        selected[run_id] = version
    return selected


def _parse_specimens(raw):
    result = {}
    for item in _array(raw, 'specimens'):
        _object(item, _SPECIMEN_FIELDS, 'specimen entry')
        run_id = _ident(item['run_id'], 'specimen run_id')
        tick = _tick(item['tick'], 'specimen')
        resistor_id = _ident(item['resistor_id'], 'specimen resistor_id')
        specimen_id = _ident(item['specimen_id'], 'specimen_id')
        condition = _condition(item['condition'], 'specimen')
        key = (run_id, tick, resistor_id)
        if key in result:
            _error('duplicate specimen row key %r' % (key,))
        result[key] = (specimen_id, condition)
    return result


def _parse_batches(raw):
    result = {}
    for item in _array(raw, 'calibration_batches'):
        _object(item, _BATCH_FIELDS, 'calibration batch entry')
        run_id = _ident(item['run_id'], 'batch run_id')
        station = _ident(item['station'], 'batch station')
        row_id = _ident(item['row_id'], 'batch row_id')
        batch_id = _ident(item['batch_id'], 'batch_id')
        key = (run_id, station, row_id)
        if key in result:
            _error('duplicate calibration batch source key %r' % (key,))
        result[key] = batch_id
    return result


def _parse_counts(raw):
    _object(raw, _CONDITIONS, 'holdout_counts')
    result = {}
    for condition in _CONDITIONS:
        value = raw[condition]
        if type(value) is not int or not HOLDOUT_MIN <= value <= HOLDOUT_MAX:
            _error('holdout_counts[%r] must be an integer in %d..%d'
                   % (condition, HOLDOUT_MIN, HOLDOUT_MAX))
        result[condition] = value
    return result


# --------------------------------------------------------------------------
# 从归档读取候选行（仅标准观测）
# --------------------------------------------------------------------------

def _archive_row(run_id, version, observation):
    """把一条归档标准观测转成内部候选行，逐字段沿用归档校验口径。"""
    if not isinstance(observation, dict):
        _error('malformed observation in run %r version %d'
               % (run_id, version))
    try:
        tick = observation['tick']
        resistor_id = observation['resistor_id']
        volts = observation['volts']
        sources = observation['sources']
    except KeyError as exc:
        _error('malformed observation in run %r version %d'
               % (run_id, version))
    tick = _tick(tick, 'observation')
    resistor_id = _ident(resistor_id, 'observation resistor_id')
    if not isinstance(volts, str):
        _error('observation volts must be the archived exact string')
    if not isinstance(sources, list) or not sources:
        _error('observation sources must be a non-empty array')
    source_keys = []
    for source in sources:
        if (not isinstance(source, (list, tuple)) or len(source) != 2):
            _error('observation source must be a (station, row_id) pair')
        station = _ident(source[0], 'source station')
        row_id = _ident(source[1], 'source row_id')
        source_keys.append((run_id, station, row_id))
    return {
        'key': (run_id, tick, resistor_id),
        'run_id': run_id,
        'version': version,
        'volts': volts,
        'source_keys': source_keys,
    }


def _load_rows(store_dir, selected):
    rows = []
    seen_keys = set()
    referenced = set()
    for run_id in sorted(selected):
        version = selected[run_id]
        try:
            snapshot = read_version(store_dir, run_id, version)
        except MeasurementError as exc:
            raise DatasetInputError('%s: %s' % (run_id, exc)) from exc
        for observation in snapshot['observations']:
            row = _archive_row(run_id, version, observation)
            if row['key'] in seen_keys:
                _error('duplicate candidate row key %r' % (row['key'],))
            seen_keys.add(row['key'])
            rows.append(row)
            referenced.update(row['source_keys'])
            if len(rows) > MAX_ROWS:
                _error('at most %d candidate rows are allowed' % MAX_ROWS)
            if len(referenced) > MAX_SOURCE_KEYS:
                _error('at most %d referenced source keys are allowed'
                       % MAX_SOURCE_KEYS)
    return rows, referenced


# --------------------------------------------------------------------------
# 恰好覆盖的补充资料
# --------------------------------------------------------------------------

def _require_cover(name, supplied, expected):
    supplied_keys = set(supplied)
    if supplied_keys != expected:
        missing = expected - supplied_keys
        extra = supplied_keys - expected
        details = []
        if missing:
            details.append('%d missing (e.g. %r)'
                           % (len(missing), min(missing)))
        if extra:
            details.append('%d extra (e.g. %r)'
                           % (len(extra), min(extra)))
        _error('%s must exactly cover the candidates: %s'
               % (name, '; '.join(details)))


# --------------------------------------------------------------------------
# 不可拆组
# --------------------------------------------------------------------------

class _DisjointSet:
    __slots__ = ('parent', 'size')

    def __init__(self, count):
        self.parent = list(range(count))
        self.size = [1] * count

    def find(self, index):
        parent = self.parent
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(self, left, right):
        left = self.find(left)
        right = self.find(right)
        if left == right:
            return
        if self.size[left] < self.size[right]:
            left, right = right, left
        self.parent[right] = left
        self.size[left] += self.size[right]


def _group_rows(rows, specimens, batches):
    """按样件与校准批次求传递闭包，返回按组标识升序的组列表。"""
    nodes = _DisjointSet(len(rows))
    first_by_specimen = {}
    first_by_batch = {}
    for index, row in enumerate(rows):
        specimen_id = specimens[row['key']][0]
        anchor = first_by_specimen.setdefault(specimen_id, index)
        if anchor != index:
            nodes.union(anchor, index)
        for source_key in row['source_keys']:
            batch_id = batches[source_key]
            anchor = first_by_batch.setdefault(batch_id, index)
            if anchor != index:
                nodes.union(anchor, index)

    groups = {}
    for index, row in enumerate(rows):
        root = nodes.find(index)
        info = groups.get(root)
        if info is None:
            groups[root] = {'id': row['key'], 'indices': [index]}
        else:
            info['indices'].append(index)
            if row['key'] < info['id']:
                info['id'] = row['key']
    if len(groups) > MAX_GROUPS:
        _error('at most %d indivisible groups are allowed' % MAX_GROUPS)
    return sorted(groups.values(), key=lambda info: info['id'])


# --------------------------------------------------------------------------
# 精确配额与字典序最小的复验组选择
# --------------------------------------------------------------------------

def _add_group(table, volts_l, volts_h, target_l, mask_h):
    """把 ``(volts_l, volts_h)`` 组加入后缀表：table[l] 为可达 h 的位集。"""
    updated = table[:]
    top = target_l - volts_l
    if top < 0:
        return updated
    if volts_h:
        for index in range(top, -1, -1):
            value = table[index]
            if value:
                updated[index + volts_l] |= (value << volts_h) & mask_h
    else:
        for index in range(top, -1, -1):
            value = table[index]
            if value:
                updated[index + volts_l] |= value
    return updated


def _select_groups(counts, target_l, target_h):
    """返回每个组的入选布尔序列；无精确解时抛 ``HoldoutInfeasible``。

    组按标识升序处理：只要包含当前组仍存在完成两种配额的方案就入选，
    由此得到排序后组标识序列字典序最小的解。
    """
    total = len(counts)
    mask_h = (1 << (target_h + 1)) - 1
    base = [0] * (target_l + 1)
    base[0] = 1

    boundaries = {total: base}
    current = base
    for index in range(total - 1, -1, -1):
        volts_l, volts_h = counts[index]
        current = _add_group(current, volts_l, volts_h, target_l, mask_h)
        if index % _BLOCK == 0:
            boundaries[index] = current
    if not ((boundaries[0][target_l] >> target_h) & 1):
        raise HoldoutInfeasible(
            'no selection of complete groups hits L=%d and H=%d exactly'
            % (target_l, target_h))

    decisions = [False] * total
    remaining_l, remaining_h = target_l, target_h
    start = 0
    while start < total:
        end = min(start + _BLOCK, total)
        tables = [None] * (end + 1)
        tables[end] = boundaries[end]
        for index in range(end - 1, start - 1, -1):
            volts_l, volts_h = counts[index]
            tables[index] = _add_group(tables[index + 1], volts_l, volts_h,
                                       target_l, mask_h)
        for index in range(start, end):
            volts_l, volts_h = counts[index]
            take = False
            if volts_l <= remaining_l and volts_h <= remaining_h:
                table = tables[index + 1]
                if (table[remaining_l - volts_l]
                        >> (remaining_h - volts_h)) & 1:
                    take = True
            decisions[index] = take
            if take:
                remaining_l -= volts_l
                remaining_h -= volts_h
        start = end
    if remaining_l or remaining_h:
        # 后缀表已保证存在解，这里只是防御性检查。
        raise HoldoutInfeasible('internal quota mismatch')
    return decisions


# --------------------------------------------------------------------------
# 公共接口
# --------------------------------------------------------------------------

def _output_row(row, specimen_id, condition, batches, group_id):
    return {
        'run_id': row['run_id'],
        'tick': row['key'][1],
        'resistor_id': row['key'][2],
        'version': row['version'],
        'volts': row['volts'],
        'condition': condition,
        'specimen_id': specimen_id,
        'sources': [
            {'run_id': source[0], 'station': source[1],
             'row_id': source[2], 'batch_id': batches[source]}
            for source in row['source_keys']
        ],
        'group_id': list(group_id),
    }


def _count_conditions(rows):
    counts = {condition: 0 for condition in _CONDITIONS}
    for row in rows:
        counts[row['condition']] += 1
    return counts


def build_datasets(store_dir, manifest):
    """从实测归档与调用方清单构造 ``(fit, holdout)`` 两份完整数据集。

    ``store_dir`` 为 ``measurements.publish`` 使用的归档目录；``manifest``
    见 ``docs/datasets.md``。成功返回::

        {
          'versions': [{'run_id': ..., 'version': ...}, ...],   # 按 run_id 排序
          'fit':     {'counts': {'L': ..., 'H': ...}, 'rows': [...]},
          'holdout': {'counts': {'L': ..., 'H': ...}, 'rows': [...]},
        }

    ``fit.rows`` 与 ``holdout.rows`` 各自按 ``(run_id, tick, resistor_id)``
    升序，二者并集恰为全部候选行且不拆组。输入不合法抛
    ``DatasetInputError``；输入合法但无法精确铺满槽位抛
    ``HoldoutInfeasible``，两种情况都不返回部分数据。构建只读归档，
    不推进版本、不改动已提交业务数据。
    """
    _object(manifest, _MANIFEST_FIELDS, 'manifest')
    selected = _parse_versions(manifest['versions'])
    specimens = _parse_specimens(manifest['specimens'])
    batches = _parse_batches(manifest['calibration_batches'])
    targets = _parse_counts(manifest['holdout_counts'])

    store_dir = os.fspath(store_dir)
    rows, referenced = _load_rows(store_dir, selected)
    candidate_keys = {row['key'] for row in rows}

    _require_cover('specimens', specimens, candidate_keys)
    _require_cover('calibration_batches', batches, referenced)

    groups = _group_rows(rows, specimens, batches)
    group_ids = [None] * len(rows)
    for info in groups:
        for index in info['indices']:
            group_ids[index] = info['id']

    counts = []
    for info in groups:
        tally = {condition: 0 for condition in _CONDITIONS}
        for index in info['indices']:
            tally[specimens[rows[index]['key']][1]] += 1
        counts.append((tally['L'], tally['H']))

    decisions = _select_groups(counts, targets['L'], targets['H'])
    selected_ids = {info['id'] for info, take in zip(groups, decisions) if take}

    fit_rows = []
    holdout_rows = []
    for index in sorted(range(len(rows)), key=lambda i: rows[i]['key']):
        row = rows[index]
        specimen_id, condition = specimens[row['key']]
        output = _output_row(row, specimen_id, condition, batches,
                             group_ids[index])
        if group_ids[index] in selected_ids:
            holdout_rows.append(output)
        else:
            fit_rows.append(output)

    # 精确配额再次核对；任何偏差都说明选择逻辑失效，绝不交付部分结果。
    holdout_counts = _count_conditions(holdout_rows)
    if (holdout_counts['L'] != targets['L']
            or holdout_counts['H'] != targets['H']):
        raise HoldoutInfeasible('internal quota mismatch')

    versions = [{'run_id': run_id, 'version': selected[run_id]}
                for run_id in sorted(selected)]
    return {
        'versions': versions,
        'fit': {'counts': _count_conditions(fit_rows), 'rows': fit_rows},
        'holdout': {'counts': holdout_counts, 'rows': holdout_rows},
    }
