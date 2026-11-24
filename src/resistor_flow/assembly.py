"""装配工位：用可移交的模块数据包求装配端口响应并逐件筛选。

装配输入只有模块数据包、实例清单（每个实例选择型号+修订并把每个端口绑定到
装配网名）、本批固定激励网名集合与逐条激励电位表。内部节点不在模块之间相连，
模块只在端口处与装配网相接；两个端口属于同一实例不构成连通。

对每条激励：

1. 驱动网电位由激励固定；其余网按每个网名下全部端口净流入电流代数和为零
   求唯一电位（每个电气连通分量至少有一个驱动网，否则整批输入错误）。
2. 由包的端口导纳矩阵 ``I = Y V`` 得到各实例各端口的净流入电流（正号表示
   流入该实例）。
3. 用包内逐额定件的压降线性组合与实际端口电位精确核算功率；严格大于额定
   值才拒绝，等于可用，无额定件不参与筛选。

通过样本的数据集只含通过样本的网电位与逐实例端口电流；被拒样本清单逐条
列出全部超额件，不附可直接执行的端口样本。两份结果都带同一份装配清单。
全程 ``fractions.Fraction`` 精确计算，数值输出为整数或 ``n/d`` 字符串。

完整约定见 ``docs/assembly.md``。
"""

from fractions import Fraction

from . import checks
from .modules import (
    MAX_PORTS,
    ModulePackageError,
    PACKAGE_FORMAT,
    rational,
)

# 业务上限（模块级常量，测试可 monkeypatch 缩小后验证边界）。
MAX_INSTANCES = 64
MAX_NETS = 160
MAX_EXCITATIONS = 4_096

_PACKAGE_FIELDS = ('format', 'model', 'revision', 'ports', 'components',
                   'admittance', 'power_checks')
_CHECK_FIELDS = ('id', 'rated_watts', 'conductance', 'drop')
_INSTANCE_FIELDS = ('instance', 'model', 'revision', 'bindings')
_EXCITATION_FIELDS = ('sample_id', 'voltages')


class AssemblyError(ValueError):
    """装配批处理失败的基类（始终是 ``ValueError``）。"""


class AssemblyInputError(AssemblyError):
    """包、实例、接线或激励不合法；不会返回任何部分结果。"""


def _error(message):
    raise AssemblyInputError(message)


def _ident(value, what):
    try:
        return checks.ident(value)
    except (ValueError, TypeError):
        _error('invalid %s' % what)


def _rational(value, what):
    try:
        return rational(value, what)
    except (ModulePackageError, ValueError, TypeError):
        _error('%s must be an exact rational string' % what)


# --------------------------------------------------------------------------
# 包消费端校验
# --------------------------------------------------------------------------

