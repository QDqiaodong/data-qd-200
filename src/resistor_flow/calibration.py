"""离线标定参数可行集、条件资格复验与盲测取数交付。

这是在已发布实测归档与 ``datasets.build_datasets`` 契约之上的一轮**只读**
离线批处理，不操作设备、不连接外部服务，也不改动发布、求解、数据集构建、
复核与交接的任何既有接口。

一轮调用完成三件事：

1. 按现有数据集构建契约取得 ``fit.rows`` / ``holdout.rows``（成组、配额与
   多解选择规则完全沿用，不重新抽样，调用方不能指定某行进入哪一侧），以每行
   标准观测 ``volts`` 作为读数 ``x``；
2. 用拟合行的参考真值区间，在调用方给定的增益/偏置矩形内求标定关系
   ``y = g*x + b`` 的**完整精确可行参数集**（凸集，可能是区域、线段或点，
   也可能为空），随后在整个可行集上逐见证求换算极值来签发 L/H 资格；
3. 按资格与覆盖范围裁决下游盲测请求，绝对量或“第一减第二”的差值都在
   **同一个参数对**上求精确闭区间，差值绝不分别求区间再相减。

所有计算使用 :class:`fractions.Fraction`，等于边界算满足；输出数值统一用
``str(Fraction)`` 的整数或 ``n/d`` 形式。完整约定与复现步骤见
``docs/calibration.md``。
"""

from fractions import Fraction
import os

from . import checks
from .datasets import (
    DatasetInputError,
    HoldoutInfeasible,
    build_datasets,
)

# 业务上限。
MAX_REQUESTS = 36_000
CONDITIONS = ('L', 'H')

# 整批状态。
STATUS_FEASIBLE = 'feasible'
STATUS_FIT_EMPTY = 'fit_empty'
STATUS_FIT_INFEASIBLE = 'fit_infeasible'

_BATCH_STATUS_ZH = {
    STATUS_FEASIBLE: '可裁决',
    STATUS_FIT_EMPTY: '没有拟合行',
    STATUS_FIT_INFEASIBLE: '拟合无可行参数',
}

# 资格状态。
QUAL_ISSUED = 'issued'
QUAL_NOT_ISSUED = 'not_issued'

# 下游请求状态（交付时为 delivered，其余为拒付）。
ITEM_DELIVERED = 'delivered'
ITEM_CONDITION_NOT_ISSUED = 'condition_not_qualified'
ITEM_OUT_OF_COVERAGE = 'out_of_coverage'
ITEM_INSUFFICIENT_PRECISION = 'insufficient_precision'

ITEM_STATUS_ZH = {
    ITEM_DELIVERED: '已交付',
    ITEM_CONDITION_NOT_ISSUED: '条件未签发',
    ITEM_OUT_OF_COVERAGE: '超出覆盖范围',
    ITEM_INSUFFICIENT_PRECISION: '精度不足',
}

# 失败原因码。
REASON_REFERENCE = 'reference_not_contained'
REASON_COVERAGE = 'insufficient_distinct_coverage'
REASON_FIT_EMPTY = 'fit_empty'
REASON_FIT_INFEASIBLE = 'fit_infeasible'


class CalibrationError(ValueError):
    """标定/资格/交付批处理失败的基类（始终是 ValueError）。"""


class CalibrationInputError(CalibrationError, DatasetInputError):
    """调用方输入不合法（含参考表覆盖错误）；不会交付任何部分结果。

    同时是 :class:`resistor_flow.datasets.DatasetInputError`，因此既有按
    数据集输入错误捕获的调用方也能统一处理；合法清单但无法精确拆分的
    :class:`HoldoutInfeasible` **不**是本异常，仍按既有独立分类抛出。
    """


# 面向不同调用习惯的别名。
QualificationError = CalibrationError
QualificationInputError = CalibrationInputError
DeliveryError = CalibrationError
DeliveryInputError = CalibrationInputError


def _error(message):
    raise CalibrationInputError(message)


def _s(value):
    """精确有理数 -> 现有整数或 ``n/d`` 字符串。"""
    return str(value)


# --------------------------------------------------------------------------
# 数值与标识解析
# --------------------------------------------------------------------------

def _decimal(value, what):
    """现有有界十进制字符串口径。"""
    try:
        return checks.number(value)
    except (ValueError, TypeError):
        _error('%s must be a bounded decimal string' % what)


def _archived_volts(value, what):
    """归档标准观测 ``volts``：原样保留的精确有理数字符串（整数或 n/d）。"""
    if not isinstance(value, str):
        _error('%s must be the archived exact rational string' % what)
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError):
        _error('%s is not an exact rational string' % what)
    if result.denominator == 0:  # pragma: no cover - Fraction 已拒绝
        _error('%s has a zero denominator' % what)
    return result


def _ident(value, what):
    try:
        return checks.ident(value)
    except (ValueError, TypeError):
        _error('invalid %s' % what)


def _tick(value, what):
    # 与 datasets 相同的 tick 口径；布尔值不算整数。
    if type(value) is not int or not 0 <= value <= 9_999_999:
        _error('%s must be an integer tick in 0..9999999' % what)
    return value


