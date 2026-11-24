"""模块制模：把内部接线表压缩成可移交的端口数据包。

制模工位掌握模块的内部节点与电阻表；装配工位只收到本模块生成的包。包保留
型号、修订、有序端口、各端口的连通归属，并给出两个端口层面的精确派生量：

* 端口导纳矩阵 ``Y``：对任意端口电位向量 ``V``，各端口净流入电流为
  ``I = Y V``（正号表示流入模块）；
* 逐额定件的功率资料：额定电阻 ID、额定功率、派生电导以及该电阻两端压降
  对端口电位的稀疏线性组合，``P = conductance * drop**2``。

内部节点身份、原接线表和源文件地址都不进入包。所有数值为精确有理数，序列化
为整数或 ``n/d`` 字符串；包是纯 JSON 兼容的字典，可跨进程独立使用。

完整约定见 ``docs/assembly.md``。
"""

from fractions import Fraction
import re

from . import checks

# 业务上限（模块级常量，测试可 monkeypatch 缩小后验证边界）。
MAX_NODES = 60
MAX_PORTS = 12
MAX_RESISTORS = 300

PACKAGE_FORMAT = 'resistor-flow/module/1'

# 包内派生量使用的精确有理数字符串：整数或 n/d，分母为正。
_RATIONAL = re.compile(r'-?(?:0|[1-9][0-9]*)(?:/[1-9][0-9]*)?\Z')

_MODULE_FIELDS = ('model', 'revision', 'nodes', 'ports', 'resistors')


class ModulePackageError(ValueError):
    """制模输入或包结构不合法；不会返回部分包。"""


def _error(message):
    raise ModulePackageError(message)


def rational(value, what='rational'):
    """解析包内精确有理数字符串（整数或 ``n/d``）；返回 ``Fraction``。"""
    if not isinstance(value, str) or not _RATIONAL.match(value):
        _error('%s must be an exact rational string (integer or n/d)' % what)
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError):
        _error('%s is not an exact rational' % what)


# --------------------------------------------------------------------------
# 精确线性代数
# --------------------------------------------------------------------------

def solve_columns(matrix, columns):
    """解 ``matrix * X = columns``；``matrix`` 为 n×n，``columns`` 为 n×k。

    使用精确有理数的高斯-约当消元。矩阵奇异返回 ``None``，由调用方决定是
    制模输入错误还是装配浮空错误。
    """
    n = len(matrix)
    if n == 0:
        return [[] for _ in range(len(columns))]
    width = len(columns[0]) if columns else 0
    augmented = [list(matrix[i]) + list(columns[i]) for i in range(n)]
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


def _conductance_matrix(node_order, edges):
    m = len(node_order)
    index = {node: i for i, node in enumerate(node_order)}
    matrix = [[Fraction(0) for _ in range(m)] for _ in range(m)]
    for edge in edges:
        a = index[edge['a']]
        b = index[edge['b']]
        g = Fraction(1) / edge['ohms']
        matrix[a][a] += g
        matrix[b][b] += g
        matrix[a][b] -= g
        matrix[b][a] -= g
    return matrix


def _components(nodes, edges):
    """无向连通分量；返回按最小节点 ID 排序的节点集合列表。"""
    adjacency = {node: set() for node in nodes}
    for edge in edges:
        adjacency[edge['a']].add(edge['b'])
        adjacency[edge['b']].add(edge['a'])
    seen = set()
    result = []
    for node in sorted(nodes):
        if node in seen:
            continue
        stack = [node]
        component = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(adjacency[current] - component)
        seen.update(component)
        result.append(component)
    return result


# --------------------------------------------------------------------------
# 制模
# --------------------------------------------------------------------------

