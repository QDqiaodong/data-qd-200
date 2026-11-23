"""离线实测数据发布链路。

本模块把仪表实测的电阻两端电压（原始读数）与可更正的接线登记结合，
发布带证据、可追溯、按版本固定的标准观测。任何数值只来自调用方交付的
实测读数和登记，既不读取也不引用电路求解结果（``nodal`` / ``service``
从不被本模块导入），求解结果不参与填值、筛值或冲突裁决。

持久化全部位于调用方指定的本地目录，无外部服务、无进程内状态：
重新打开目录即可读取全部历史版本。目录布局::

    <store_dir>/<run_id>/
        lock                       # fcntl 咨询锁
        current.json               # {"version": N}，原子替换
        network.json               # 首版登记的网络，后续版本不得替换
        raw/<station>/<row_id>.json  # 每条原始身份一份，只写一次
        requests/<request_id>.json # 成功请求的幂等记录（含解析后内容）
        versions/vN/               # 每个成功版本一个不可变目录
            meta.json              # 版号、请求 ID、本版完整登记
            observations.json      # 标准观测行 + 冲突键证据
            evidence.json          # 每个原始身份的逐版证据
            changes.json           # 相对直接前版的变更集

提交要么完整可见、要么完全不可见：版本目录先以暂存名构建再原子改名，
请求记录先于 ``current.json`` 切换落盘；发布进程在任何时刻被中断，
重新打开后只能看到完整旧版或完整新版，且新版可见时其重试身份与证据
必然已经可用。
"""

import fcntl
import json
import os
import shutil
import tempfile
from bisect import bisect_right
from contextlib import contextmanager
from fractions import Fraction

from . import checks
from .model import parse as parse_network

# 业务上限（模块级常量，测试可 monkeypatch 缩小后验证边界）。
MAX_READINGS = 100_000
MAX_REGISTRATIONS = 10_000
TICK_MIN = 0
TICK_MAX = 9_999_999
INTERVAL_END_MAX = 10_000_000

_READING_FIELDS = ('station', 'row_id', 'channel', 'tick', 'raw_volts')
_REG_FIELDS = ('station', 'channel', 'start_tick', 'end_tick',
               'resistor_id', 'positive_node', 'gain', 'offset_volts')
_BASE_FIELDS = ('run_id', 'request_id', 'base_version', 'readings', 'registrations')

_STATUS_UNBOUND = 'unbound'
_STATUS_ACCEPTED = 'accepted'
_STATUS_CONFLICT = 'conflict'


class MeasurementError(ValueError):
    """所有被拒绝的发布/读取请求；抛出前不得改动任何已提交数据。"""


# --------------------------------------------------------------------------
# 通用持久化小工具
# --------------------------------------------------------------------------

def _dump(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      allow_nan=False).encode('utf-8')


def _atomic_write(path, obj):
    """同目录临时文件 + fsync + 原子改名。"""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(_dump(obj))
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


