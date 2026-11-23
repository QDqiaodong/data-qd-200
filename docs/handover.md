# 离线分卷交接与按案复核

把一次已经封版的实测归档（`resistor_flow.measurements` 发布的目录）中
**一个已发布的正整数版本**封装成一份封存目录与若干可独立复制的数据卷，
移交给没有原归档的离线复核组。目录先交接，数据卷按需调取；复核方凭
独立交接的目录 SHA-256 摘要、收到的目录与本地卷目录按案复核；材料不齐
时凭补卷单向发卷方调取所缺卷。本模块纯标准库、不连接外部服务、不操作
设备，不改动归档与既有求解、发布、数据集、质量复核接口的任何行为。

## 接口

```python
from resistor_flow.handover import (
    seal, review_case, deliver_volumes, HandoverError,
)

info = seal(store_dir, run_id, version, output_dir, max_bodies_per_volume)
result = review_case(catalog_digest, catalog_path, volumes_dir, request)
delivery = deliver_volumes(sealed_dir, order)
```

三个接口的失败（封装输入非法、目录无法读出、目录摘要不符、请求字段
非法、运行或版号与目录不符、补卷单越范围或所请求卷不可用）都抛
`HandoverError`（`ValueError` 子类），不返回部分结果，也不改动任何
已交接资料或原归档。

## 封装：`seal`

```python
info = seal(store_dir, run_id, version, output_dir, max_bodies_per_volume)
```

- `store_dir`：`measurements.publish` 使用的归档目录；只读，不推进版本。
- `run_id`、`version`：一个**已发布的正整数**版本；未知运行或版号报错，
  不回落当前版。
- `output_dir`：输出目录，必须未存在。
- `max_bodies_per_volume`：每卷最多容纳的裁决或证据正文条数，1..256 的
  整数（布尔值不算）。

以该版标准观测、冲突和逐身份证据为准：

- 按 `(tick, resistor_id)` 保留每个有绑定证据的组及其实际裁决：标准观测
  的精确值和完整来源，或冲突的完整来源；
- 按 `(station, row_id)` 保存每条完整原始证据（原始读数、状态、匹配
  登记、换算值与观测键），包括未绑定项；每条原始证据正文在全部卷中只
  保存一次。

输出布局：

```
<output_dir>/
    catalog.json            # 封存目录
    volumes/<sha256>.json   # 数据卷，按内容寻址命名
```

- **目录只存键、归属、路由与完整性信息**：测点时刻键、原始身份、组的
  类别（采用/冲突）与成员归属、每条正文位于哪一卷第几条、每卷的
  SHA-256。目录不含测量值、换算值、登记正文，也不替代裁决正文。
- **裁决卷与证据卷分离**，可以分别交接；不需要复制原归档的版本树、
  相邻变更、其他运行或其他版本。
- 卷以内容摘要命名：同一版本与同一上限重复封装，目录与卷的内容标识
  完全一致，与输出位置、时间无关。

返回 `{'run_id', 'version', 'catalog_digest', 'catalog_path',
'volumes_dir', 'volumes'}`；`catalog_digest` 是目录文件的 SHA-256。

## 目录摘要的独立交接前提

实验室必须**另行**把 `catalog_digest` 经可信渠道交给接收方（与目录、
数据卷的传输渠道分开）。接收方只认与该摘要相符的目录：

- 不以数据卷自报的运行或版本选择目录；卷内自报的身份只用于与目录
  核对，不能代替内容校验；
- 目录无法读出、目录字节与摘要不符、或请求字段非法，整个请求报错；
- 请求中的 `run_id`、`version` 必须与目录一致，未知运行或版号不回落。

## 按案复核：`review_case`

```python
result = review_case(catalog_digest, catalog_path, volumes_dir, request)
```

`catalog_path` 指向收到的目录文件，`volumes_dir` 是本地卷目录（可以
只到一部分卷，也可以混入同名旧卷）。`request` 是封闭字段集合，两种
动作：

```python
{'run_id', 'version', 'action': 'observation', 'tick', 'resistor_id'}
{'run_id', 'version', 'action': 'identity', 'station', 'row_id'}
```

ID、tick 与原始身份语义沿用发布链路。复核只读本案依赖的卷，不枚举或
打开无关卷；确认不存在不需要读卷。返回结果一律标明 `catalog_digest`
与 `version`，`status` 为下列之一：

| 状态 | 含义 | 附加字段 |
| --- | --- | --- |
| `complete` | 材料齐备且全部核对通过 | 见下 |
| `awaiting_volumes` | 案存在但材料不齐（待补卷） | `missing_volumes`：该案全部不可用卷的去重、稳定排序清单 |
| `no_bound_evidence` | 该版此键没有绑定证据 | `key`（不读任何卷） |
| `no_such_reading` | 该版没有该原始身份 | `identity`（不读任何卷） |
| `inconsistent` | 完整性通过但正文与目录矛盾 | `detail` |