def _condition(value):
    if not isinstance(value, str) or value not in CONDITIONS:
        _error('condition must be "L" or "H"')
    return value


_LOWER_KEYS = ('lower', 'lo', 'low', 'min', 'minimum', 'lower_bound',
               'ref_low', 'left')
_UPPER_KEYS = ('upper', 'hi', 'high', 'max', 'maximum', 'upper_bound',
               'ref_high', 'right')


def _pick_alias(obj, names, what, required=True):
    for name in names:
        if name in obj:
            return obj[name]
    if required:
        _error('%s is missing one of fields %s' % (what, ', '.join(names)))
    return None


def _parse_bound_pair(value, what):
    """把多种常见区间写法归一为 (lower Fraction, upper Fraction)。"""
    if isinstance(value, dict):
        low_raw = _pick_alias(value, _LOWER_KEYS, what)
        high_raw = _pick_alias(value, _UPPER_KEYS, what)
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        low_raw, high_raw = value
    else:
        _error('%s must be a [lower, upper] pair or object' % what)
    low = _decimal(low_raw, what + ' lower')
    high = _decimal(high_raw, what + ' upper')
    if low > high:
        _error('%s has lower bound greater than upper bound' % what)
    return low, high


def _parse_parameter_range(value, canonical, *, require_positive=False):
    if isinstance(value, dict):
        low_raw = _pick_alias(value, _LOWER_KEYS + (canonical + '_low',
                                                     canonical + '_min',
                                                     canonical + '_lo'),
                              canonical + '_range')
        high_raw = _pick_alias(value, _UPPER_KEYS + (canonical + '_high',
                                                     canonical + '_max',
                                                     canonical + '_hi'),
                               canonical + '_range')
        low = _decimal(low_raw, canonical + ' lower endpoint')
        high = _decimal(high_raw, canonical + ' upper endpoint')
    else:
        low, high = _parse_bound_pair(value, canonical + '_range')
    if low > high:
        _error('%s range lower endpoint exceeds upper endpoint' % canonical)
    if require_positive and (low <= 0 or high <= 0):
        _error('both gain range endpoints must be strictly positive')
    return low, high


# --------------------------------------------------------------------------
# 参考区间表解析（支持三元组键映射或条目数组）
# --------------------------------------------------------------------------

def _reference_key(run_id, tick, resistor_id):
    return (_ident(run_id, 'reference run_id'),
            _tick(tick, 'reference tick'),
            _ident(resistor_id, 'reference resistor_id'))


def _reference_from_object(entry, origin):
    if not isinstance(entry, dict):
        _error('%s must be an object' % origin)
    key = _reference_key(entry.get('run_id'), entry.get('tick'),
                         entry.get('resistor_id'))
    if 'reference' in entry:
        bounds = _parse_bound_pair(entry['reference'],
                                   'reference for %r' % (key,))
    elif 'range' in entry:
        bounds = _parse_bound_pair(entry['range'],
                                   'reference for %r' % (key,))
    elif 'interval' in entry:
        bounds = _parse_bound_pair(entry['interval'],
                                   'reference for %r' % (key,))
    elif 'bounds' in entry:
        bounds = _parse_bound_pair(entry['bounds'],
                                   'reference for %r' % (key,))
    else:
        low_raw = _pick_alias(entry, _LOWER_KEYS,
                              'reference for %r' % (key,))
        high_raw = _pick_alias(entry, _UPPER_KEYS,
                               'reference for %r' % (key,))
        low = _decimal(low_raw, 'reference lower for %r' % (key,))
        high = _decimal(high_raw, 'reference upper for %r' % (key,))
        if low > high:
            _error('reference for %r has lower > upper' % (key,))
        bounds = (low, high)
    return key, bounds