def _parse_module(data):
    if not isinstance(data, dict):
        _error('module must be an object')
    checks.fields(data, _MODULE_FIELDS)
    model = checks.ident(data['model'])
    revision = checks.ident(data['revision'])

    node_items = checks.array(data['nodes'], 1, MAX_NODES)
    nodes = []
    for item in node_items:
        node = checks.ident(item)
        if node in nodes:
            _error('duplicate node id')
        nodes.append(node)

    port_items = checks.array(data['ports'], 1, MAX_PORTS)
    ports = []
    for item in port_items:
        port = checks.ident(item)
        if port in ports:
            _error('duplicate port id')
        if port not in nodes:
            _error('port must be a node')
        ports.append(port)

    resistor_items = checks.array(data['resistors'], 0, MAX_RESISTORS)
    seen = set()
    edges = []
    for item in resistor_items:
        checks.fields(item, ('id', 'a', 'b', 'ohms'), ('rated_watts',))
        key = checks.ident(item['id'])
        a = checks.ident(item['a'])
        b = checks.ident(item['b'])
        ohms = checks.number(item['ohms'])
        if key in seen or a not in nodes or b not in nodes or a == b or ohms <= 0:
            _error('invalid resistor')
        seen.add(key)
        rating = None
        if 'rated_watts' in item:
            rating = checks.number(item['rated_watts'])
            if rating <= 0:
                _error('positive rating required')
        edges.append({'id': key, 'a': a, 'b': b, 'ohms': ohms,
                      'rating': rating})
    return model, revision, nodes, ports, edges


def build_module_package(data):
    """制模工位：校验内部接线并生成可移交的模块数据包。

    输入字段为 ``model``、``revision``、``nodes``（节点 ID 字符串列表）、
    ``ports``（有序端口 ID 列表，端口即节点）与 ``resistors``（沿用本仓
    ``id/a/b/ohms/rated_watts`` 口径）。端口列表次序被保留，其余列表换序不
    改变包内容。
    """
    try:
        return _build_module_package(data)
    except ModulePackageError:
        raise
    except ValueError as exc:  # checks.* 抛出的普通校验错误
        raise ModulePackageError(str(exc)) from exc


def _build_module_package(data):
    model, revision, nodes, ports, edges = _parse_module(data)

    # 每个连通分量（含孤立节点）都必须至少有一个端口。
    component_nodes = _components(nodes, edges)
    port_set = set(ports)
    for component in component_nodes:
        if not (component & port_set):
            _error('every component must have a port')

    # 端口在前、内部节点随后，内部节点按 ID 排序以保证换序不影响结果。
    port_position = {port: i for i, port in enumerate(ports)}
    internal = sorted(node for node in nodes if node not in port_position)
    order = ports + internal
    p = len(ports)
    conductance = _conductance_matrix(order, edges)

    g_pp = [row[:p] for row in conductance[:p]]
    g_pi = [row[p:] for row in conductance[:p]]
    g_ii = [row[p:] for row in conductance[p:]]
    g_ip = [[g_pi[i][j] for i in range(p)] for j in range(len(internal))]
    h = solve_columns(g_ii, g_ip)
    if h is None:  # 每个分量都有端口时不应发生
        _error('singular internal conductance system')

    # Y = G_pp - G_pi * H
    admittance = [[g_pp[i][j]
                   - sum((g_pi[i][t] * h[t][j] for t in range(len(internal))),
                         Fraction(0))
                   for j in range(p)] for i in range(p)]

    # 节点电位对端口电位的系数：端口为单位向量，内部节点为 -H 行。
    node_vectors = {}
    for i, port in enumerate(ports):
        vector = [Fraction(0)] * p
        vector[i] = Fraction(1)
        node_vectors[port] = vector
    for t, node in enumerate(internal):
        node_vectors[node] = [-h[t][j] for j in range(p)]

    power_checks = []
    for edge in edges:
        if edge['rating'] is None:
            continue
        drop = [a - b for a, b in zip(node_vectors[edge['a']],
                                      node_vectors[edge['b']])]
        sparse = [[i, str(value)] for i, value in enumerate(drop) if value]
        power_checks.append({
            'id': edge['id'],
            'rated_watts': str(edge['rating']),
            'conductance': str(Fraction(1) / edge['ohms']),
            'drop': sparse,
        })
    power_checks.sort(key=lambda item: item['id'])

    # 端口连通归属：按最小端口次序排列，分量内部保持端口次序。
    port_index = {port: i for i, port in enumerate(ports)}
    grouped = []
    for component in component_nodes:
        members = sorted((port_index[port] for port in component & port_set))
        grouped.append(members)
    grouped.sort(key=lambda members: members[0])
    components = [[ports[i] for i in members] for members in grouped]

    return {
        'format': PACKAGE_FORMAT,
        'model': model,
        'revision': revision,
        'ports': list(ports),
        'components': components,
        'admittance': [[str(value) for value in row] for row in admittance],
        'power_checks': power_checks,
    }


# 兼容别名：制模步骤。
package_module = build_module_package
pack_module = build_module_package
