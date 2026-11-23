"""离线分卷交接与按案复核。

把 ``measurements`` 归档中一个已发布的正整数版本封装成一份封存目录与
若干可独立复制的数据卷，移交给没有原归档的离线复核组；复核方凭独立
交接的目录 SHA-256 摘要、收到的目录与本地卷目录按案复核，发卷方凭
完好的封存副本按补卷单发卷。本模块纯标准库、不连接外部服务、不操作
设备，不改动归档与既有求解、发布、数据集、质量复核接口的任何行为。

封存布局（``seal`` 的输出目录）::

    <output_dir>/
        catalog.json            # 封存目录：键、归属、路由与完整性信息
        volumes/<sha256>.json   # 数据卷：裁决正文卷与证据正文卷，按内容寻址

目录只存键（测点时刻、原始身份）、归属（组与未绑定）、路由（正文在哪
一卷第几条）与完整性信息（每卷 SHA-256）；测量值、换算值、登记正文与
裁决正文只出现在数据卷里，每条原始证据正文全卷唯一。卷以内容摘要命名：
同一版本与同一每卷上限重复封装得到内容标识完全一致的目录与卷，与输出
位置、时间无关。裁决卷与证据卷分离，可以分别交接；不需要复制原归档的
版本树、相邻变更、其他运行或其他版本。

复核以实验室**另行交接**的目录摘要为信任根：目录无法读出、摘要不符或
请求字段非法时整个请求报错；卷是否可用一律以封存目录登记的摘要为准，
缺失、不可读或内容不符都算不可用，卷自报的运行或版本不能代替内容校验。
"""

import hashlib
import json
import os
import re

from . import checks
from .measurements import (
    INTERVAL_END_MAX,
    TICK_MAX,
    TICK_MIN,
    MeasurementError,
    read_version,
)

# 每卷正文条数上限的合法范围（封装参数）。
MIN_BODIES_PER_VOLUME = 1
MAX_BODIES_PER_VOLUME = 256

# 复核动作。
ACTION_OBSERVATION = 'observation'   # 按测点时刻
ACTION_IDENTITY = 'identity'         # 按原始身份

# 复核状态。
STATUS_COMPLETE = 'complete'                 # 材料齐备，交付核对结果
STATUS_AWAITING = 'awaiting_volumes'         # 案存在但材料不齐，待补卷
STATUS_NO_BOUND = 'no_bound_evidence'        # 该版此键没有绑定证据
STATUS_NO_READING = 'no_such_reading'        # 该版没有该原始身份
STATUS_INCONSISTENT = 'inconsistent'         # 完整性通过但正文与目录矛盾

_KIND_ACCEPTED = 'accepted'
_KIND_CONFLICT = 'conflict'
_STATUS_UNBOUND = 'unbound'

_CATALOG_FORMAT = 'resistor-flow-handover-catalog/1'
_VOLUME_FORMAT = 'resistor-flow-handover-volume/1'
_VOLUME_SUFFIX = '.json'

_HEX64 = re.compile(r'[0-9a-f]{64}')
_RATIONAL = re.compile(r'-?(?:0|[1-9][0-9]*)(?:/[1-9][0-9]*)?')

_READING_FIELDS = ('station', 'row_id', 'channel', 'tick', 'raw_volts')
_REGISTRATION_FIELDS = ('station', 'channel', 'start_tick', 'end_tick',
                       'resistor_id', 'positive_node', 'gain', 'offset_volts')
_EVIDENCE_FIELDS = ('identity', 'reading', 'status', 'registration',
                    'converted_volts', 'observation_key')


class HandoverError(ValueError):
    """封装、复核请求或补卷单被拒绝；抛出时不改动任何已交接资料。"""


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------

def _error(message):
    raise HandoverError(message)