def _parse_package(package):
    if not isinstance(package, dict):
        _error('module package must be an object')
    try:
        checks.fields(package, _PACKAGE_FIELDS)
    except ValueError as exc:
        _error('malformed module package: %s' % exc)
    if package['format'] != PACKAGE_FORMAT:
        _error('unsupported module package format')
    model = _ident(package['model'], 'package model')
    revision = _ident(package['revision'], 'package revision')

    port_items = checks.array(package['ports'], 1, MAX_PORTS)
    ports = []
    for item in port_items:
        port = _ident(item, 'package port id')
        if port in ports:
            _error('duplicate package port id')
        ports.append(port)
    position = {port: index for index, port in enumerate(ports)}
    p = len(ports)

    components = package['components']
    if not isinstance(components, list) or not 1 <= len(components) <= p:
        _error('components must be a non-empty list of port groups')
    covered = set()
    canonical_components = []
    for group in components:
        if not isinstance(group, list) or not 1 <= len(group) <= p:
            _error('invalid package component')
        members = []
        for item in group:
            port = _ident(item, 'package component port')
            if port not in position or port in covered:
                _error('package components must partition ports exactly once')
            covered.add(port)
            members.append(position[port])
        canonical_components.append(sorted(members))
    if covered != set(ports):
        _error('package components must cover every port')
    canonical_components.sort(key=lambda members: members[0])

    rows = package['admittance']
    if not isinstance(rows, list) or len(rows) != p:
        _error('admittance must have one row per port')
    admittance = []
    for row in rows:
        if not isinstance(row, list) or len(row) != p:
            _error('admittance must be a square matrix over ports')
        admittance.append([_rational(value, 'admittance entry')
                           for value in row])

    raw_checks = package['power_checks']
    if not isinstance(raw_checks, list):
        _error('power_checks must be an explicit list (empty means no rated parts)')
    power_checks = []
    seen_ids = set()
    for item in raw_checks:
        if not isinstance(item, dict):
            _error('power check must be an object')
        try:
            checks.fields(item, _CHECK_FIELDS)
        except ValueError as exc:
            _error('malformed power check: %s' % exc)
        resistor_id = _ident(item['id'], 'power check resistor id')
        if resistor_id in seen_ids:
            _error('duplicate power check resistor id')
        seen_ids.add(resistor_id)
        rating = _rational(item['rated_watts'], 'rated_watts')
        if rating <= 0:
            _error('rated_watts must be positive')
        conductance = _rational(item['conductance'], 'conductance')
        if conductance <= 0:
            _error('conductance must be positive')
        drop_items = item['drop']
        if not isinstance(drop_items, list) or len(drop_items) > p:
            _error('power check drop must be a list of port coefficients')
        drop = {}
        for pair in drop_items:
            if not isinstance(pair, list) or len(pair) != 2:
                _error('drop entries must be [port_index, coefficient]')
            index, coefficient = pair
            if type(index) is not int or not 0 <= index < p:
                _error('drop port index out of range')
            if index in drop:
                _error('duplicate drop port index')
            value = _rational(coefficient, 'drop coefficient')
            if value == 0:
                _error('drop coefficient must be non-zero')
            drop[index] = value
        power_checks.append({
            'id': resistor_id,
            'rating': rating,
            'rated_watts': str(rating),
            'conductance': conductance,
            'drop': sorted(drop.items()),
        })
    power_checks.sort(key=lambda entry: entry['id'])

    return {
        'model': model,
        'revision': revision,
        'ports': ports,
        'components': [[ports[index] for index in members]
                       for members in canonical_components],
        'admittance': admittance,
        'power_checks': power_checks,
    }


# --------------------------------------------------------------------------
# 实例、网与激励校验
# --------------------------------------------------------------------------

class _Instance:
    __slots__ = ('instance', 'model', 'revision', 'package', 'bindings',
                 'nets')

    def __init__(self, instance, model, revision, package, bindings):
        self.instance = instance
        self.model = model
        self.revision = revision
        self.package = package
        self.bindings = bindings      # port -> net
        self.nets = [bindings[port] for port in package['ports']]


def _parse_instances(instances, packages):
    items = checks.array(instances, 1, MAX_INSTANCES)
    parsed = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            _error('instance must be an object')
        try:
            checks.fields(item, _INSTANCE_FIELDS)
        except ValueError as exc:
            _error('malformed instance: %s' % exc)
        instance_id = _ident(item['instance'], 'instance id')
        if instance_id in seen:
            _error('duplicate instance id')
        seen.add(instance_id)
        model = _ident(item['model'], 'instance model')
        revision = _ident(item['revision'], 'instance revision')
        package = packages.get((model, revision))
        if package is None:
            _error('instance selects a model/revision absent from packages')
        raw = item['bindings']
        if not isinstance(raw, dict):
            _error('bindings must be an object mapping port to net')
        if set(raw) != set(package['ports']):
            _error('every package port must be bound to exactly one net')
        bindings = {}
        for port in package['ports']:
            bindings[port] = _ident(raw[port], 'bound net name')
        parsed.append(_Instance(instance_id, model, revision, package,
                                bindings))
    parsed.sort(key=lambda entry: entry.instance)
    return parsed


def _parse_packages(packages):
    if not isinstance(packages, list) or not packages:
        _error('packages must be a non-empty list of module packages')
    items = packages
    index = {}
    for item in items:
        package = _parse_package(item)
        key = (package['model'], package['revision'])
        if key in index:
            _error('duplicate model/revision in package list')
        index[key] = package
    return index


def _parse_driven(driven_nets, assembly_nets):
    items = checks.array(driven_nets, 0, MAX_NETS)
    driven = []
    for item in items:
        net = _ident(item, 'driven net name')
        if net in driven:
            _error('duplicate driven net name')
        if net not in assembly_nets:
            _error('driven net is not part of the assembly')
        driven.append(net)
    driven.sort()  # 驱动网是集合：输入次序不影响任何结果
    return driven


