# 离线标定可行集、资格复验与盲测交付

`resistor_flow.calibration.calibration_batch` 在**同一轮只读离线批处理**中
完成任务书要求的三件事，不操作设备、不连外部服务，也不改动发布、求解、数据
集构建、复核与交接的任何既有接口：

1. 调用现有 `resistor_flow.datasets.build_datasets` 取得 `fit.rows` 与
   `holdout.rows`，完整沿用样件/校准批次成组、不拆组、精确配额与多解字典序
   选择规则；不重新抽样，调用方不能指定某行进入哪一侧。读数 `x` 只用每行的
   标准观测 `volts`（归档原样精确有理数字符串，可能是整数或 `n/d`），不用裸
   原始读数、冲突键或求解值。
2. 用拟合行参考区间求 `y = g*x + b` 的**完整精确可行参数集**，再在整个可行
   集上逐见证求换算极值，裁决 L/H 资格与覆盖范围。
3. 按资格与覆盖范围处理最多 36,000 项盲测请求，交付绝对量或“第一减第二”的
   **同参耦合**差值闭区间。

全程 `fractions.Fraction` 精确计算，等于边界算满足；所有输出数值用
`str(Fraction)` 的整数或 `n/d` 字符串。

## Python 调用

```python
from resistor_flow.calibration import (
    calibration_batch, CalibrationInputError,
)
from resistor_flow.datasets import HoldoutInfeasible

result = calibration_batch(
    store_dir,          # measurements.publish 使用的归档目录
    manifest,           # 现有 build_datasets 清单（原样传入）
    references,         # 参考区间表，键 (run_id, tick, resistor_id)
    ['1', '2'],         # 增益 g 的允许区间，两端必须严格为正
    ['0', '10'],        # 偏置 b 的允许区间
    requests,           # 下游请求数组；可为 [] 或 None
)
```

也支持把全部资料放进一个对象：
`calibration_batch(store_dir, payload)`，其中 `payload` 含
`manifest / references / gain_range / offset_range / requests`；或
`calibration_batch(payload)`（payload 另含 `store_dir`）。常见关键字别名
`reference_table=`、`g_range=`、`b_range=`、`parameter_ranges={'g':..,'b':..}`、
`queries=` 等也接受。模块别名 `resistor_flow.qualification.qualify` 与
`resistor_flow.delivery.deliver` 是同一实现。

### 参考区间表

必须**恰好覆盖** `fit.rows ∪ holdout.rows` 的每个行键，缺失、多余、重复键都
是输入错误。规范写法是对象数组：

```python
references = [
    {'run_id': 'R1', 'tick': 1, 'resistor_id': 'r1',
     'lower': '2', 'upper': '8'},          # 上下界可相等
]
```

也接受 `{(run_id, tick, resistor_id): ['2', '8']}` 三元组键映射、嵌套
`{run_id: {tick: {resistor_id: [lo, hi]}}}`，以及
`[key_triple, lo, hi]` 等数组写法。所有界用现有有界十进制字符串解析
（最多 9 位整数、6 位小数），要求 `lower <= upper`。

### 下游请求

```python
requests = [
    {'request_id': 'q1',
     'reading':  {'condition': 'L', 'x': '3'},   # 单个绝对量
     'max_width': '8'},                           # 非负容许宽度（闭，<=）
    {'request_id': 'q2',
     'first':  {'condition': 'L', 'x': '3'},
     'second': {'condition': 'H', 'x': '2'},      # 第一减第二
     'max_width': '1'},
]
```

`x` 与 `max_width` 都是有界十进制字符串；`max_width` 不得为负。`request_id`
沿用本仓 ID 规则且同批不得重复；批量上限 `MAX_REQUESTS = 36000`，空批合法
（仍返回资格）。`readings=[...]`（1 或 2 项）、`a/b`、`left/right` 等写法也
接受。

## 整批状态与失败分类

**异常（不交付任何部分结果）：**

| 情形 | 类型 |
| --- | --- |
| 结构/类型/ID/tick/数值非法，请求 ID 重复，宽度为负，增益端点非严格正，区间下界大于上界，参考键缺失/多余/重复，请求超过 36,000 等 | `CalibrationInputError`（同时是 `DatasetInputError`、`ValueError`） |
| 清单引用未知历史版本 | 现有 `DatasetInputError`（由 `build_datasets` 抛出，不回落当前版） |
| 清单合法但无法精确铺满 L/H 配额 | 现有 `HoldoutInfeasible`（`DatasetError` 子类，**不是** `DatasetInputError`），保持独立分类 |

接口先完整解析本轮全部输入（请求、区间、参考表语法），再调用
`build_datasets` 与业务裁决，因此例如“清单无法拆分 + 请求 ID 重复”会先报输入
错误。

**正常返回中的整批 `status`（不是异常）：**

- `feasible`：有拟合行且存在可行参数；
- `fit_empty`：没有拟合行（配额取走全部候选）；
- `fit_infeasible`：有拟合行但允许矩形内没有任何参数对同时满足它们。

后两者都不签发任何条件资格，也不交付任何换算值；每个请求因条件未签发而标为
`condition_not_qualified`。三种状态另有中文 `status_text`。

## 返回结构（均为 JSON 原生类型）