def _dump(obj):
    """与归档一致的规范 JSON：键排序、无浮点、无环境相关字节。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      allow_nan=False).encode('utf-8')


def _read_file(path):
    with open(path, 'rb') as handle:
        return handle.read()


def _write_file(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temporary = path + '.tmp-%d' % os.getpid()
    try:
        with open(temporary, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _fields(obj, required, what):
    if not isinstance(obj, dict) or set(obj) != set(required):
        _error('%s must be an object with exactly fields: %s'
               % (what, ', '.join(required)))
    return obj


def _ident(value, what):
    try:
        return checks.ident(value)
    except ValueError:
        _error('invalid %s' % what)


def _positive_int(value, what):
    if type(value) is not int or value < 1:
        _error('%s must be a positive integer' % what)
    return value


def _tick(value, what='tick'):
    try:
        return checks.integer(value, TICK_MIN, TICK_MAX)
    except ValueError:
        _error('%s must be an integer in %d..%d'
               % (what, TICK_MIN, TICK_MAX))


def _limit(value):
    if (type(value) is not int
            or not MIN_BODIES_PER_VOLUME <= value <= MAX_BODIES_PER_VOLUME):
        _error('max bodies per volume must be an integer in %d..%d'
               % (MIN_BODIES_PER_VOLUME, MAX_BODIES_PER_VOLUME))
    return value


def _volume_id(value, what='volume id'):
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        _error('%s must be a sha-256 hex string' % what)
    return value


def _digest(value):
    return _volume_id(value, 'catalog digest')


def _rational(value):
    return isinstance(value, str) and _RATIONAL.fullmatch(value) is not None


# --------------------------------------------------------------------------
# 封装
# --------------------------------------------------------------------------

def _snapshot_parts(snapshot):
    """从版本快照取出稳定排序的组与逐身份证据，并核对内部一致。"""
    try:
        groups = []
        for row in snapshot['observations']:
            groups.append({
                'tick': row['tick'],
                'resistor_id': row['resistor_id'],
                'kind': _KIND_ACCEPTED,
                'volts': row['volts'],
                'sources': sorted([list(pair) for pair in row['sources']]),
            })
        for row in snapshot['conflicts']:
            groups.append({
                'tick': row['tick'],
                'resistor_id': row['resistor_id'],
                'kind': _KIND_CONFLICT,
                'sources': sorted([list(pair) for pair in row['sources']]),
            })
        groups.sort(key=lambda group: (group['tick'], group['resistor_id']))
        evidence = [dict(entry) for entry in snapshot['evidence']]
        evidence.sort(key=lambda entry: (entry['identity'][0],
                                         entry['identity'][1]))
    except (KeyError, TypeError, IndexError) as exc:
        raise HandoverError('malformed archive snapshot: %s' % exc) from exc
    _check_snapshot(groups, evidence)
    return groups, evidence


def _check_snapshot(groups, evidence):
    by_key = {}
    for group in groups:
        key = (group['tick'], group['resistor_id'])
        if key in by_key:
            _error('archive snapshot has duplicate observation keys')
        by_key[key] = group
    members = {key: set() for key in by_key}
    for entry in evidence:
        if not isinstance(entry, dict) or set(entry) != set(_EVIDENCE_FIELDS):
            _error('malformed evidence entry in archive snapshot')
        identity = entry['identity']
        if not isinstance(identity, list) or len(identity) != 2:
            _error('malformed evidence identity in archive snapshot')
        station, row_id = identity
        key = entry['observation_key']
        status = entry['status']
        if status == _STATUS_UNBOUND:
            if key is not None:
                _error('unbound evidence carries an observation key')
            continue
        if key is None:
            _error('bound evidence lacks an observation key')
        group = by_key.get((key[0], key[1]))
        if group is None:
            _error('evidence references a missing group')
        if group['kind'] != status:
            _error('evidence status disagrees with the group verdict')
        if [station, row_id] not in group['sources']:
            _error('evidence identity is not a source of its group')
        members[(key[0], key[1])].add((station, row_id))
    for key, group in by_key.items():
        if {tuple(pair) for pair in group['sources']} != members[key]:
            _error('group sources and bound evidence disagree')


def _verdict_body(group):
    body = {
        'kind': group['kind'],
        'tick': group['tick'],
        'resistor_id': group['resistor_id'],
        'sources': group['sources'],
    }
    if group['kind'] == _KIND_ACCEPTED:
        body['volts'] = group['volts']
    return body


def _emit_volumes(volumes, volumes_dir, kind, run_id, version, bodies, limit):
    """把正文序列按上限分块写入数据卷，返回每块的内容标识列表。"""
    vids = []
    for start in range(0, len(bodies), limit):
        chunk = bodies[start:start + limit]
        data = _dump({
            'format': _VOLUME_FORMAT,
            'kind': kind,
            'run_id': run_id,
            'version': version,
            'bodies': chunk,
        })
        vid = hashlib.sha256(data).hexdigest()
        if vid not in volumes:
            volumes[vid] = {'kind': kind, 'sha256': vid, 'bodies': len(chunk)}
            _write_file(os.path.join(volumes_dir, vid + _VOLUME_SUFFIX), data)
        vids.append(vid)
    return vids


def seal(store_dir, run_id, version, output_dir, max_bodies_per_volume):
    """把指定运行的一个已发布正整数版本封装成封存目录与数据卷。

    ``max_bodies_per_volume`` 为每卷最多容纳的裁决或证据正文条数
    （1..256 的整数）。``output_dir`` 必须未存在；成功后其中含
    ``catalog.json`` 与 ``volumes/``。返回目录摘要、各路径与全部卷的
    内容标识。同一版本与同一上限重复封装得到内容标识完全一致的目录与
    卷，与输出位置、时间无关。未知运行或版号、非法上限抛
    ``HandoverError``；本接口只读归档，不推进版本。
    """
    run_id = _ident(run_id, 'run_id')
    version = _positive_int(version, 'version')
    limit = _limit(max_bodies_per_volume)
    if not isinstance(store_dir, (str, os.PathLike)):
        _error('store_dir must be a filesystem path')
    if not isinstance(output_dir, (str, os.PathLike)):
        _error('output_dir must be a filesystem path')
    store_dir = os.fspath(store_dir)
    output_dir = os.fspath(output_dir)
    try:
        snapshot = read_version(store_dir, run_id, version)
    except MeasurementError as exc:
        raise HandoverError(str(exc)) from exc
    if os.path.exists(output_dir):
        _error('output dir already exists: %s' % output_dir)

    groups, evidence = _snapshot_parts(snapshot)
    volumes_dir = os.path.join(output_dir, 'volumes')
    os.makedirs(volumes_dir)
    volumes = {}
    verdict_vids = _emit_volumes(
        volumes, volumes_dir, 'verdict', run_id, version,
        [_verdict_body(group) for group in groups], limit)
    evidence_vids = _emit_volumes(
        volumes, volumes_dir, 'evidence', run_id, version, evidence, limit)

    catalog = {
        'format': _CATALOG_FORMAT,
        'run_id': run_id,
        'version': version,
        'max_bodies_per_volume': limit,
        'volumes': volumes,
        'groups': [{
            'tick': group['tick'],
            'resistor_id': group['resistor_id'],
            'kind': group['kind'],
            'sources': group['sources'],
            'verdict': {'volume': verdict_vids[index // limit],
                        'index': index % limit},
        } for index, group in enumerate(groups)],
        'evidence': [{
            'station': entry['identity'][0],
            'row_id': entry['identity'][1],
            'group': (list(entry['observation_key'])
                      if entry['observation_key'] is not None else None),
            'volume': evidence_vids[index // limit],
            'index': index % limit,
        } for index, entry in enumerate(evidence)],
    }
    data = _dump(catalog)
    digest = hashlib.sha256(data).hexdigest()
    catalog_path = os.path.join(output_dir, 'catalog.json')
    _write_file(catalog_path, data)
    return {
        'run_id': run_id,
        'version': version,
        'catalog_digest': digest,
        'catalog_path': catalog_path,
        'volumes_dir': volumes_dir,
        'volumes': sorted(volumes),
    }


# --------------------------------------------------------------------------
# 目录读取与校验
# --------------------------------------------------------------------------

def _read_catalog_raw(catalog_path):
    if not isinstance(catalog_path, (str, os.PathLike)):
        _error('catalog path must be a filesystem path')
    try:
        return _read_file(os.fspath(catalog_path))
    except OSError as exc:
        _error('catalog cannot be read: %s' % exc)


def _parse_catalog(raw):
    try:
        obj = json.loads(raw)
    except ValueError:
        _error('catalog is not valid JSON')
    return _validate_catalog(obj)


def _catalog_volumes(raw):
    if not isinstance(raw, dict):
        _error('catalog volumes must be an object')
    volumes = {}
    for vid, entry in raw.items():
        _volume_id(vid, 'volume id')
        _fields(entry, ('kind', 'sha256', 'bodies'), 'volume entry')
        if entry['kind'] not in ('verdict', 'evidence'):
            _error('volume kind must be verdict or evidence')
        if entry['sha256'] != vid:
            _error('volume digest does not match its identifier')
        bodies = entry['bodies']
        if (type(bodies) is not int
                or not MIN_BODIES_PER_VOLUME <= bodies <= MAX_BODIES_PER_VOLUME):
            _error('volume body count out of range')
        volumes[vid] = {'kind': entry['kind'], 'sha256': entry['sha256'],
                        'bodies': bodies}
    return volumes


def _sources(raw):
    if not isinstance(raw, list) or not raw:
        _error('group sources must be a non-empty array')
    sources = []
    seen = set()
    for pair in raw:
        if not isinstance(pair, list) or len(pair) != 2:
            _error('group source must be a (station, row_id) pair')
        station = _ident(pair[0], 'source station')
        row_id = _ident(pair[1], 'source row_id')
        if (station, row_id) in seen:
            _error('duplicate group source')
        seen.add((station, row_id))
        sources.append([station, row_id])
    return sources


def _locator(raw, volumes, kind):
    _fields(raw, ('volume', 'index'), 'locator')
    vid = _volume_id(raw['volume'], 'locator volume')
    info = volumes.get(vid)
    if info is None:
        _error('locator references an unknown volume')
    if info['kind'] != kind:
        _error('locator references a %s volume as %s' % (info['kind'], kind))
    index = raw['index']
    if type(index) is not int or not 0 <= index < info['bodies']:
        _error('locator index out of range')
    return vid, index


def _catalog_groups(raw, volumes):
    if not isinstance(raw, list):
        _error('catalog groups must be an array')
    groups = {}
    for item in raw:
        _fields(item, ('tick', 'resistor_id', 'kind', 'sources', 'verdict'),
                'group entry')
        tick = _tick(item['tick'], 'group tick')
        resistor_id = _ident(item['resistor_id'], 'group resistor_id')
        kind = item['kind']
        if kind not in (_KIND_ACCEPTED, _KIND_CONFLICT):
            _error('group kind must be accepted or conflict')
        sources = _sources(item['sources'])
        vid, index = _locator(item['verdict'], volumes, 'verdict')
        key = (tick, resistor_id)
        if key in groups:
            _error('duplicate group key in catalog')
        groups[key] = {
            'tick': tick,
            'resistor_id': resistor_id,
            'kind': kind,
            'sources': sources,
            'verdict_volume': vid,
            'verdict_index': index,
        }
    return groups


def _catalog_evidence(raw, volumes):
    if not isinstance(raw, list):
        _error('catalog evidence must be an array')
    entries = {}
    for item in raw:
        _fields(item, ('station', 'row_id', 'group', 'volume', 'index'),
                'evidence entry')
        station = _ident(item['station'], 'evidence station')
        row_id = _ident(item['row_id'], 'evidence row_id')
        group = item['group']
        if group is not None:
            if not isinstance(group, list) or len(group) != 2:
                _error('evidence group must be a (tick, resistor_id) pair')
            group = [_tick(group[0], 'evidence group tick'),
                     _ident(group[1], 'evidence group resistor_id')]
        vid, index = _locator({'volume': item['volume'],
                               'index': item['index']}, volumes, 'evidence')
        key = (station, row_id)
        if key in entries:
            _error('duplicate evidence identity in catalog')
        entries[key] = {
            'station': station,
            'row_id': row_id,
            'group': group,
            'volume': vid,
            'index': index,
        }
    return entries


def _cross_check_catalog(groups, evidence):
    members = {key: set() for key in groups}
    for (station, row_id), entry in evidence.items():
        group = entry['group']
        if group is None:
            continue
        key = (group[0], group[1])
        if key not in groups:
            _error('evidence references a missing group')
        members[key].add((station, row_id))
    for key, group in groups.items():
        if {tuple(pair) for pair in group['sources']} != members[key]:
            _error('group sources and bound evidence disagree in catalog')


def _validate_catalog(obj):
    _fields(obj, ('format', 'run_id', 'version', 'max_bodies_per_volume',
                  'volumes', 'groups', 'evidence'), 'catalog')
    if obj['format'] != _CATALOG_FORMAT:
        _error('unsupported catalog format')
    run_id = _ident(obj['run_id'], 'catalog run_id')
    version = _positive_int(obj['version'], 'catalog version')
    limit = _limit(obj['max_bodies_per_volume'])
    volumes = _catalog_volumes(obj['volumes'])
    groups = _catalog_groups(obj['groups'], volumes)
    evidence = _catalog_evidence(obj['evidence'], volumes)
    _cross_check_catalog(groups, evidence)
    return {
        'run_id': run_id,
        'version': version,
        'limit': limit,
        'volumes': volumes,
        'groups': groups,
        'evidence': evidence,
    }


# --------------------------------------------------------------------------
# 复核请求与案卷范围
# --------------------------------------------------------------------------

def _validate_request(request):
    if not isinstance(request, dict):
        _error('request must be an object')
    action = request.get('action')
    if action == ACTION_OBSERVATION:
        _fields(request, ('run_id', 'version', 'action', 'tick',
                          'resistor_id'), 'request')
    elif action == ACTION_IDENTITY:
        _fields(request, ('run_id', 'version', 'action', 'station',
                          'row_id'), 'request')
    else:
        _error('request action must be %r or %r'
               % (ACTION_OBSERVATION, ACTION_IDENTITY))
    parsed = {
        'run_id': _ident(request['run_id'], 'run_id'),
        'version': _positive_int(request['version'], 'version'),
        'action': action,
    }
    if action == ACTION_OBSERVATION:
        parsed['tick'] = _tick(request['tick'])
        parsed['resistor_id'] = _ident(request['resistor_id'], 'resistor_id')
    else:
        parsed['station'] = _ident(request['station'], 'station')
        parsed['row_id'] = _ident(request['row_id'], 'row_id')
    return parsed


def _require_case_owner(catalog, request):
    if (request['run_id'] != catalog['run_id']
            or request['version'] != catalog['version']):
        _error('unknown run or version for this catalog')


def _case_scope(catalog, request):
    """本案允许取用的卷集合与路由；案不存在时 found=False、集合为空。"""
    if request['action'] == ACTION_OBSERVATION:
        key = (request['tick'], request['resistor_id'])
        group = catalog['groups'].get(key)
        if group is None:
            return False, set(), None
        needed = {group['verdict_volume']}
        members = []
        for station, row_id in group['sources']:
            entry = catalog['evidence'][(station, row_id)]
            needed.add(entry['volume'])
            members.append(entry)
        return True, needed, {'group': group, 'members': members}
    key = (request['station'], request['row_id'])
    entry = catalog['evidence'].get(key)
    if entry is None:
        return False, set(), None
    needed = {entry['volume']}
    group = None
    if entry['group'] is not None:
        group = catalog['groups'][(entry['group'][0], entry['group'][1])]
        needed.add(group['verdict_volume'])
    return True, needed, {'entry': entry, 'group': group}


# --------------------------------------------------------------------------
# 卷检查与正文一致性
# --------------------------------------------------------------------------

def _check_volume_object(obj, info, catalog):
    """完整性已通过后的正文结构核对；返回矛盾描述或 None。"""
    if not isinstance(obj, dict):
        return 'volume is not an object'
    if set(obj) != {'format', 'kind', 'run_id', 'version', 'bodies'}:
        return 'volume fields mismatch'
    if obj['format'] != _VOLUME_FORMAT:
        return 'volume format mismatch'
    if obj['kind'] != info['kind']:
        return 'volume kind disagrees with the catalog'
    if obj['run_id'] != catalog['run_id'] or obj['version'] != catalog['version']:
        return 'volume identity disagrees with the catalog'
    if (not isinstance(obj['bodies'], list)
            or len(obj['bodies']) != info['bodies']):
        return 'volume body count disagrees with the catalog'
    return None


def _inspect_volumes(catalog, volumes_dir, needed):
    """只打开本案依赖的卷，返回 (不可用清单, 矛盾详情, 已解析卷)。

    缺失、不可读或内容与登记摘要不符都计入不可用清单；完整性通过但
    正文与目录矛盾时返回矛盾详情。二者都不命中时已解析卷覆盖全部
    所需卷。
    """
    unavailable = []
    parsed = {}
    for vid in sorted(needed):
        info = catalog['volumes'][vid]
        path = os.path.join(volumes_dir, vid + _VOLUME_SUFFIX)
        try:
            raw = _read_file(path)
        except OSError:
            unavailable.append(vid)     # 缺失或不可读
            continue
        if hashlib.sha256(raw).hexdigest() != info['sha256']:
            unavailable.append(vid)     # 内容与登记摘要不符
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            return None, 'volume %s is not valid JSON' % vid, None
        problem = _check_volume_object(obj, info, catalog)
        if problem is not None:
            return None, problem, None
        parsed[vid] = obj
    return unavailable, None, parsed


def _check_verdict_body(body, group):
    if not isinstance(body, dict):
        return 'verdict body is not an object'
    expected = {'kind', 'tick', 'resistor_id', 'sources'}
    if group['kind'] == _KIND_ACCEPTED:
        expected = expected | {'volts'}
    if set(body) != expected:
        return 'verdict body fields mismatch'
    if body['kind'] != group['kind']:
        return 'verdict kind disagrees with the catalog'
    if (body['tick'] != group['tick']
            or body['resistor_id'] != group['resistor_id']):
        return 'verdict key disagrees with the catalog routing'
    if body['sources'] != group['sources']:
        return 'verdict sources disagree with the catalog routing'
    if group['kind'] == _KIND_ACCEPTED and not _rational(body['volts']):
        return 'verdict volts is not an exact rational string'
    return None


def _check_reading_body(reading, entry):
    if not isinstance(reading, dict) or set(reading) != set(_READING_FIELDS):
        return 'evidence reading fields mismatch'
    if (reading['station'] != entry['station']
            or reading['row_id'] != entry['row_id']):
        return 'evidence reading identity disagrees with the catalog routing'
    try:
        checks.ident(reading['channel'])
        checks.integer(reading['tick'], TICK_MIN, TICK_MAX)
        checks.number(reading['raw_volts'])
    except ValueError:
        return 'evidence reading is malformed'
    return None


def _check_registration_body(registration, reading, group):
    if (not isinstance(registration, dict)
            or set(registration) != set(_REGISTRATION_FIELDS)):
        return 'evidence registration fields mismatch'
    try:
        checks.ident(registration['station'])
        checks.ident(registration['channel'])
        checks.ident(registration['positive_node'])
        start = checks.integer(registration['start_tick'], TICK_MIN, TICK_MAX)
        end = checks.integer(registration['end_tick'], 1, INTERVAL_END_MAX)
        gain = checks.number(registration['gain'])
        checks.number(registration['offset_volts'])
    except ValueError:
        return 'evidence registration is malformed'
    if (registration['station'] != reading['station']
            or registration['channel'] != reading['channel']):
        return 'evidence registration does not match its reading'
    if registration['resistor_id'] != group['resistor_id']:
        return 'evidence registration disagrees with the group'
    if not start <= reading['tick'] < end:
        return 'evidence registration does not cover its reading'
    if gain <= 0:
        return 'evidence registration gain is not positive'
    return None


def _check_evidence_body(body, entry, group):
    if not isinstance(body, dict):
        return 'evidence body is not an object'
    if set(body) != set(_EVIDENCE_FIELDS):
        return 'evidence body fields mismatch'
    if body['identity'] != [entry['station'], entry['row_id']]:
        return 'evidence identity disagrees with the catalog routing'
    problem = _check_reading_body(body['reading'], entry)
    if problem is not None:
        return problem
    if group is None:
        if body['status'] != _STATUS_UNBOUND:
            return 'evidence status disagrees with the catalog'
        if (body['observation_key'] is not None
                or body['registration'] is not None
                or body['converted_volts'] is not None):
            return 'unbound evidence carries binding data'
        return None
    if body['status'] != group['kind']:
        return 'evidence status disagrees with the group verdict'
    if body['observation_key'] != [group['tick'], group['resistor_id']]:
        return 'evidence attribution disagrees with the catalog'
    if body['reading']['tick'] != group['tick']:
        return 'evidence reading tick disagrees with its group'
    if not _rational(body['converted_volts']):
        return 'evidence converted volts is not an exact rational string'
    return _check_registration_body(body['registration'], body['reading'],
                                    group)


def _verdict_of(group, parsed):
    body = parsed[group['verdict_volume']]['bodies'][group['verdict_index']]
    return _check_verdict_body(body, group), body


def _evidence_of(entry, group, parsed):
    body = parsed[entry['volume']]['bodies'][entry['index']]
    return _check_evidence_body(body, entry, group), body


# --------------------------------------------------------------------------
# 按案复核
# --------------------------------------------------------------------------

def _case_echo(request):
    if request['action'] == ACTION_OBSERVATION:
        return {'key': {'tick': request['tick'],
                        'resistor_id': request['resistor_id']}}
    return {'identity': [request['station'], request['row_id']]}


def _complete_observation(base, echo, routing, parsed):
    group = routing['group']
    problem, verdict = _verdict_of(group, parsed)
    if problem is not None:
        return dict(base, status=STATUS_INCONSISTENT, detail=problem, **echo)
    evidence = []
    for entry in routing['members']:
        problem, body = _evidence_of(entry, group, parsed)
        if problem is not None:
            return dict(base, status=STATUS_INCONSISTENT, detail=problem,
                        **echo)
        evidence.append(body)
    return dict(base, status=STATUS_COMPLETE, verdict=verdict,
                evidence=evidence, **echo)


def _complete_identity(base, echo, routing, parsed):
    entry = routing['entry']
    group = routing['group']
    problem, body = _evidence_of(entry, group, parsed)
    if problem is not None:
        return dict(base, status=STATUS_INCONSISTENT, detail=problem, **echo)
    if group is None:
        return dict(base, status=STATUS_COMPLETE, group=None,
                    disposition=_STATUS_UNBOUND, evidence=body, **echo)
    problem, verdict = _verdict_of(group, parsed)
    if problem is not None:
        return dict(base, status=STATUS_INCONSISTENT, detail=problem, **echo)
    if [entry['station'], entry['row_id']] not in verdict['sources']:
        return dict(base, status=STATUS_INCONSISTENT,
                    detail='identity is not a source of its group verdict',
                    **echo)
    return dict(base, status=STATUS_COMPLETE,
                group=[group['tick'], group['resistor_id']],
                disposition=group['kind'], evidence=body, **echo)


def review_case(catalog_digest, catalog_path, volumes_dir, request):
    """以独立交接的目录摘要、收到的目录与本地卷目录复核一个案。

    ``request`` 为封闭字段集合::

        {'run_id', 'version', 'action': 'observation', 'tick', 'resistor_id'}
        {'run_id', 'version', 'action': 'identity', 'station', 'row_id'}

    目录无法读出、摘要不符、请求字段非法、运行或版号与目录不符都抛
    ``HandoverError``，不回落。返回结果一律标明目录摘要与版本；状态为
    ``complete`` / ``awaiting_volumes`` / ``no_bound_evidence`` /
    ``no_such_reading`` / ``inconsistent`` 之一。只读本案依赖的卷，
    不枚举或打开无关卷；确认不存在不需要读卷。
    """
    digest = _digest(catalog_digest)
    raw = _read_catalog_raw(catalog_path)
    if hashlib.sha256(raw).hexdigest() != digest:
        _error('catalog digest mismatch')
    catalog = _parse_catalog(raw)
    request = _validate_request(request)
    _require_case_owner(catalog, request)
    if not isinstance(volumes_dir, (str, os.PathLike)):
        _error('volumes dir must be a filesystem path')
    volumes_dir = os.fspath(volumes_dir)

    base = {
        'run_id': catalog['run_id'],
        'version': catalog['version'],
        'catalog_digest': digest,
        'action': request['action'],
    }
    echo = _case_echo(request)
    found, needed, routing = _case_scope(catalog, request)
    if not found:
        status = (STATUS_NO_BOUND if request['action'] == ACTION_OBSERVATION
                  else STATUS_NO_READING)
        return dict(base, status=status, **echo)
    unavailable, problem, parsed = _inspect_volumes(catalog, volumes_dir,
                                                    needed)
    if problem is not None:
        return dict(base, status=STATUS_INCONSISTENT, detail=problem, **echo)
    if unavailable:
        return dict(base, status=STATUS_AWAITING,
                    missing_volumes=unavailable, **echo)
    if request['action'] == ACTION_OBSERVATION:
        return _complete_observation(base, echo, routing, parsed)
    return _complete_identity(base, echo, routing, parsed)


# --------------------------------------------------------------------------
# 补卷交付
# --------------------------------------------------------------------------

def _order_volumes(raw):
    if not isinstance(raw, list):
        _error('order volumes must be an array')
    result = []
    for vid in raw:
        _volume_id(vid, 'order volume')
        result.append(vid)
    return result


def deliver_volumes(sealed_dir, order):
    """按补卷单核定范围并交付所请求的卷（只读封存副本，不重封存）。

    ``sealed_dir`` 为发卷方完好的封存副本（含 ``catalog.json`` 与
    ``volumes/``，不需要原归档）。``order`` 为封闭字段集合::

        {'catalog_digest': ..., 'request': <原复核请求>, 'volumes': [...]}

    发卷方独立根据固定目录核定该动作允许取用的卷范围，不信任补卷单
    自填的范围；目录摘要不符、夹带无关卷或任一所请求卷不可用都整单
    拒绝（抛 ``HandoverError``），不交付部分。合法时只交付所请求的
    卷，返回其内容标识、摘要与在封存副本中的路径供复制；不顺带交付
    整组、整版或前后版本，也不改动原归档。
    """
    if not isinstance(sealed_dir, (str, os.PathLike)):
        _error('sealed dir must be a filesystem path')
    sealed_dir = os.fspath(sealed_dir)
    _fields(order, ('catalog_digest', 'request', 'volumes'), 'delivery order')
    digest = _digest(order['catalog_digest'])
    raw = _read_catalog_raw(os.path.join(sealed_dir, 'catalog.json'))
    if hashlib.sha256(raw).hexdigest() != digest:
        _error('catalog digest mismatch')
    catalog = _parse_catalog(raw)
    request = _validate_request(order['request'])
    _require_case_owner(catalog, request)
    requested = _order_volumes(order['volumes'])
    _found, scope, _routing = _case_scope(catalog, request)
    outside = sorted(set(requested) - scope)
    if outside:
        _error('order includes volumes outside the case scope: %s'
               % outside[0])
    volumes_dir = os.path.join(sealed_dir, 'volumes')
    delivered = []
    for vid in sorted(set(requested)):
        info = catalog['volumes'][vid]
        path = os.path.join(volumes_dir, vid + _VOLUME_SUFFIX)
        try:
            data = _read_file(path)
        except OSError:
            _error('requested volume %s is unavailable' % vid)
        if hashlib.sha256(data).hexdigest() != info['sha256']:
            _error('requested volume %s fails integrity' % vid)
        delivered.append({'volume_id': vid, 'sha256': info['sha256'],
                          'path': path})
    return {
        'run_id': catalog['run_id'],
        'version': catalog['version'],
        'catalog_digest': digest,
        'volumes': delivered,
    }