def parse_references(raw):
    """解析参考区间表为 ``{(run_id,tick,resistor_id): (lo, hi)}``。

    接受：

    * 以三元组（或长度 3 数组）为键、``[lo, hi]``/区间对象为值的映射；
    * 嵌套映射 ``{run_id: {tick: {resistor_id: [lo, hi]}}}``；
    * 含 ``run_id/tick/resistor_id`` 与上下界字段的对象数组；
    * ``[key_triple, [lo, hi]]``、``[key_triple, lo, hi]``、
      ``[run_id, tick, resistor_id, [lo, hi]]`` 或
      ``[run_id, tick, resistor_id, lo, hi]`` 数组条目。
    """
    if raw is None:
        _error('reference table is required')
    result = {}

    def store(key, bounds):
        if key in result:
            _error('duplicate reference key %r' % (key,))
        result[key] = bounds

    if isinstance(raw, dict):
        for raw_key, raw_value in raw.items():
            if isinstance(raw_key, tuple):
                if len(raw_key) != 3:
                    _error('reference tuple key must have length 3')
                key = _reference_key(*raw_key)
                bounds = _parse_bound_pair(
                    raw_value, 'reference for %r' % (key,))
                store(key, bounds)
            elif isinstance(raw_key, str):
                run_id = _ident(raw_key, 'reference run_id')
                if not isinstance(raw_value, dict):
                    _error('reference for run %r must be an object' % run_id)
                for tick_raw, by_resistor in raw_value.items():
                    tick = _coerce_tick_key(tick_raw)
                    if not isinstance(by_resistor, dict):
                        _error('reference tick %r must map resistor ids'
                               % tick_raw)
                    for resistor_id, interval in by_resistor.items():
                        key = _reference_key(run_id, tick, resistor_id)
                        bounds = _parse_bound_pair(
                            interval, 'reference for %r' % (key,))
                        store(key, bounds)
            else:
                _error('reference table keys must be triple tuples')
        return result

    if not isinstance(raw, (list, tuple)):
        _error('reference table must be an object keyed by row triple '
               'or an array of entries')

    for index, item in enumerate(raw):
        origin = 'reference[%d]' % index
        if isinstance(item, dict):
            key, bounds = _reference_from_object(item, origin)
        elif isinstance(item, (list, tuple)) and len(item) == 2 \
                and isinstance(item[0], (list, tuple)):
            if len(item[0]) != 3:
                _error('%s triple key must have length 3' % origin)
            key = _reference_key(*item[0])
            bounds = _parse_bound_pair(item[1], origin)
        elif isinstance(item, (list, tuple)) and len(item) == 3 \
                and isinstance(item[0], (list, tuple)):
            if len(item[0]) != 3:
                _error('%s triple key must have length 3' % origin)
            key = _reference_key(*item[0])
            bounds = _parse_bound_pair([item[1], item[2]], origin)
        elif isinstance(item, (list, tuple)) and len(item) == 4 \
                and not isinstance(item[0], (list, tuple)):
            key = _reference_key(item[0], item[1], item[2])
            bounds = _parse_bound_pair(item[3], origin)
        elif isinstance(item, (list, tuple)) and len(item) == 5 \
                and not isinstance(item[0], (list, tuple)):
            key = _reference_key(item[0], item[1], item[2])
            low = _decimal(item[3], origin + ' lower')
            high = _decimal(item[4], origin + ' upper')
            if low > high:
                _error('%s lower > upper' % origin)
            bounds = (low, high)
        else:
            _error('%s is not a recognizable reference entry' % origin)
        store(key, bounds)
    return result


def _coerce_tick_key(value):
    # JSON 对象的整数键会变成字符串；Python 调用也可能直接给 int。
    if type(value) is int:
        return _tick(value, 'reference tick')
    if isinstance(value, str):
        integer = _safe_int(value)
        if integer is not None and value.strip() == str(integer):
            return _tick(integer, 'reference tick')
    _error('reference nested tick key %r is not an integer' % (value,))