```python
{
  'status': 'feasible' | 'fit_empty' | 'fit_infeasible',
  'status_text': ..., 'feasible': bool,
  'versions': [{'run_id': ..., 'version': ...}],   # 历史版本清单，按 run_id
  'parameter_ranges': {'gain': [g0, g1], 'offset': [b0, b1]},
  'qualifications': {'L': <qual>, 'H': <qual>},
  'qualification_list': [<qual L>, <qual H>],
  'witnesses': [ ... ],        # 扁平、按 (条件, 行键) 排序的全部见证
  'requests': [ <item>, ... ], # 按 request_id 排序
  'items': ..., 'items_by_id': {request_id: <item>},
}
```

每个条件资格 `<qual>`：

```python
{
  'condition': 'L' | 'H', 'issued': bool,
  'status': 'issued' | 'not_issued',
  'coverage': [x_min, x_max] | None,   # 仅 issued 时非 None
  'reference_passed': bool | None,     # 所有见证完整区间被参考包含
  'coverage_sufficient': bool,         # 至少两个不同见证 x
  'distinct_x_count': int, 'witness_count': int,
  'failure_reasons': ['reference_not_contained'
                      | 'insufficient_distinct_coverage'
                      | 'fit_empty' | 'fit_infeasible', ...],
  'witnesses': [{
      'key': [run_id, tick, resistor_id], 'version': ...,
      'x': '...', 'reference': [lo, hi],
      'min_value': '...', 'max_value': '...',
      'reference_passed': bool | None, 'passed': ...,
  }, ...],                              # 按行键排序
}
```

参考复验要求见证在**整个可行集**上的完整换算区间 `[min, max]` 被其参考区间闭
包含（`lo <= min` 且 `max <= hi`），仅相交不通过。资格需同时满足“全部见证通
过”和“至少两个不同 x”；两项失败原因可同时出现。覆盖范围是全部见证 `x` 的闭
最小到最大，只表示下游可引用的读数区间，不声称任意插值为真。复验不缩小拟合
集，H 失格不影响 L。

每个请求 `<item>`：

- 交付：`status='delivered'`（`status_text='已交付'`），含
  `lower / upper / range=[lower,upper] / width`（另附 `min_value/max_value`、
  `interval` 同义字段）与 `request_id`。
- 拒付（**不含任何换算极值、宽度或点估计**），按固定优先级：
  1. `condition_not_qualified` / `条件未签发`：任一读数条件未签发；
  2. `out_of_coverage` / `超出覆盖范围`：条件已签发，但任一 `x` 不在其闭覆盖
     区间内（两数相同、差值会抵消也照常检查）；
  3. `insufficient_precision` / `精度不足`：区间宽度 `upper-lower` 大于
     `max_width`（相等算通过）。

拒付不阻塞同批其他项；资格见证审计始终返回。结果只由解析后的资料决定：清单、
参考表或请求条目换序不改变结果，列表分别按条件、行键、请求 ID 确定性排序。

## 为什么差值不会被错误拒付

绝对量 `x*g+b` 在二维凸可行集上的极值沿凸/凹包络链求得；可行集可能是区域、
线段或点。差值在**同一对 `(g,b)`** 下为

```
(x1*g + b) - (x2*g + b) = (x1-x2) * g
```

因此直接在可行 `g` 区间上取端点，而不是把两个绝对区间相减。验收 1 中
`L(x=3) - H(x=2) = g`，可行集上恰为 `[1,2]`（宽度 1，通过上限 1）；若分别求
绝对区间再相减会得到宽得多的区间而误拒。

## 可复现验收

```sh
# 仓库根目录；完整单元/验收测试（含下面的规模子进程验收）
PYTHONPATH=src python3 -m unittest tests.test_calibration -v
# 或按 README 安装后：
python3 -I -m unittest tests.test_calibration -v

# 规格第 3 条的一键可生成离线批处理验收（生成不计时，核对进程在
# `ulimit -v 262144` 下计时，上限 CPU 20s / 地址空间 256MiB）：
python3 acceptance/scale_acceptance.py

# 分步：
python3 acceptance/scale_acceptance.py generate /tmp/q5acc
bash -c 'ulimit -v 262144; exec python3 -I acceptance/scale_acceptance.py check /tmp/q5acc'
```

`acceptance/scale_acceptance.py` 用现有 `publish` 生成 12,004 条标准观测：
前四个行键为 L/H 各两条复验（`x=0,12000`，参考 `[-1000,14000]`，配额各 2，
各自独立组），其余 12,000 条按每 50 行一样件分组、来源批次不跨组，读数
`x=i`、参考 `[i-i²/10⁶, i+1000]`；随后发出 24,000 项 L/H 绝对量（宽度上限
1200）与 12,000 项 `L(i)-H(0)` 差值（上限 480），共 36,000 项，逐项核对：

- 两资格覆盖均为 `[0,12000]`；
- 全部绝对量区间恰为 `[i-i²/10⁶, i+1000]`；
- 全部差值区间恰为 `[0.97i, 1.01i]`。

本机 CPython 3.11 实测核对进程 CPU 约 4 秒、峰值 RSS 约 160 MiB，留有充足余
量；该时限只针对这一负载，不承诺所有合法输入同耗时。