def _parse_excitations(excitations, driven):
    items = checks.array(excitations, 0, MAX_EXCITATIONS)
    required = set(driven)
    parsed = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            _error('excitation must be an object')
        try:
            checks.fields(item, _EXCITATION_FIELDS)
        except ValueError as exc:
            _error('malformed excitation: %s' % exc)
        sample_id = _ident(item['sample_id'], 'sample id')
        if sample_id in seen:
            _error('duplicate sample id')
        seen.add(sample_id)
        table = item['voltages']
        if not isinstance(table, dict):
            _error('excitation voltages must be an object')
        if set(table) != required:
            _error('excitation must give exactly the driven net potentials')
        values = {}
        for net in driven:
            try:
                values[net] = checks.number(table[net])
            except (ValueError, TypeError):
                _error('excitation potential must be a bounded decimal string')
        parsed.append((sample_id, [values[net] for net in driven]))
    parsed.sort(key=lambda entry: entry[0])
    return parsed


# --------------------------------------------------------------------------
# 装配连通与端口响应
# --------------------------------------------------------------------------

class _DrivenLinear:
    """驱动电位向量上的线性映射 ``sum(coef * drives[index])``。

    电导与派生系数都是精确有理数；求值是每次样本的主循环，因此把常见情形
    （单个驱动网、系数为有理数）的循环压到最少。``entries`` 为空表示恒零。
    """

    __slots__ = ('entries', 'single', 'integer')

    def __init__(self, entries):
        self.entries = [(index, value) for index, value in entries if value]
        if len(self.entries) == 1:
            index, value = self.entries[0]
            self.single = (index, value)
            self.integer = value.denominator == 1
        else:
            self.single = None
            self.integer = False

    def evaluate(self, drives):
        if self.single is not None:
            index, value = self.single
            if self.integer:
                return drives[index] * value
            return value * drives[index]
        total = Fraction(0)
        for index, value in self.entries:
            total += value * drives[index]
        return total


class _UnionFind:
    def __init__(self, values):
        self.parent = {value: value for value in values}

    def find(self, value):
        root = value
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[value] != root:
            self.parent[value], value = root, self.parent[value]
        return root

    def union(self, left, right):
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _check_connected(instances, nets, driven):
    """每个电气连通分量至少有一个驱动网。"""
    forest = _UnionFind(nets)
    for entry in instances:
        for group in entry.package['components']:
            anchor = entry.bindings[group[0]]
            for port in group[1:]:
                forest.union(anchor, entry.bindings[port])
    driven_set = set(driven)
    components = {}
    for net in nets:
        components.setdefault(forest.find(net), set()).add(net)
    for members in components.values():
        if not (members & driven_set):
            _error('floating assembly component without a driven net')


def _solve_response(instances, nets, driven):
    """求未知网电位对驱动网电位的精确线性映射。

    返回 ``{net: _DrivenLinear}``，驱动网为单位映射。
    """
    driven_index = {net: index for index, net in enumerate(driven)}
    unknown = [net for net in nets if net not in driven_index]
    unknown_index = {net: index for index, net in enumerate(unknown)}
    n = len(unknown)
    d = len(driven)

    if n:
        matrix = [[Fraction(0) for _ in range(n)] for _ in range(n)]
        rhs = [[Fraction(0) for _ in range(d)] for _ in range(n)]
        for entry in instances:
            package = entry.package
            y = package['admittance']
            port_nets = entry.nets
            p = len(package['ports'])
            for q in range(p):
                row = unknown_index.get(port_nets[q])
                if row is None:
                    continue
                y_row = y[q]
                for j in range(p):
                    value = y_row[j]
                    if value == 0:
                        continue
                    column = unknown_index.get(port_nets[j])
                    if column is not None:
                        matrix[row][column] += value
                    else:
                        rhs[row][driven_index[port_nets[j]]] -= value
        solution = _gauss_jordan(matrix, rhs)
        if solution is None:
            _error('singular assembly system (floating component)')
        rows = {}
        for net, row in zip(unknown, solution):
            rows[net] = _DrivenLinear(
                (index, value) for index, value in enumerate(row) if value)
    else:
        rows = {}
    for net, index in driven_index.items():
        rows[net] = _DrivenLinear([(index, Fraction(1))])
    return rows