def _safe_int(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------
# 下游请求解析（先完整校验，再作业务裁决）
# --------------------------------------------------------------------------

_WIDTH_KEYS = ('max_width', 'width_limit', 'tolerance_width',
               'allowed_width', 'width_tolerance', 'width', 'tolerance')
_ID_KEYS = ('request_id', 'id')
_SINGLE_KEYS = ('reading', 'target', 'point', 'sample', 'value', 'query')
_FIRST_KEYS = ('first', 'a', 'left', 'minuend', 'reading1', 'x1')
_SECOND_KEYS = ('second', 'b', 'right', 'subtrahend', 'reading2', 'x2')


def _parse_reading(raw, what):
    if not isinstance(raw, dict):
        _error('%s must be an object with condition and x' % what)
    condition = _condition(raw.get('condition'))
    x_raw = raw.get('x', raw.get('volts', raw.get('reading_x')))
    x = _decimal(x_raw, what + ' x')
    return {'condition': condition, 'x': x, 'x_text': _s(x)}


def _parse_one_request(raw, index, seen_ids):
    if not isinstance(raw, dict):
        _error('requests[%d] must be an object' % index)

    request_id = None
    for name in _ID_KEYS:
        if name in raw:
            request_id = raw[name]
            break
    if request_id is None:
        _error('requests[%d] is missing request_id' % index)
    request_id = _ident(request_id, 'request_id')
    if request_id in seen_ids:
        _error('duplicate request_id %r' % request_id)
    seen_ids.add(request_id)

    width_raw = _pick_alias(raw, _WIDTH_KEYS,
                            'request %r tolerance width' % request_id)
    max_width = _decimal(width_raw,
                         'request %r tolerance width' % request_id)
    if max_width < 0:
        _error('request %r tolerance width must be non-negative' % request_id)

    # 读数：显式 first/second、长度 1/2 的 readings，或单个 reading。
    first_raw = _pick_alias(raw, _FIRST_KEYS, 'x', required=False)
    second_raw = _pick_alias(raw, _SECOND_KEYS, 'x', required=False)
    readings_raw = raw.get('readings')
    single_raw = _pick_alias(raw, _SINGLE_KEYS, 'x', required=False)

    readings = None
    if first_raw is not None or second_raw is not None:
        if first_raw is None or second_raw is None:
            _error('request %r difference needs both first and second'
                   % request_id)
        readings = [_parse_reading(first_raw, request_id + ' first'),
                    _parse_reading(second_raw, request_id + ' second')]
    elif readings_raw is not None:
        if not isinstance(readings_raw, (list, tuple)) or \
                len(readings_raw) not in (1, 2):
            _error('request %r readings must contain one or two readings'
                   % request_id)
        readings = [_parse_reading(item, '%s readings[%d]' % (request_id, j))
                    for j, item in enumerate(readings_raw)]
    elif single_raw is not None:
        readings = [_parse_reading(single_raw, request_id + ' reading')]
    elif 'condition' in raw and 'x' in raw:
        readings = [_parse_reading(raw, request_id + ' reading')]
    else:
        _error('request %r must contain one reading or first/second pair'
               % request_id)

    kind_hint = raw.get('kind', raw.get('type', raw.get('mode',
                                                        raw.get('op'))))
    if kind_hint is not None:
        if not isinstance(kind_hint, str):
            _error('request %r kind must be a string' % request_id)
        hint = kind_hint.strip().lower()
        if hint in ('absolute', 'abs', 'value', 'single', 'reading',
                    'converted'):
            hinted = 'absolute'
        elif hint in ('difference', 'diff', 'subtract', 'minus',
                      'first_minus_second'):
            hinted = 'difference'
        else:
            _error('request %r has unknown kind %r' % (request_id, kind_hint))
        if (hinted == 'absolute') != (len(readings) == 1):
            _error('request %r kind %r disagrees with reading count'
                   % (request_id, kind_hint))

    kind = 'absolute' if len(readings) == 1 else 'difference'
    return {
        'request_id': request_id,
        'kind': kind,
        'max_width': max_width,
        'readings': readings,
    }


def parse_requests(raw):
    if raw is None:
        return []
    if isinstance(raw, dict):
        items = []
        for request_id, body in raw.items():
            if not isinstance(body, dict):
                _error('request %r body must be an object' % request_id)
            merged = dict(body)
            merged.setdefault('request_id', request_id)
            items.append(merged)
        raw = items
    if not isinstance(raw, (list, tuple)):
        _error('requests must be an array or an id-keyed object')
    if len(raw) > MAX_REQUESTS:
        _error('at most %d downstream requests are allowed' % MAX_REQUESTS)
    seen = set()
    return [_parse_one_request(item, index, seen)
            for index, item in enumerate(raw)]


# --------------------------------------------------------------------------
# 精确二维可行集：直线上包络 / 下包络 + 凸查询链
#
# 约束 lo <= g*x + b <= hi 等价于
#   B_lo(g)=max(max_fit(lo-x*g), b_lo) <= b
#   B_hi(g)=min(min_fit(hi-x*g), b_hi) >= b
# 可行 g 是 {B_lo<=B_hi} 这个凸函数的下水平区间 [G0,G1]；
# 最大换算值沿凹链 B_hi，最小沿凸链 B_lo，查询为单峰序列二分。
# --------------------------------------------------------------------------

def _max_envelope(lines):
    """返回 (hull_lines, start_thresholds)。

    ``hull_lines[j]=(m,c)`` 是上包络第 j 段直线；``start_thresholds[j]``
    为该段开始占优的 g（j=0 时为 None）。斜率严格递增，阈值严格递增。
    """
    best = {}
    for slope, intercept in lines:
        old = best.get(slope)
        if old is None or intercept > old:
            best[slope] = intercept
    hull = []
    for slope in sorted(best):
        intercept = best[slope]
        while len(hull) >= 2:
            m1, c1 = hull[-2]
            m2, c2 = hull[-1]
            # x12=(c1-c2)/(m2-m1) >= x23=(c2-c)/(m-m2) 时中间线冗余。
            if (c1 - c2) * (slope - m2) >= (c2 - intercept) * (m2 - m1):
                hull.pop()
            else:
                break
        hull.append((slope, intercept))
    thresholds = [None]
    for j in range(1, len(hull)):
        m1, c1 = hull[j - 1]
        m2, c2 = hull[j]
        thresholds.append((c1 - c2) / (m2 - m1))
    return hull, thresholds


def _active_index(thresholds, g):
    """g 所在包络段下标；thresholds[0] 为 None。"""
    marks = thresholds[1:]
    left, right = 0, len(marks)
    while left < right:
        mid = (left + right) // 2
        if marks[mid] <= g:
            left = mid + 1
        else:
            right = mid
    return left


def _envelope_value(hull, thresholds, g):
    slope, intercept = hull[_active_index(thresholds, g)]
    return slope * g + intercept


def _chain_vertices(hull, thresholds, low, high):
    gs = {low, high}
    for threshold in thresholds[1:]:
        if low <= threshold <= high:
            gs.add(threshold)
    return [(g, _envelope_value(hull, thresholds, g)) for g in sorted(gs)]


class FeasibleSpace:
    """拟合约束与参数矩形交出的完整精确可行集。

    ``empty`` 为 True 时其余字段无意义。否则 ``g_min/g_max`` 是可行 g 的闭
    区间端点，``low_chain`` 为凸下边界 ``B_lo`` 的顶点，``high_chain`` 为凹
    上边界 ``B_hi`` 的顶点（均已裁到 ``[g_min,g_max]``）。
    """

    __slots__ = ('empty', 'g_min', 'g_max', 'low_chain', 'high_chain')

    def __init__(self, fit_rows, references, gain_range, offset_range):
        g_low, g_high = gain_range
        b_low, b_high = offset_range
        low_lines = [( -row['x'], row['ref_low']) for row in fit_rows]
        low_lines.append((Fraction(0), b_low))
        # B_hi = -max(x*g - hi)，复用同一个上包络构造。
        high_neg_lines = [(row['x'], -row['ref_high']) for row in fit_rows]
        high_neg_lines.append((Fraction(0), -b_high))

        low_hull, low_th = _max_envelope(low_lines)
        high_hull_neg, high_th_neg = _max_envelope(high_neg_lines)

        def b_low_at(g):
            return _envelope_value(low_hull, low_th, g)

        def b_high_at(g):
            return -_envelope_value(high_hull_neg, high_th_neg, g)

        boundaries = {g_low, g_high}
        for threshold in low_th[1:]:
            if g_low <= threshold <= g_high:
                boundaries.add(threshold)
        for threshold in high_th_neg[1:]:
            if g_low <= threshold <= g_high:
                boundaries.add(threshold)
        boundaries = sorted(boundaries)

        g_min = None
        g_max = None

        def consider(g):
            nonlocal g_min, g_max
            if b_low_at(g) <= b_high_at(g):
                if g_min is None or g < g_min:
                    g_min = g
                if g_max is None or g > g_max:
                    g_max = g

        for point in boundaries:
            consider(point)

        # 合并断点后每一段上 D=B_lo-B_hi 都是线性的；严格穿越时补精确根。
        for left, right in zip(boundaries, boundaries[1:]):
            if left == right:
                continue
            mid = (left + right) / 2
            ml, cl = low_hull[_active_index(low_th, mid)]
            nmi, nci = high_hull_neg[_active_index(high_th_neg, mid)]
            mh, ch = -nmi, -nci  # B_hi = mh*g + ch
            d_slope = ml - mh
            d_const = cl - ch
            d_left = d_slope * left + d_const
            d_right = d_slope * right + d_const
            if (d_left > 0 and d_right < 0) or \
                    (d_left < 0 and d_right > 0):
                consider(-d_const / d_slope)

        if g_min is None:
            self.empty = True
            self.g_min = self.g_max = None
            self.low_chain = self.high_chain = None
            return

        self.empty = False
        self.g_min = g_min
        self.g_max = g_max
        low_vertices = _chain_vertices(low_hull, low_th, g_min, g_max)
        high_neg_vertices = _chain_vertices(high_hull_neg, high_th_neg,
                                            g_min, g_max)
        self.low_chain = low_vertices
        self.high_chain = [(g, -value) for g, value in high_neg_vertices]

    # -- 查询 -------------------------------------------------------------

    @staticmethod
    def _peak(coefficient_x, vertices):
        """凹（单峰）顶点序列上 max(x*g+b)，二分相邻值。"""
        left, right = 0, len(vertices) - 1

        def value(index):
            g, b = vertices[index]
            return coefficient_x * g + b

        while left < right:
            mid = (left + right) // 2
            if value(mid) < value(mid + 1):
                left = mid + 1
            else:
                right = mid
        return value(left)

    @staticmethod
    def _trough(coefficient_x, vertices):
        """凸（单谷）顶点序列上 min(x*g+b)，二分相邻值。"""
        left, right = 0, len(vertices) - 1

        def value(index):
            g, b = vertices[index]
            return coefficient_x * g + b

        while left < right:
            mid = (left + right) // 2
            if value(mid) > value(mid + 1):
                left = mid + 1
            else:
                right = mid
        return value(left)

    def absolute_range(self, x):
        """单个读数换算量 y=x*g+b 在整个可行集上的闭区间。"""
        minimum = self._trough(x, self.low_chain)
        maximum = self._peak(x, self.high_chain)
        return minimum, maximum

    def difference_range(self, x_first, x_second):
        """同一对 (g,b) 下 (g*x1+b)-(g*x2+b)=g*(x1-x2) 的闭区间。

        偏置严格抵消，且两个读数共用同一个 g；这不是两个绝对区间的相减。
        """
        slope = x_first - x_second
        if slope == 0:
            zero = Fraction(0)
            return zero, zero
        endpoint_a = slope * self.g_min
        endpoint_b = slope * self.g_max
        if endpoint_a <= endpoint_b:
            return endpoint_a, endpoint_b
        return endpoint_b, endpoint_a


# --------------------------------------------------------------------------
# 行装配与资格审计
# --------------------------------------------------------------------------

def _row_key(row):
    return row['run_id'], row['tick'], row['resistor_id']


def _attach_rows(dataset, references):
    fit_rows = []
    holdout_rows = []
    actual_keys = set()
    for side, target in (('fit', fit_rows), ('holdout', holdout_rows)):
        for row in dataset[side]['rows']:
            key = _row_key(row)
            actual_keys.add(key)
            ref = references.get(key)
            if ref is None:
                # 覆盖检查在外部统一给出 missing/extra 汇总。
                ref = (None, None)
            target.append({
                'key': key,
                'run_id': row['run_id'],
                'tick': row['tick'],
                'resistor_id': row['resistor_id'],
                'version': row['version'],
                'x': _archived_volts(row['volts'],
                                     'row %r volts' % (key,)),
                'volts_text': row['volts'],
                'condition': row['condition'],
                'ref_low': ref[0],
                'ref_high': ref[1],
            })
    return fit_rows, holdout_rows, actual_keys


def _require_reference_coverage(references, actual_keys):
    supplied = set(references)
    if supplied == actual_keys:
        return
    missing = actual_keys - supplied
    extra = supplied - actual_keys
    details = []
    if missing:
        details.append('%d missing (e.g. %r)'
                       % (len(missing), min(missing)))
    if extra:
        details.append('%d extra (e.g. %r)' % (len(extra), min(extra)))
    _error('reference table must exactly cover fit and holdout rows: %s'
           % '; '.join(details))


def _witness_audit(row, space):
    low_value = high_value = None
    passed = None
    if space is not None:
        low_value, high_value = space.absolute_range(row['x'])
        passed = (row['ref_low'] <= low_value
                  and high_value <= row['ref_high'])
    return {
        'key': list(row['key']),
        'run_id': row['run_id'],
        'tick': row['tick'],
        'resistor_id': row['resistor_id'],
        'version': row['version'],
        'x': _s(row['x']),
        'reference': [_s(row['ref_low']), _s(row['ref_high'])],
        'min_value': None if low_value is None else _s(low_value),
        'max_value': None if high_value is None else _s(high_value),
        'reference_passed': passed,
        'passed': passed,
    }


def _qualification(condition, holdout_rows, space, batch_status):
    rows = sorted((row for row in holdout_rows
                   if row['condition'] == condition),
                  key=lambda row: row['key'])
    witnesses = [_witness_audit(row, space) for row in rows]

    if space is None:
        reason = (REASON_FIT_EMPTY if batch_status == STATUS_FIT_EMPTY
                  else REASON_FIT_INFEASIBLE)
        return {
            'condition': condition,
            'issued': False,
            'status': QUAL_NOT_ISSUED,
            'coverage': None,
            'distinct_x_count': len({row['x'] for row in rows}),
            'witness_count': len(rows),
            'reference_passed': None,
            'coverage_sufficient': False,
            'failure_reasons': [reason],
            'witnesses': witnesses,
        }

    reference_passed = all(witness['reference_passed']
                           for witness in witnesses)
    distinct_x = sorted({row['x'] for row in rows})
    coverage_sufficient = len(distinct_x) >= 2
    issued = reference_passed and coverage_sufficient
    reasons = []
    if not reference_passed:
        reasons.append(REASON_REFERENCE)
    if not coverage_sufficient:
        reasons.append(REASON_COVERAGE)

    qualification = {
        'condition': condition,
        'issued': issued,
        'status': QUAL_ISSUED if issued else QUAL_NOT_ISSUED,
        'coverage': None,
        'distinct_x_count': len(distinct_x),
        'witness_count': len(rows),
        'reference_passed': reference_passed,
        'coverage_sufficient': coverage_sufficient,
        'failure_reasons': reasons,
        'witnesses': witnesses,
    }
    if issued:
        qualification['coverage'] = [_s(distinct_x[0]),
                                    _s(distinct_x[-1])]
        qualification['coverage_low'] = _s(distinct_x[0])
        qualification['coverage_high'] = _s(distinct_x[-1])
    return qualification


def _in_coverage(reading, qualifications):
    qualification = qualifications[reading['condition']]
    if not qualification['issued']:
        return False, False
    low = Fraction(qualification['coverage'][0])
    high = Fraction(qualification['coverage'][1])
    inside = low <= reading['x'] <= high
    return True, inside


def _request_item(parsed, qualifications, space):
    request_id = parsed['request_id']
    readings = parsed['readings']

    issued_flags = []
    coverage_flags = []
    for reading in readings:
        issued, inside = _in_coverage(reading, qualifications)
        issued_flags.append(issued)
        coverage_flags.append(inside)

    echo_readings = [{'condition': item['condition'], 'x': item['x_text']}
                     for item in readings]
    base = {
        'request_id': request_id,
        'kind': parsed['kind'],
        'max_width': _s(parsed['max_width']),
        'readings': echo_readings,
    }

    if not all(issued_flags):
        base.update({
            'status': ITEM_CONDITION_NOT_ISSUED,
            'status_text': ITEM_STATUS_ZH[ITEM_CONDITION_NOT_ISSUED],
            'delivered': False,
        })
        return base

    if not all(coverage_flags):
        base.update({
            'status': ITEM_OUT_OF_COVERAGE,
            'status_text': ITEM_STATUS_ZH[ITEM_OUT_OF_COVERAGE],
            'delivered': False,
        })
        return base

    if parsed['kind'] == 'absolute':
        lower, upper = space.absolute_range(readings[0]['x'])
    else:
        lower, upper = space.difference_range(readings[0]['x'],
                                              readings[1]['x'])
    width = upper - lower
    if width > parsed['max_width']:
        # 拒付项不附换算极值或由极值得到的宽度/点估计。
        base.update({
            'status': ITEM_INSUFFICIENT_PRECISION,
            'status_text': ITEM_STATUS_ZH[ITEM_INSUFFICIENT_PRECISION],
            'delivered': False,
        })
        return base

    lower_s, upper_s = _s(lower), _s(upper)
    base.update({
        'status': ITEM_DELIVERED,
        'status_text': ITEM_STATUS_ZH[ITEM_DELIVERED],
        'delivered': True,
        'lower': lower_s,
        'upper': upper_s,
        'min_value': lower_s,
        'max_value': upper_s,
        'range': [lower_s, upper_s],
        'interval': [lower_s, upper_s],
        'width': _s(width),
    })
    return base


# --------------------------------------------------------------------------
# 参数解析兼容层
# --------------------------------------------------------------------------

def _is_interval_like(value):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return True
    return isinstance(value, dict) and (
        any(key in value for key in _LOWER_KEYS)
        or any(key in value for key in _UPPER_KEYS))


def _is_combined_ranges(value):
    if isinstance(value, dict):
        return (any(k in value for k in ('gain', 'g', 'gain_range',
                                         'g_range'))
                and any(k in value for k in ('offset', 'b', 'offset_range',
                                             'b_range', 'bias')))
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and _is_interval_like(value[0])
            and _is_interval_like(value[1]))