@contextmanager
def _file_lock(path, exclusive):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, 'a+b')
    try:
        fcntl.flock(handle.fileno(),
                    fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class _Paths:
    """一个 run 的全部落盘位置；ID 已过 ident 规则，不含路径分隔符。"""

    def __init__(self, store_dir, run_id):
        self.run = os.path.join(store_dir, run_id)
        self.lock = os.path.join(self.run, 'lock')
        self.current = os.path.join(self.run, 'current.json')
        self.network = os.path.join(self.run, 'network.json')
        self.raw_dir = os.path.join(self.run, 'raw')
        self.request_dir = os.path.join(self.run, 'requests')
        self.version_dir_root = os.path.join(self.run, 'versions')

    def raw_file(self, station, row_id):
        return os.path.join(self.raw_dir, station, row_id + '.json')

    def request_file(self, request_id):
        return os.path.join(self.request_dir, request_id + '.json')

    def version_dir(self, version):
        return os.path.join(self.version_dir_root, 'v%d' % version)

    def staging_dir(self, version):
        return os.path.join(self.version_dir_root,
                            '.staging-v%d-%d' % (version, os.getpid()))


# --------------------------------------------------------------------------
# 结构校验与请求身份
# --------------------------------------------------------------------------

def _canonical(obj):
    """解析后结构的规范形式：字典键序无关，数组顺序与字符串原文保留。"""
    if isinstance(obj, dict):
        return {key: _canonical(obj[key]) for key in sorted(obj)}
    if isinstance(obj, list):
        return [_canonical(item) for item in obj]
    return obj


def _validate_reading(item):
    checks.fields(item, _READING_FIELDS)
    checks.ident(item['station'])
    checks.ident(item['row_id'])
    checks.ident(item['channel'])
    checks.integer(item['tick'], TICK_MIN, TICK_MAX)
    checks.number(item['raw_volts'])  # 有界十进制字符串，Fraction 仅用于校验


def _validate_registration(item, endpoints):
    checks.fields(item, _REG_FIELDS)
    station = checks.ident(item['station'])
    channel = checks.ident(item['channel'])
    start = checks.integer(item['start_tick'], TICK_MIN, TICK_MAX)
    end = checks.integer(item['end_tick'], 1, INTERVAL_END_MAX)
    if start >= end:
        raise MeasurementError('registration interval must be non-empty')
    resistor_id = checks.ident(item['resistor_id'])
    positive_node = checks.ident(item['positive_node'])
    if resistor_id not in endpoints:
        raise MeasurementError('registration resistor is not in the network')
    node_a, node_b = endpoints[resistor_id]
    if positive_node != node_a and positive_node != node_b:
        raise MeasurementError('positive probe must be a resistor endpoint')
    gain = checks.number(item['gain'])
    if gain <= 0:
        raise MeasurementError('gain must be positive')
    checks.number(item['offset_volts'])
    return (station, channel), start, end


def _validate_request_shape(request):
    if not isinstance(request, dict):
        raise MeasurementError('request must be an object')
    # network 是否合法由业务层按当前版号裁决（首版必需、后续禁止；
    # 重放首版请求时携带 network 是正常情形）。
    checks.fields(request, _BASE_FIELDS, ('network',))
    run_id = checks.ident(request['run_id'])
    request_id = checks.ident(request['request_id'])
    base_version = request['base_version']
    if type(base_version) is not int or base_version < 0:
        raise MeasurementError('base_version must be a non-negative integer')
    readings = checks.array(request['readings'], 0, MAX_READINGS)
    for reading in readings:
        _validate_reading(reading)
    return run_id, request_id, base_version, readings


def _validate_network(network):
    try:
        _nodes, edges = parse_network(network)
    except ValueError as exc:
        raise MeasurementError('invalid network: %s' % exc) from exc
    return {edge['id']: (edge['a'], edge['b']) for edge in edges}


def _validate_registrations(registrations, endpoints):
    items = checks.array(registrations, 0, MAX_REGISTRATIONS)
    intervals = {}
    validated = []
    for item in items:
        key, start, end = _validate_registration(item, endpoints)
        intervals.setdefault(key, []).append((start, end))
        validated.append(item)
    for ranges in intervals.values():
        ranges.sort()
        for (_, prev_end), (next_start, _) in zip(ranges, ranges[1:]):
            # 左闭右开：相接（prev_end == next_start）与缺失允许，重叠拒绝。
            if next_start < prev_end:
                raise MeasurementError('registration intervals overlap')
    return validated


# --------------------------------------------------------------------------
# 原始读数存取
# --------------------------------------------------------------------------

def _load_all_raw(paths):
    """返回 {(station, row_id): reading dict}，内容为各文件落盘原文。"""
    result = {}
    if not os.path.isdir(paths.raw_dir):
        return result
    for station in sorted(os.listdir(paths.raw_dir)):
        station_dir = os.path.join(paths.raw_dir, station)
        if not os.path.isdir(station_dir):
            continue
        for filename in sorted(os.listdir(station_dir)):
            if not filename.endswith('.json'):
                continue
            row_id = filename[:-len('.json')]
            with open(os.path.join(station_dir, filename), 'rb') as handle:
                result[(station, row_id)] = json.loads(handle.read())
    return result


def _merge_readings(stored, incoming):
    """合并本批读数；同身份同内容去重，同身份异内容拒绝。"""
    batch = {}
    for item in incoming:
        identity = (item['station'], item['row_id'])
        if identity in batch:
            if _canonical(batch[identity]) != _canonical(item):
                raise MeasurementError(
                    'conflicting raw readings for identity %r' % (identity,))
            continue  # 同身份同内容的重发，只保留一条
        batch[identity] = item
    merged = dict(stored)
    for identity, item in batch.items():
        if identity in stored:
            if _canonical(stored[identity]) != _canonical(item):
                raise MeasurementError(
                    'conflicting raw readings for identity %r' % (identity,))
        merged[identity] = item
    if len(merged) > MAX_READINGS:
        raise MeasurementError('run exceeds %d unique raw readings'
                               % MAX_READINGS)
    return merged, [key for key in batch if key not in stored]


# --------------------------------------------------------------------------
# 逐版快照计算（输入仅为实测读数与登记）
# --------------------------------------------------------------------------

def _build_registration_index(registrations):
    groups = {}
    for reg in registrations:
        groups.setdefault((reg['station'], reg['channel']), []).append(reg)
    index = {}
    for key, entries in groups.items():
        entries.sort(key=lambda r: r['start_tick'])
        index[key] = ([r['start_tick'] for r in entries], entries)
    return index


def _lookup_registration(index, station, channel, tick):
    location = index.get((station, channel))
    if location is None:
        return None
    starts, entries = location
    pos = bisect_right(starts, tick) - 1
    if pos < 0:
        return None
    candidate = entries[pos]  # 左闭右开：start <= tick < end
    if candidate['end_tick'] <= tick:
        return None
    return candidate


def _compute_snapshot(readings, registrations, endpoints):
    """用截至本版的全部读数与本版完整登记计算观测、冲突与逐身份证据。"""
    index = _build_registration_index(registrations)
    bindings = {}       # identity -> (registration, Fraction, obs_key)
    grouped = {}        # obs_key -> [(identity, Fraction, registration)]
    for identity, reading in readings.items():
        reg = _lookup_registration(index, reading['station'],
                                   reading['channel'], reading['tick'])
        if reg is None:
            continue
        raw = Fraction(reading['raw_volts'])
        gain = Fraction(reg['gain'])
        offset = Fraction(reg['offset_volts'])
        value = raw * gain + offset
        node_a, node_b = endpoints[reg['resistor_id']]
        if reg['positive_node'] == node_b:
            value = -value  # 正探针接 b：相对电阻 a->b 正向整体反号
        obs_key = (reading['tick'], reg['resistor_id'])
        bindings[identity] = (reg, value, obs_key)
        grouped.setdefault(obs_key, []).append((identity, value, reg))

    observations = []
    conflicts = []
    agreement = {}      # obs_key -> 'accepted' | 'conflict'
    for obs_key in sorted(grouped):
        entries = grouped[obs_key]
        sources = sorted(identity for identity, _v, _r in entries)
        distinct = {value for _i, value, _r in entries}
        if len(distinct) == 1:
            value = next(iter(distinct))
            observations.append({
                'tick': obs_key[0],
                'resistor_id': obs_key[1],
                'volts': str(value),  # 现有有理数字符串：整数或 n/d
                'sources': [list(identity) for identity in sources],
            })
            agreement[obs_key] = _STATUS_ACCEPTED
        else:
            # 整键不发布：不择一、不平均、不保留上一版，全部身份列证据。
            conflicts.append({
                'tick': obs_key[0],
                'resistor_id': obs_key[1],
                'sources': [list(identity) for identity in sources],
            })
            agreement[obs_key] = _STATUS_CONFLICT

    evidence = []
    for identity in sorted(readings):
        reading = readings[identity]
        entry = {
            'identity': list(identity),
            'reading': reading,
            'status': _STATUS_UNBOUND,
            'registration': None,
            'converted_volts': None,
            'observation_key': None,
        }
        if identity in bindings:
            reg, value, obs_key = bindings[identity]
            entry.update({
                'status': agreement[obs_key],
                'registration': reg,
                'converted_volts': str(value),
                'observation_key': list(obs_key),
            })
        evidence.append(entry)
    return observations, conflicts, evidence


def _build_changeset(previous_rows, current_rows, from_version, to_version):
    """有源版与目标版号的增量；来源集合变化也算改变。"""
    removed = [list(key) for key in sorted(previous_rows)
               if key not in current_rows]
    upserted = [current_rows[key] for key in sorted(current_rows)
                if key not in previous_rows
                or previous_rows[key] != current_rows[key]]
    return {
        'from_version': from_version,
        'to_version': to_version,
        'removed': removed,
        'upserted': upserted,
    }


def _rows_by_key(observations):
    return {(row['tick'], row['resistor_id']): row for row in observations}


# --------------------------------------------------------------------------
# 版本目录与崩溃恢复
# --------------------------------------------------------------------------

def _read_current(paths):
    if not os.path.exists(paths.current):
        return 0
    with open(paths.current, 'rb') as handle:
        return int(json.loads(handle.read())['version'])


def _write_current(paths, version):
    _atomic_write(paths.current, {'version': version})


def _request_records(paths):
    """返回 {request_id: record}；仅含已落盘的成功请求记录。"""
    records = {}
    if not os.path.isdir(paths.request_dir):
        return records
    for filename in sorted(os.listdir(paths.request_dir)):
        if not filename.endswith('.json'):
            continue
        with open(os.path.join(paths.request_dir, filename), 'rb') as handle:
            record = json.loads(handle.read())
        records[record['request_id']] = record
    return records


def _recover(paths, current_version):
    """在持锁状态下收尾上次中断的提交，并清理暂存残骸。

    中断点只有三种后果：
    * 停在版本目录改名/请求记录之前：只剩暂存目录，删除即可；
    * 版本目录与请求记录已落盘但 current 未切换：补做切换；
    * 版本目录存在但无请求记录：属孤儿目录，删除（raw 文件不可变，保留）。
    """
    if os.path.isdir(paths.version_dir_root):
        for name in sorted(os.listdir(paths.version_dir_root)):
            if name.startswith('.staging-'):
                shutil.rmtree(os.path.join(paths.version_dir_root, name))
    records = _request_records(paths)
    recorded_versions = {record['version']: request_id
                         for request_id, record in records.items()}
    version = current_version
    while True:
        next_version = version + 1
        next_dir = paths.version_dir(next_version)
        if not os.path.isdir(next_dir):
            break
        if next_version in recorded_versions:
            _write_current(paths, next_version)  # 补完中断的原子切换
            version = next_version
        else:
            shutil.rmtree(next_dir)  # 孤儿版本，对消费者从未可见
    return version


def _load_version(paths, version):
    directory = paths.version_dir(version)
    if not os.path.isdir(directory):
        raise MeasurementError('unknown version %d' % version)
    with open(os.path.join(directory, 'meta.json'), 'rb') as handle:
        meta = json.loads(handle.read())
    with open(os.path.join(directory, 'observations.json'), 'rb') as handle:
        observation_payload = json.loads(handle.read())
    with open(os.path.join(directory, 'evidence.json'), 'rb') as handle:
        evidence_payload = json.loads(handle.read())
    with open(os.path.join(directory, 'changes.json'), 'rb') as handle:
        changeset = json.loads(handle.read())
    return {
        'run_id': meta['run_id'],
        'version': version,
        'observations': observation_payload['observations'],
        'conflicts': observation_payload['conflicts'],
        'evidence': evidence_payload['evidence'],
        'changeset': changeset,
    }


def _empty_version_zero(run_id):
    return {
        'run_id': run_id,
        'version': 0,
        'observations': [],
        'conflicts': [],
        'evidence': [],
        'changeset': None,
    }


# --------------------------------------------------------------------------
# 发布
# --------------------------------------------------------------------------

def publish(store_dir, request, _hooks=None):
    """发布一个新版本。

    成功前提：``base_version`` 等于当前版，且 ``request_id`` 未被另一内容
    使用。返回目标版号、变更集与该版完整快照。已成功的
    ``(run_id, request_id)`` 重放同一请求内容时，返回原版号与原版变更集
    （旧底版照样重放），不再推进版本。任何校验失败或过期底版都不会改动
    已提交数据。``_hooks`` 为测试用故障注入点：
    ``raw_written`` / ``version_renamed`` / ``request_recorded`` /
    ``before_current_swap``，回调抛异常即模拟该点进程中断。
    """
    try:
        return _publish(store_dir, request, _hooks or {})
    except MeasurementError:
        raise
    except ValueError as exc:
        raise MeasurementError(str(exc)) from exc


def _hook(hooks, point):
    callback = hooks.get(point)
    if callback is not None:
        callback(point)


def _publish(store_dir, request, hooks):
    if not isinstance(request, dict):
        raise MeasurementError('request must be an object')
    # 先校验 run_id，再把它作为路径组件，避免非法 ID 触及 store 之外。
    run_id_hint = checks.ident(request.get('run_id'))
    paths = _Paths(store_dir, run_id_hint)
    with _file_lock(paths.lock, exclusive=True):
        current_version = _recover(paths, _read_current(paths))
        network_required = current_version == 0
        run_id, request_id, base_version, readings = \
            _validate_request_shape(request)

        records = _request_records(paths)
        if request_id in records:
            record = records[request_id]
            if record['content'] != _canonical(request):
                raise MeasurementError(
                    'request_id %r was already used with different content'
                    % request_id)
            # 同 ID 同内容：无论底版多旧都重放原结果，不再推进。
            return _load_version(paths, record['version'])

        if base_version != current_version:
            raise MeasurementError(
                'stale base_version: current is %d, request based on %d'
                % (current_version, base_version))

        if network_required:
            if not isinstance(request.get('network'), dict):
                raise MeasurementError('first version requires a network')
            endpoints = _validate_network(request['network'])
        elif 'network' in request:
            raise MeasurementError(
                'network is accepted only on the first version')
        else:
            with open(paths.network, 'rb') as handle:
                endpoints = {edge['id']: (edge['a'], edge['b'])
                             for edge in json.loads(handle.read())['resistors']}
        registrations = _validate_registrations(
            request['registrations'], endpoints)

        stored = _load_all_raw(paths)
        merged, new_identities = _merge_readings(stored, readings)

        observations, conflicts, evidence = _compute_snapshot(
            merged, registrations, endpoints)
        current_rows = _rows_by_key(observations)
        if current_version == 0:
            previous_rows = {}
        else:
            previous = _load_version(paths, current_version)
            previous_rows = _rows_by_key(previous['observations'])
        next_version = current_version + 1
        changeset = _build_changeset(
            previous_rows, current_rows, current_version, next_version)

        # --- 持久化：先不可变原始读数，再完整版本目录，再请求记录，最后切换
        for identity in new_identities:
            _atomic_write(paths.raw_file(*identity), merged[identity])
        _hook(hooks, 'raw_written')

        if network_required:
            _atomic_write(paths.network, request['network'])

        staging = paths.staging_dir(next_version)
        if os.path.exists(staging):
            shutil.rmtree(staging)
        os.makedirs(staging)
        _atomic_write(os.path.join(staging, 'meta.json'), {
            'run_id': run_id,
            'version': next_version,
            'request_id': request_id,
            'registrations': registrations,
        })
        _atomic_write(os.path.join(staging, 'observations.json'), {
            'observations': observations,
            'conflicts': conflicts,
        })
        _atomic_write(os.path.join(staging, 'evidence.json'),
                      {'evidence': evidence})
        _atomic_write(os.path.join(staging, 'changes.json'), changeset)
        final_dir = paths.version_dir(next_version)
        os.replace(staging, final_dir)  # 同一文件系统上的原子改名
        _hook(hooks, 'version_renamed')

        _atomic_write(paths.request_file(request_id), {
            'request_id': request_id,
            'version': next_version,
            'content': _canonical(request),
        })
        _hook(hooks, 'request_recorded')

        _hook(hooks, 'before_current_swap')
        _write_current(paths, next_version)
        return _load_version(paths, next_version)


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------

def read_version(store_dir, run_id, version):
    """读取指定版本；版号、观测、证据与变更集保证同属该版本。

    未知历史版本报错，绝不回落到当前版。版本 0 是任何运行的空基线
    （无观测、无证据、无变更集）。
    """
    try:
        run_id = checks.ident(run_id)
        if type(version) is not int or version < 0:
            raise MeasurementError('version must be a non-negative integer')
        paths = _Paths(store_dir, run_id)
        with _file_lock(paths.lock, exclusive=False):
            current_version = _read_current(paths)
            if version > current_version:
                raise MeasurementError(
                    'unknown version %d for run %r' % (version, run_id))
            if version == 0:
                return _empty_version_zero(run_id)
            return _load_version(paths, version)
    except MeasurementError:
        raise
    except ValueError as exc:
        raise MeasurementError(str(exc)) from exc


def read_current(store_dir, run_id):
    """读取当前版本的完整快照与变更集。"""
    try:
        run_id = checks.ident(run_id)
    except ValueError as exc:
        raise MeasurementError(str(exc)) from exc
    paths = _Paths(store_dir, run_id)
    with _file_lock(paths.lock, exclusive=False):
        return read_version(store_dir, run_id, _read_current(paths))


def read_evidence(store_dir, run_id, station, row_id, version=None):
    """按原始身份查某版（默认当前版）固定下来的证据。"""
    identity = [station, row_id]
    if version is None:
        snapshot = read_current(store_dir, run_id)
    else:
        snapshot = read_version(store_dir, run_id, version)
    for entry in snapshot['evidence']:
        if entry['identity'] == identity:
            return entry
    raise MeasurementError('unknown raw identity %r in version %r'
                           % (tuple(identity), snapshot['version']))