**按测点时刻**：目录没有绑定组的合法键确认 `no_bound_evidence`；有冲突
的组不算这个状态。组存在时，只有该组的裁决正文和其列出的**全部**完整
原始证据都可核对，才交付 `verdict`（裁决正文）与 `evidence`（逐条完整
证据，按来源顺序）；冲突照样需要完整证据。不重算或改判电压，不因某站
的卷未到而缩减来源。

**按原始身份**：目录没有的身份确认 `no_such_reading`。未绑定身份须看到
完整证据才可确认未绑定（`disposition: 'unbound'`），不取任何裁决卷。
已绑定身份须看到完整证据及其所属组的裁决正文，并核对该身份确为此组
来源、两处归属与裁决一致；交付该条 `evidence`、`group` 归属与
`disposition`（`accepted`/`conflict`），**不**要求取同组的其他原始
证据，也不交付裁决正文——按身份复核成功不能冒充按测点时刻复核完整。

材料不齐时返回 `awaiting_volumes` 与该案**全部**不可用卷的清单：即使
裁决卷自身缺失，所缺证据卷也一次列齐，不会等第一轮补完再报第二轮。
清单未核完前不交付任何裁决或证据。

## 不可用卷与资料不一致的边界

卷是否可用**以封存目录为准**：

- **不可用**（计入 `missing_volumes`，按待补卷处理）：卷缺失、不可读，
  或内容的 SHA-256 与目录登记的摘要不符。同名卷、自报属于正确版本的卷
  都不能代替内容校验。
- **资料不一致**（`inconsistent`，不按待补卷反复索取）：完整性校验
  通过（内容摘要与登记一致），但正文与目录的路由、身份、归属或裁决
  矛盾——例如卷内正文身份与目录路由不符、证据归属与组裁决不一致、
  裁决正文与目录登记的键或来源不符。此时材料已到齐却是坏的，继续
  索取同样的卷没有意义。

目录本身无法读出、摘要不符或结构非法属于**目录问题**，整个请求抛
`HandoverError`，不进入上述两种状态。

## 补卷交付：`deliver_volumes`

```python
delivery = deliver_volumes(sealed_dir, order)
```

发卷方使用完好的封存副本（`sealed_dir` 含 `catalog.json` 与
`volumes/`，不需要原归档），接收补卷单：

```python
{'catalog_digest': ..., 'request': <原复核请求>, 'volumes': [卷标识, ...]}
```

发卷方**独立根据固定目录**核定该复核动作允许取用的卷范围（按测点时刻：
该组裁决卷加全部来源的证据卷；按原始身份：该身份的证据卷，已绑定的
再加所属组的裁决卷；目录能证明不存在时范围为空），不信任补卷单自填
的范围，也不必知道接收端究竟坏了哪些卷，允许按需分次请求。

- 目录摘要不符、请求字段非法、夹带范围外的卷、或任一所请求卷在发卷方
  副本中不可用，**整单拒绝**（抛 `HandoverError`），不交付部分；
- 合法时只交付所请求的卷，返回 `[{'volume_id', 'sha256', 'path'}]`
  （按卷标识排序）供复制；不顺带交付整组、整版或前后版本；
- 补卷不重封存，也不改动原归档；`sealed_dir` 全程只读。

## 可复现验收

```sh
# 在本仓根目录
python3 -I -m unittest discover -s tests -v
```

`tests/test_handover.py` 覆盖：

1. 同一版内：tick 4 的 r1 由 S/p、T/q 同值支持，tick 5 的 r1 双源异值
   冲突，S/u 未绑定，另有无关测点时刻。一条正文一卷封装并移除 tick 4
   裁决卷与 T/q 证据卷：按键查一次索取两卷；按 S/p 查只索取裁决卷；
   夹带 T/q 卷的单被发卷端整单拒绝；分次补卷后先按身份复核通过、再
   补齐得到完整双方证据；损坏无关卷不阻塞也从未被读取（含打开记录
   核对）；冲突、未绑定、确实不存在分别可辨，缺证据的未绑定项不被
   提前确认。
2. 新版只改 S/p 的增益与偏置（换算值、裁决与来源不变，登记证据不同）：
   旧卷冒名替换待补卷且不返回旧登记；卷已在但内容不符同样待补卷；
   旧目录加足旧卷仍可离线复核；补卷端不认旧目录对应卷；坏目录用例；
   逐项断言补卷单范围、交付的卷、最终精确值与历史登记。
3. 每卷上限大于一：组跨多个证据卷、不同案共用同一卷时按案准确列出
   不可用卷且不重复，同卷其他记录不混入案结果；移走原归档后仅凭已
   交接资料完成复核与补卷；重复封装内容标识一致。