def _split_ranges(value):
    if isinstance(value, dict):
        gain = None
        for key in ('gain_range', 'g_range', 'gain', 'g'):
            if key in value:
                gain = value[key]
                break
        offset = None
        for key in ('offset_range', 'b_range', 'offset', 'bias_range',
                    'bias', 'b'):
            if key in value:
                offset = value[key]
                break
        return gain, offset
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value[0], value[1]
    _error('parameter ranges must contain gain and offset intervals')


def _looks_like_requests(value):
    return (isinstance(value, list)
            and (not value or isinstance(value[0], dict)))


def _resolve_call(store_dir, manifest, references, gain_range, offset_range,
                  requests, kwargs):
    """支持位置式调用、五参合并区间、单 payload 与常见关键字别名。"""
    # calibration_batch({...完整 payload...})。
    payload = None
    if isinstance(store_dir, dict) and manifest is None:
        payload = store_dir
        store_dir = payload.get('store_dir', payload.get('store'))

    # “一对增益和偏置区间”作为第四参的形式：
    # f(store, manifest, refs, [gain_interval, offset_interval], requests?)
    if _is_combined_ranges(gain_range):
        if requests is None and _looks_like_requests(offset_range):
            requests = offset_range
            offset_range = None
        combined_gain, combined_offset = _split_ranges(gain_range)
        gain_range = combined_gain
        if offset_range is None:
            offset_range = combined_offset

    # calibration_batch(store_dir, payload)。
    if payload is None and isinstance(manifest, dict) and (
            'manifest' in manifest or 'dataset_manifest' in manifest
            or 'references' in manifest or 'reference_table' in manifest
            or 'requests' in manifest or 'gain_range' in manifest
            or 'ranges' in manifest or 'parameter_ranges' in manifest):
        payload = manifest
        manifest = None

    aliases = {
        'manifest': ('dataset_manifest', 'build_manifest'),
        'references': ('reference_table', 'reference_ranges', 'ref_table',
                       'reference_intervals', 'ref_ranges'),
        'gain_range': ('g_range', 'gain'),
        'offset_range': ('b_range', 'offset', 'bias_range'),
        'requests': ('downstream_requests', 'queries', 'items'),
    }
    payload = payload if isinstance(payload, dict) else {}

    def resolve(slot, positional):
        if slot in kwargs:
            return kwargs.pop(slot)
        for alias in aliases[slot]:
            if alias in kwargs:
                return kwargs.pop(alias)
        if positional is not None:
            return positional
        if slot in payload:
            return payload[slot]
        for alias in aliases[slot]:
            if alias in payload:
                return payload[alias]
        return None

    manifest = resolve('manifest', manifest)
    references = resolve('references', references)
    gain_range = resolve('gain_range', gain_range)
    offset_range = resolve('offset_range', offset_range)
    requests = resolve('requests', requests)

    combined = None
    for key in ('ranges', 'parameter_ranges', 'calibration_ranges',
                'parameter_bounds', 'bounds'):
        if key in kwargs:
            combined = kwargs.pop(key)
            break
    if combined is None:
        combined = payload.get(
            'ranges', payload.get('parameter_ranges',
                                 payload.get('bounds')))
    if combined is not None:
        combined_gain, combined_offset = _split_ranges(combined)
        if gain_range is None:
            gain_range = combined_gain
        if offset_range is None:
            offset_range = combined_offset

    if kwargs:
        raise CalibrationInputError(
            'unrecognized keyword arguments: %s' % ', '.join(sorted(kwargs)))
    return (store_dir, manifest, references, gain_range, offset_range,
            requests)