def _gauss_jordan(matrix, rhs):
    """精确求解 ``A X = B``；``A`` 奇异返回 ``None``。"""
    n = len(matrix)
    width = len(rhs[0]) if rhs else 0
    augmented = [list(matrix[i]) + list(rhs[i]) for i in range(n)]
    for col in range(n):
        pivot = next((row for row in range(col, n)
                      if augmented[row][col] != 0), None)
        if pivot is None:
            return None
        if pivot != col:
            augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        scale = augmented[col][col]
        if scale != 1:
            augmented[col] = [value / scale for value in augmented[col]]
        for row in range(n):
            if row == col:
                continue
            factor = augmented[row][col]
            if factor:
                augmented[row] = [value - factor * other for value, other
                                  in zip(augmented[row], augmented[col])]
    return [row[n:n + width] for row in augmented]


def _combine(terms):
    """把若干 ``(coefficient, _DrivenLinear)`` 精确合并成一个映射。"""
    accumulator = {}
    for coefficient, expression in terms:
        for index, value in expression.entries:
            accumulator[index] = accumulator.get(index, Fraction(0)) \
                + coefficient * value
    return _DrivenLinear(sorted(accumulator.items()))


def _port_currents(instances, net_rows):
    """每个实例每个端口的电流对驱动网电位的线性映射。"""
    slots = []
    for entry in instances:
        package = entry.package
        y = package['admittance']
        for q, port in enumerate(package['ports']):
            y_row = y[q]
            expression = _combine(
                (y_row[j], net_rows[entry.nets[j]])
                for j in range(len(package['ports'])) if y_row[j])
            slots.append({
                'instance': entry.instance,
                'port': port,
                'expression': expression,
            })
    return slots


def _power_checks(instances, net_rows):
    """每个实例每个额定件的压降线性映射与电导。"""
    checks_out = []
    for entry in instances:
        package = entry.package
        ports = package['ports']
        for check in package['power_checks']:
            expression = _combine(
                (coefficient, net_rows[entry.bindings[ports[position]]])
                for position, coefficient in check['drop'])
            checks_out.append({
                'instance': entry.instance,
                'model': entry.model,
                'revision': entry.revision,
                'resistor_id': check['id'],
                'conductance': check['conductance'],
                'rating': check['rating'],
                'rated_watts': check['rated_watts'],
                'expression': expression,
            })
    checks_out.sort(key=lambda item: (item['instance'], item['resistor_id']))
    return checks_out


def _unique_expressions(expressions):
    """按系数签名去重；返回 ``(unique, uids)``，相同映射只求值一次。"""
    index = {}
    unique = []
    uids = []
    for expression in expressions:
        signature = tuple(expression.entries)
        uid = index.get(signature)
        if uid is None:
            uid = len(unique)
            index[signature] = uid
            unique.append(expression)
        uids.append(uid)
    return unique, uids


# --------------------------------------------------------------------------
# 装配批处理
# --------------------------------------------------------------------------

def assemble_batch(packages, instances, driven_nets, excitations):
    """装配工位：由模块包与接线求每条激励的端口响应并逐件筛选。

    ``packages`` 为模块数据包列表（型号+修订唯一）；``instances`` 每个元素
    为 ``{'instance', 'model', 'revision', 'bindings'}``，``bindings`` 把
    该包每个端口映射到恰好一个装配网名；``driven_nets`` 为本批固定激励网名
    集合；``excitations`` 每个元素为 ``{'sample_id', 'voltages'}``，
    ``voltages`` 恰好覆盖 ``driven_nets``。

    返回 ``{'accepted': {...}, 'rejected': {...}}``：两份结果都带装配清单，
    通过样本带网电位与按实例/端口区分的电流，被拒样本只列全部超额件。
    """
    try:
        return _assemble(packages, instances, driven_nets, excitations)
    except AssemblyInputError:
        raise
    except ValueError as exc:  # checks.* 抛出的普通校验错误
        raise AssemblyInputError(str(exc)) from exc


