# 模块制模与装配批处理

两个可由 Python 调用的处理步骤，把“知道内部接线的制模工位”与“只收到端口
模型的装配工位”分开：

| 工位 | 接口 | 作用 |
| --- | --- | --- |
| 制模 | `resistor_flow.modules.build_module_package(module)` | 校验内部接线，生成可移交、可序列化的模块数据包 |
| 装配 | `resistor_flow.assembly.assemble_batch(packages, instances, driven_nets, excitations)` | 用包与接线求装配端口响应，按逐件额定功率筛选样本 |

两者都只用标准库与精确有理数（`fractions.Fraction`），不导入网络求解的
`nodal`/`service` 入口，不改动既有求解、实测发布、数据集、复核、交接与标定的
接口或行为。装配步骤的输入里没有内部节点、没有原接线表，也没有回调；交付的
包同样不含这些内容。整个链路只处理计算数据，不写回实测归档、不操作设备。

## 制模输入

```python
module = {
    'model': 'M', 'revision': 'B',
    'nodes': ['p', 'c', 'q'],            # 节点 ID，1..60，列表内唯一
    'ports': ['p', 'q'],                 # 有序端口 ID，1..12，端口就是节点
    'resistors': [
        {'id': 'r1', 'a': 'p', 'b': 'c', 'ohms': '2', 'rated_watts': '10'},
        {'id': 'r2', 'a': 'c', 'b': 'q', 'ohms': '4', 'rated_watts': '20'},
    ],
}
package = build_module_package(module)
```

- `id/a/b/ohms/rated_watts` 沿用本仓既有口径：ID 为
  `[A-Za-z0-9_:-]{1,64}`；`ohms` 为有界十进制字符串且必须严格为正；
  `rated_watts` 可不填，填了必须严格为正。
- `nodes`、`ports` 与 `resistors` 各自的 ID 在列表内唯一；每条支路两端必须是
  不同的已知节点；最多 300 个电阻。
- 不输入固定电位：端口电位由装配工位激励，内部节点无注入电流。
- 每个连通分量（含孤立节点）必须至少有一个端口，否则报输入错误。一个模块可
  以有多个互不连通的端口分量，也可以有平行支路。
- 失败抛 `ModulePackageError`（`ValueError` 子类），不返回部分包。

## 包内容

```python
{
  'format': 'resistor-flow/module/1',
  'model': ..., 'revision': ...,
  'ports': [...],                       # 端口次序原样保留
  'components': [[...], ...],           # 端口连通归属，按最小端口次序排列
  'admittance': [[...], ...],           # 端口导纳矩阵 Y（p×p）
  'power_checks': [
    {'id': 'r1', 'rated_watts': '10', 'conductance': '1/2',
     'drop': [[0, '1/3'], [1, '-1/3']]},  # [端口下标, 压降系数]
    ...
  ],
}
```

- `ports` + `components` 保留型号、修订、端口及其连通归属。`components` 是
  端口集合的一个划分，每个分量内部端口按端口列表次序排列，分量之间按最小
  端口下标排序。
- `admittance` 给出端口层面响应：对任意端口电位向量 `V`，各端口净流入电流
  `I = Y V`（本包正号表示流入模块）。
- `power_checks` 只为**有额定值**的电阻给出条目，含电阻 ID、额定值、派生
  电导，以及该电阻两端压降对端口电位的稀疏线性组合：`drop = Σ c_k V_k`，
  `P = conductance * drop**2`。无额定的电阻不出现在包里，但它们的电导已
  汇总进 `admittance`，因此端口电流仍然精确。
- 数值全部是整数或 `n/d` 字符串（`Fraction` 的规范串），可直接
  `json.dump`/`json.load` 跨进程使用。
- 包内**没有**内部节点身份、原接线表、源文件地址或回调；只有端口层面的
  派生量。
- 确定性：`nodes` 与 `resistors` 换序不改变包内容（内部节点按 ID 排序，功率
  条目按电阻 ID 排序）；`ports` 是端口次序的唯一来源，换序会改变矩阵行列与
  分量内的端口次序。
- 两个模块从端口看可以完全相同：只要 `admittance` 一致，任意激励下端口电流
  就一致；内部电阻是否超额由各自的 `power_checks` 决定，可能不同。

## 装配输入

```python
result = assemble_batch(
    packages,       # 模块数据包列表；型号+修订组合在清单内唯一
    instances,      # 1..64 个实例
    ['N0', 'N48'],  # 本批固定激励网名集合（可为空）
    excitations,    # 0..4096 条 {'sample_id', 'voltages'}
)
```

实例：

```python
{'instance': 'I17', 'model': 'CH', 'revision': 'LOW',
 'bindings': {'p': 'N17', 'q': 'N18'}}
```

- 物理实例 ID 在清单内唯一；每个实例显式选择一个包（型号+修订），并把该包
  的**每个**端口绑定到**恰好一个**装配网名。重复引用同一包会产生独立物理
  实例；不同修订不能靠型号或端口电流相同而替用。
- 多个端口可以绑到同一网，包括同一实例的两个端口。内部节点不在模块之间
  相连；两个端口属于同一实例不构成连通。
- 装配网是所有绑定网名的集合，至多 160 个。

激励：

```python
{'sample_id': 's0001', 'voltages': {'N0': '0', 'N48': '0.2'}}
```

- 样本 ID 唯一；`voltages` 必须恰好覆盖 `driven_nets`（遗漏或多余都报错），
  电位沿用本仓有界十进制字符串口径，允许相同电位。