# --------------------------------------------------------------------------
# 公共接口
# --------------------------------------------------------------------------

def calibration_batch(store_dir, manifest=None, references=None,
                      gain_range=None, offset_range=None, requests=None,
                      **kwargs):
    """运行一整轮标定可行集、资格复验与盲测交付。

    典型调用::

        result = calibration_batch(
            store_dir, manifest, reference_table,
            ['0.97', '1.01'], ['-1000', '1000'], requests)

    也支持把全部资料放进一个 payload 对象作为第二个位置参数::

        result = calibration_batch(store_dir, {
            'manifest': manifest,
            'references': reference_table,
            'gain_range': ['0.97', '1.01'],
            'offset_range': ['-1000', '1000'],
            'requests': requests,
        })

    输入不合法抛 :class:`CalibrationInputError`（也是
    :class:`DatasetInputError` / ``ValueError``）；清单合法但无法精确拆分时
    原样抛 :class:`HoldoutInfeasible`，不降级、不交付部分结果。
    """
    (store_dir, manifest, references, gain_range, offset_range,
     requests) = _resolve_call(
        store_dir, manifest, references, gain_range, offset_range, requests,
        kwargs)

    # —— 先完整解析本轮新输入（不触碰业务裁决），保证输入错误优先。 ——
    if not isinstance(store_dir, (str, bytes, os.PathLike)):
        _error('store_dir must be a filesystem path')
    if manifest is None:
        _error('dataset manifest is required')
    parsed_references = parse_references(references)
    if gain_range is None or offset_range is None:
        _error('gain_range and offset_range are required')
    gain = _parse_parameter_range(gain_range, 'gain',
                                  require_positive=True)
    offset = _parse_parameter_range(offset_range, 'offset')
    parsed_requests = parse_requests(requests)

    # —— 现有数据集契约：未知版本/坏清单 -> DatasetInputError；合法但无法
    #    拆分 -> HoldoutInfeasible（独立分类，继续原样向上抛）。把数据集层
    #    输入错误统一为本轮 CalibrationInputError（它仍是 DatasetInputError
    #    与 ValueError），但绝不捕获/转换 HoldoutInfeasible。 ——
    try:
        dataset = build_datasets(store_dir, manifest)
    except DatasetInputError as exc:
        if isinstance(exc, CalibrationInputError):
            raise
        raise CalibrationInputError(str(exc)) from exc

    fit_rows, holdout_rows, actual_keys = _attach_rows(
        dataset, parsed_references)
    _require_reference_coverage(parsed_references, actual_keys)

    # —— 拟合可行集与整批状态。 ——
    if not fit_rows:
        status = STATUS_FIT_EMPTY
        space = None
    else:
        candidate = FeasibleSpace(fit_rows, parsed_references, gain, offset)
        if candidate.empty:
            status = STATUS_FIT_INFEASIBLE
            space = None
        else:
            status = STATUS_FEASIBLE
            space = candidate

    qualifications = {
        condition: _qualification(condition, holdout_rows, space, status)
        for condition in CONDITIONS
    }

    items = [_request_item(parsed, qualifications, space)
             for parsed in parsed_requests]
    # 按请求 ID 确定性排序（ID 唯一）。
    items.sort(key=lambda item: item['request_id'])
    items_by_id = {item['request_id']: item for item in items}

    qualification_list = [qualifications[c] for c in CONDITIONS]
    witnesses_flat = []
    for qualification in qualification_list:
        for witness in qualification['witnesses']:
            witnesses_flat.append((qualification['condition'], witness))
    witnesses_flat.sort(key=lambda pair: (pair[0], tuple(pair[1]['key'])))

    return {
        'status': status,
        'batch_status': status,
        'status_text': _BATCH_STATUS_ZH[status],
        'feasible': status == STATUS_FEASIBLE,
        'versions': dataset['versions'],
        'fit_count': len(fit_rows),
        'holdout_count': len(holdout_rows),
        'counts': dataset['holdout']['counts'],
        'parameter_ranges': {
            'gain': [_s(gain[0]), _s(gain[1])],
            'offset': [_s(offset[0]), _s(offset[1])],
        },
        'qualifications': qualifications,
        'qualification_list': qualification_list,
        'witnesses': [witness for _cond, witness in witnesses_flat],
        'requests': items,
        'items': items,
        'items_by_id': items_by_id,
        'deliveries': items_by_id,
    }


# 面向不同调用习惯的函数别名（同一实现，不改变裁决）。
evaluate_batch = calibration_batch
run_batch = calibration_batch
run_calibration = calibration_batch
process_batch = calibration_batch
qualify_and_deliver = calibration_batch
qualify = calibration_batch
deliver = calibration_batch
calibrate = calibration_batch
evaluate = calibration_batch
deliver_blind = calibration_batch
