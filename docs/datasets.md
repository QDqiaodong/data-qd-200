# 离线拟合/复验数据集构建

从实测归档（`resistor_flow.measurements` 发布的目录）的指定历史版本构造两份
可直接消费的完整数据集：

- `fit`：拟合集，用于估计仪器修正参数；
- `holdout`：独立复验集，用于检验这些参数。

同属一个物理样件、或任意一条来源共用同一仪表校准批次的观测必须同侧，否则
复验会把共有偏差当成可泛化结果。本模块纯标准库、离线、只读归档，不导入
电路求解器（`nodal` / `service`），求解结果不参与生成或筛选数据；也不改变
发布协议与既有求解入口的任何行为。

## 接口

```python
from resistor_flow.datasets import (
    build_datasets, DatasetInputError, HoldoutInfeasible,
)

result = build_datasets(store_dir, manifest)
```

`store_dir` 为 `measurements.publish` 使用的归档目录；`manifest` 为调用方
提供的清单（见下）。成功返回：

```python
{
  'versions': [{'run_id': ..., 'version': ...}, ...],        # 按 run_id 排序
  'fit':     {'counts': {'L': ..., 'H': ...}, 'rows': [...]},
  'holdout': {'counts': {'L': ..., 'H': ...}, 'rows': [...]},
}
```

两份 `rows` 各自按行键 `(run_id, tick, resistor_id)` 升序；二者不重叠、
并集恰为全部候选行，不丢行、不拆组。

## 清单格式

清单是封闭字段集合的对象：

```python
{
  'versions': [{'run_id': 'R1', 'version': 3}, ...],          # 也可写成 [run_id, version]
  'specimens': [
    {'run_id': 'R1', 'tick': 12, 'resistor_id': 'r1',
     'specimen_id': 'SP-7', 'condition': 'L'}, ...
  ],
  'calibration_batches': [
    {'run_id': 'R1', 'station': 'S1', 'row_id': 'row-9', 'batch_id': 'CAL-2'}, ...
  ],
  'holdout_counts': {'L': 120, 'H': 80},
}
```

- `versions`：每个 `run_id` 至多出现一次，`version` 必须是该运行已发布的
  正整数版本（布尔值不算整数）。只取对应版本的**标准观测**作为候选行；
  未绑定读数、冲突键、裸原始文件与相邻版本变更都不补行。
- 候选行键：`(run_id, tick, resistor_id)`；原始来源键：
  `(run_id, station, row_id)`。二者都带运行作用域，不同运行的相同局部 ID
  不会被误认成同一来源。
- `specimens`：为**每个候选行键**补充 `specimen_id` 与 `condition`（`L`/`H`）。
  同一 `specimen_id` 可跨运行、测点与条件。
- `calibration_batches`：为**每个被引用的来源键**补充 `batch_id`（那次读数
  使用的仪表校准批次）。必须覆盖每一条来源，不能只看首条来源，也不能用站点名
  代替批次。
- 两份补充资料必须**恰好**覆盖候选行 / 被引用来源；缺失、重复或多余键都拒绝。
- 样件 ID 与批次 ID 各自是整份清单内的全局命名空间，互不相关。
- `holdout_counts`：`L`、`H` 各自的确切复验行数，均为 1..1,500 的整数。

## 输出行

```python
{
  'run_id': ..., 'tick': ..., 'resistor_id': ..., 'version': ...,  # 归档版本
  'volts': '...',                    # 归档原精确有理数字符串，原样保留
  'condition': 'L' | 'H',
  'specimen_id': ...,
  'sources': [{'run_id': ..., 'station': ..., 'row_id': ..., 'batch_id': ...}, ...],
  'group_id': [run_id, tick, resistor_id],   # 不可拆组标识 = 组内最小行键
}
```

## 成组与选组

- 任意两行同属一个样件，或各自任意一条来源共用同一校准批次，即同组；
  该义务经其他候选行传递，跨运行成立。每行只带自身的完整来源与批次。
- 组标识取组内最小行键，行键按 `run_id`、数值 `tick`、`resistor_id` 比较，
  ID 区分大小写。
- 复验集整组选取，使 `L`、`H` 行数分别精确命中 `holdout_counts`；其余
  候选全部进入拟合集。多解时取**排序后复验组标识序列字典序最小**者
  （优先兑现靠前、且仍能完成两条件配额的组）。
- 结果只由清单解析后的内容决定：清单条目换序不改变输出。

## 失败约定

| 情形 | 异常 | 结果 |
| --- | --- | --- |
| 结构/类型/ID/tick 非法、未知版本、条件非法、补充资料缺失/重复/多余、超限 | `DatasetInputError`（`DatasetError`、`ValueError` 子类） | 不返回任何一侧数据 |
| 清单合法但无法精确铺满两条件槽位 | `HoldoutInfeasible`（`DatasetError` 子类，**不是** `DatasetInputError`） | 不返回部分数据集，也不降级到较近数量 |

规模上限（模块级常量，可测试时缩小）：至多 100 个运行、30,000 条候选行、
90,000 个被引用来源键、分组后至多 3,000 组。

构建全程只读归档：不推进版本、不改动原始读数或历史版本；当清单指定的
历史版本不变时，归档后来推进不影响结果。

## 可复现验收

```sh
# 在本仓根目录
python3 -I -m unittest discover -s tests -v
```

`tests/test_datasets.py` 覆盖：

1. A–F 六行：加来源前选 A、B；给 A 增一条同值来源并复用 C 的批次后，
   A、C、F 传递成组（组标识为 A）退出复验，改选 B、E；用旧清单重建仍选
   A、B 且结果逐字节等价。另覆盖跨运行同局部 ID 不误认、非标准观测被排除、
   漏报非首条来源批次被拒。
2. 三组 (1,1)/(2,0)/(0,2) 目标 (2,2)：第一组单看未超额但被跳过，交付后两组；
   合法无解抛 `HoldoutInfeasible` 且不返回数据；清单调序、未知版本与资料错误
   的约定行为；逐行检查成员、来源、批次、组与计数。
3. 128 MiB 地址空间、CPython 3.11 单核负载：1,050 个 L 单行组 + 1,050 个
   H 单行组 + 600 个混合组，目标各 1,500。新进程从打开已备归档与清单到
   构建并逐项核对完整两份数据（准备归档不计时）须不超过 45 秒。用例内自行
   生成归档、以 `ulimit -v 131072` 运行子进程并计时；本机实测约 0.55 秒、
   峰值约 46 MB。该时间线只针对这一负载，不承诺所有合法输入同耗时。