def _assemble(packages, instances, driven_nets, excitations):
    package_index = _parse_packages(packages)
    parsed_instances = _parse_instances(instances, package_index)

    nets = sorted({net for entry in parsed_instances for net in entry.nets})
    if len(nets) > MAX_NETS:
        _error('assembly exceeds %d nets' % MAX_NETS)
    driven = _parse_driven(driven_nets, set(nets))
    parsed_excitations = _parse_excitations(excitations, driven)

    _check_connected(parsed_instances, nets, driven)

    net_rows = _solve_response(parsed_instances, nets, driven)
    current_slots = _port_currents(parsed_instances, net_rows)
    power_checks = _power_checks(parsed_instances, net_rows)

    # 所有线性映射一次性去重：网电位、端口电流、额定件压降共用求值。
    expressions = [net_rows[net] for net in nets]
    expressions += [slot['expression'] for slot in current_slots]
    expressions += [check['expression'] for check in power_checks]
    unique, uids = _unique_expressions(expressions)
    net_uids = uids[:len(nets)]
    slot_uids = uids[len(nets):len(nets) + len(current_slots)]
    check_uids = uids[len(nets) + len(current_slots):]

    # 功率函数也可去重：相同（压降映射，电导）只需算一次。
    power_index = {}
    power_funcs = []
    check_power = []
    for check, uid in zip(power_checks, check_uids):
        key = (uid, check['conductance'])
        position = power_index.get(key)
        if position is None:
            position = len(power_funcs)
            power_index[key] = position
            power_funcs.append(key)
        check_power.append(position)

    # 同一份装配清单用于两份结果：实例到型号/修订及网绑定，网名确定性排序。
    assembly = {
        'instances': [{
            'instance': entry.instance,
            'model': entry.model,
            'revision': entry.revision,
            'bindings': [{'port': port, 'net': entry.bindings[port]}
                         for port in entry.package['ports']],
        } for entry in parsed_instances],
        'nets': list(nets),
        'driven_nets': list(driven),
    }

    evaluate = [expression.evaluate for expression in unique]
    accepted = []
    rejected = []
    for sample_id, drives in parsed_excitations:
        values = [evaluate[index](drives) for index in range(len(unique))]
        sample_powers = [conductance * values[uid] * values[uid]
                         for uid, conductance in power_funcs]
        over = []
        for position, check in enumerate(power_checks):
            watts = sample_powers[check_power[position]]
            if watts > check['rating']:
                over.append({
                    'instance': check['instance'],
                    'model': check['model'],
                    'revision': check['revision'],
                    'resistor_id': check['resistor_id'],
                    'watts': str(watts),
                    'rated_watts': check['rated_watts'],
                })
        if over:
            rejected.append({'sample_id': sample_id,
                             'over_rating': over})
        else:
            accepted.append({
                'sample_id': sample_id,
                'voltages': {net: str(values[uid])
                             for net, uid in zip(nets, net_uids)},
                'currents': _current_map(current_slots, values, slot_uids),
            })

    return {
        'accepted': {
            'assembly': _copy_assembly(assembly),
            'sample_count': len(accepted),
            'samples': accepted,
        },
        'rejected': {
            'assembly': _copy_assembly(assembly),
            'sample_count': len(rejected),
            'samples': rejected,
        },
        'summary': {
            'accepted': len(accepted),
            'rejected': len(rejected),
            'power_parts': len(power_checks),
        },
    }


def _current_map(slots, values, uids):
    """按实例、端口分组的电流表；实例与端口按 ID 确定性排序。"""
    grouped = {}
    for slot, uid in zip(slots, uids):
        grouped.setdefault(slot['instance'], {})[slot['port']] = \
            str(values[uid])
    return {instance: {port: grouped[instance][port]
                       for port in sorted(grouped[instance])}
            for instance in sorted(grouped)}


def _copy_assembly(assembly):
    return {
        'instances': [{
            'instance': entry['instance'],
            'model': entry['model'],
            'revision': entry['revision'],
            'bindings': [dict(binding) for binding in entry['bindings']],
        } for entry in assembly['instances']],
        'nets': list(assembly['nets']),
        'driven_nets': list(assembly['driven_nets']),
    }


# 兼容别名：装配步骤。
run_assembly = assemble_batch
assembly_batch = assemble_batch