- 驱动网不要求净流入为零；未被驱动的网在同一网名的全部端口之间净流入代数
  和为零。按实际接线，每个电气连通分量至少有一个驱动网，否则整批报
  `AssemblyInputError`（浮空）——不私设零点，也不因两端口同属一实例而推定
  连通。
- 整批先完成全部校验（包括空激励批的装配）再交付；无效引用、遗漏或多余的
  端口/激励网、重复 ID、浮空或非法数值都不会返回部分结果。

## 装配输出

```python
{
  'accepted': {
    'assembly': {...}, 'sample_count': N, 'samples': [
      {'sample_id': ..., 'voltages': {net: '精确电位', ...},
       'currents': {instance: {port: '精确电流', ...}, ...}},
    ]},
  'rejected': {
    'assembly': {...}, 'sample_count': M, 'samples': [
      {'sample_id': ..., 'over_rating': [
        {'instance': ..., 'model': ..., 'revision': ...,
         'resistor_id': ..., 'watts': '精确功率', 'rated_watts': ...}, ...]},
    ]},
  'summary': {'accepted': N, 'rejected': M, 'power_parts': ...},
}
```

- 通过样本的数据集只含通过的样本，绝不混入被拒样本；被拒样本清单逐条列出
  全部超额件，不附可直接执行的端口样本。
- 拒绝一条激励不影响同批其他激励。
- 两份结果都带同一份装配清单 `assembly`：实例到型号/修订及端口到网的绑定、
  排序后的装配网列表与驱动网列表。
- 排序确定性：样本按样本 ID 升序；超额件按实例 ID、电阻 ID 升序；实例按
  实例 ID、端口按端口 ID、网按网名升序。实例清单、包清单、驱动网集合与
  激励录入顺序都不影响结果（驱动网按集合处理）。
- 电流正号表示流入该实例。电压、电流、功率都是整数或 `n/d` 字符串。
- 功率核算用本次装配实际产生的端口电位（由包内 `drop` 与网电位精确求出），
  **严格大于**额定值才拒绝，等于可用，无额定的支路不参与筛选。

## 错误边界

| 情形 | 异常 |
| --- | --- |
| 制模输入结构/ID/端点/阻值/额定值非法，重复节点或电阻，分量缺端口 | `ModulePackageError`（`ValueError` 子类） |
| 包格式错误、`format` 不符、缺 `power_checks` 等必需字段、矩阵非方阵或端口数不符、组件划分不完整、功率资料字段非法 | `AssemblyInputError` |
| 包清单型号+修订重复、实例引用不存在的包、端口绑定遗漏/多余、实例 ID 重复 | `AssemblyInputError` |
| 驱动网不属于装配、驱动网重复、激励网遗漏/多余、样本 ID 重复、非法电位 | `AssemblyInputError` |
| 某电气连通分量没有驱动网（浮空） | `AssemblyInputError` |

`power_checks` 必须是显式列表：空列表表示“无额定件”，与“未交付该字段”
不同——后者是缺必需模型数据，整批拒绝。消费端不要求签名，也不检测数学上
自洽的伪造包；包被信任来自制模工位。

## 自动验收与复现

```sh
# 在本仓根目录
python3 -m pip install -e .        # 或始终设置 PYTHONPATH=src
python3 -m unittest tests.test_assembly -v
python3 acceptance/assembly_acceptance.py
```

`tests/test_assembly.py` 覆盖任务书三条验收：

1. 同一装配位置的两个修订（串联 2 Ω/4 Ω 与交换后的 4 Ω/2 Ω，额定 10 W/
   20 W）：端口导纳与端口电流 `+2/-2` 完全一致；初版 p=12 交付，换修订后
   仅因 `r1=16 W > 10 W` 拒绝，且同批 p=6 仍交付。另在同一测试进程外的
   `-I` 子进程、先移走源模型文件后只用包与装配输入重跑；检查包内不含内部
   节点/接线/源路径；缺失 `power_checks` 时整批报错。
2. 三端口星形（x/y/z 经 1/2/2 Ω 到内部 c，后两件 0.5 W）加一只 1 Ω 二端口
   负载：x=3 V、y/z 同网 n、n 经负载接 0 V，得 n=1 V、电流 1、-1/2、-1/2，
   负载 n 端 1 A，等额定可交付；x=6 V 时只报 ry、rz 各 2 W。另测含两个互不
   连通单端口分量的模块：只驱动一网报浮空，两网并为同一驱动网则不浮空；
   覆盖同包多实例电阻 ID 不串账、非法映射/激励表、零电流、等额定与录入顺序
   稳定交付，以及随机图与直接求解的交叉核对（见测试内联断言）。
3. `acceptance/assembly_acceptance.py` 生成 48 个两端口实例（每模块 12 个
   1 Ω 串联）串成 N0..N48 的冷启动批次，4,096 条激励为 N0=0、N48=i/5 V；
   其中一个实例换成仅一件 1 W 额定值的同电性修订。`check` 在全新进程读取
   已制好的包与批次输入（**准备阶段不算响应**），到核对完两份完整结果为止，
   单核 Linux CPython 3.11 下 CPU ≤ 35 秒、地址空间 ≤ 256 MiB。结果须交付
   i≤2880 的样本（网 Nj 电位 i·j/240、电流幅值 i/2880），i>2880 只报该低限
   额定件功率 `(i/2880)² W`。

分步复现：

```sh
python3 acceptance/assembly_acceptance.py generate /tmp/asmacc
bash -c 'ulimit -v 262144; exec python3 -I acceptance/assembly_acceptance.py check /tmp/asmacc'
```
