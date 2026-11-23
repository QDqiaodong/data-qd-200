# data-qd-200 · 电阻网络电位与功率

由固定电位节点和正电阻网络解未知节点电压，计算支路电流、发热、额定功率诊断并检查KCL和能量守恒。

## 安装与运行

Python >=3.11，实际验证 CPython 3.12.14 + uv 0.12.19。运行依赖仅标准库；构建固定 setuptools 80.9.0、wheel 0.45.1。每个项目独立虚拟环境，公共下载缓存可以复用。

在本仓根目录执行：

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/resistor-flow --source fixtures/input.json --output run/demo.json
```

以上安装命令已在本地公共缓存存在时真实执行。首次使用且缓存为空时须先准备 Python 和锁定构建包，或在允许联网的安装环境移除对应 `--offline`；运行与测试不需要网络。`.venv` 互不共享，未纳入Git。

样例12V分压网络，中点8V、电流2A、电阻发热8W与16W，总供能24W，1件超过输入额定功率。

演示标准输出摘要：

```json
{"dissipated_watts": "24", "nodes": 3, "over_rating": 1, "resistors": 2, "supplied_watts": "24"}
```

完整证据写入 `run/demo.json`。数值输入为契约约定的十进制字符串或整数分数对，输出的精确有理数采用整数或 `n/d` 字符串，避免浮点舍入。

## 已执行验证

独立环境离线构建安装、依赖一致性检查、6项业务测试及安装后的命令行端到端演示通过。另核重复目标、符号链接目标和重复JSON键都返回2，既有输出字节不变，非法输入不发布报告。输出路径必须未存在，重新演示请选新文件名。

源码入口为 `src/resistor_flow/service.py`，测试在 `tests/`。业务字段、身份、单位、界限和失败约定见 [数据契约](docs/contract.md)。样例全部为原创合成数据，见 [样例来源](fixtures/SOURCES.md)，源码和样例采用MIT许可。

## 当前边界

只含理想电位边界与线性电阻的直流稳态，不含外加电流源、瞬态或真实设备控制。

## 离线实测数据发布链路

`resistor_flow.measurements` 在不改动既有求解、输入与输出行为的前提下，
提供仪表实测读数的离线发布与读取接口（纯 Python 标准库、状态全部落在
调用方指定的本地目录，无外部服务、无新增命令行）：

| 接口 | 作用 |
| --- | --- |
| `publish(store_dir, request)` | 校验并发布一个新版本，返回版号、变更集与完整快照 |
| `read_version(store_dir, run_id, version)` | 读取指定历史版本（未知版本报错，不回落当前版） |
| `read_current(store_dir, run_id)` | 读取当前版本 |
| `read_evidence(store_dir, run_id, station, row_id, version=None)` | 按原始身份查该版固定的证据 |

发布请求含 `run_id`、`request_id`、`base_version`、本批 `readings` 与本版
完整生效的 `registrations`；首版还须带一份通过现有网络输入校验的
`network`，后续版本沿用且不得替换。数值一律走现有的有界十进制字符串
口径与有理数字符串输出，链路内不导入求解器，任何值只来自实测读数与登记。

要点：

- 原始身份 `(station,row_id)` 全程保留；同身份同内容重发去重，异内容拒绝整次发布。
- 登记按站/通道的左闭右开区间匹配触发点；未绑定读数只进证据，不进标准观测。
- 标准观测键 `(tick,resistor_id)`：全部已绑定读数换算一致才发布一条并附稳定排序的
  来源身份；不一致则整键不发布并列出全部冲突身份，不择一、不平均、不沿用上一版。
- 每版交付有源版/目标版号的变更集（消失键 + 新增或改变的完整行，来源集合变化也算），
  消费者逐版应用即可重建各版观测表，无需清库重灌。
- `(run_id,request_id)` 幂等：同内容重放原版号与原变更集（旧底版照样重放），
  同 ID 改内容报错；未成功的请求不占用 ID。
- 提交为崩溃原子：版本目录暂存构建后原子改名、请求记录先于 `current.json` 切换；
  中断后重开只见完整旧版或完整新版，同请求重试可完整推进。

接口字段、版本语义与故障边界的完整约定见
[数据契约](docs/contract.md#离线实测数据发布链路)。

## 离线拟合/复验数据集构建

`resistor_flow.datasets.build_datasets(store_dir, manifest)` 从上述归档的
指定历史版本构造一对可直接消费的数据集：`fit`（估计仪器修正参数）与
`holdout`（独立复验）。只读取标准观测，按物理样件与仪表校准批次求不可拆
组，整组选取复验行使 `L`、`H` 精确命中调用方配额，其余进拟合集；不导入
求解器、不改发布协议与既有入口、不推进归档版本。清单格式、输出字段、
无解行为（`HoldoutInfeasible` 与输入错误 `DatasetInputError` 区分）及
128 MiB 下的可复现验收见 [数据集构建文档](docs/datasets.md)。

本仓提供当前文档描述的业务基线；尚未实现的演进能力见契约中的集成说明，不承诺固定数量的后续任务。

## 离线标准观测质量复核

`resistor_flow.review.review(store_dir, run_id, version, window, half_widths)`
对指定历史版本、指定采样窗口内的标准观测做只读质量复核（纯标准库，无新增
命令行、不操作设备、不求电流或发热、不改归档）。每个有观测的 tick 只要求
存在一套共同节点电位使各测点的 `V(a)-V(b)` 落在归档值加减半宽的闭区间内；
若整段仅隔离**一座**记录站即可恢复自洽，且这样的站恰好唯一，则交付仍受支持
观测，并为失去观测的测点给出独立标识为推断的补测区间。存在多种解释时明确
区分“无合格站”和“候选不唯一”，不择一、不扩大半宽、不把更宽的推断区间当作
实测数据。字段、状态与故障边界见 [质量复核文档](docs/review.md)。

## 离线分卷交接与按案复核

`resistor_flow.handover` 把一次已封版的正整数版本封装成封存目录与可独立
复制的数据卷，移交给没有原归档的离线复核组（纯标准库、无外部服务、不改
归档与既有接口）：

| 接口 | 作用 |
| --- | --- |
| `seal(store_dir, run_id, version, output_dir, max_bodies_per_volume)` | 生成封存目录与按内容寻址的裁决卷/证据卷 |
| `review_case(catalog_digest, catalog_path, volumes_dir, request)` | 凭独立交接的目录摘要按测点时刻或原始身份复核 |
| `deliver_volumes(sealed_dir, order)` | 发卷方按固定目录核定补卷单范围并交付所请求的卷 |

要点：

- 目录只存键、归属、路由与完整性信息；裁决正文与逐身份证据正文分卷
  存放，每条证据正文全卷唯一；同一输入与每卷上限（1..256）重复封装，
  目录与卷的内容标识一致，与输出位置、时间无关。
- 目录摘要经独立渠道交接；卷可用性以目录登记摘要为准，缺失、不可读
  或内容不符均不可用。材料不齐返回“待补卷”与该案全部不可用卷的去重
  排序清单；完整性通过但正文与目录矛盾报“资料不一致”，不按待补卷
  反复索取。
- 补卷整单拒绝目录摘要不符、夹带无关卷或所请求卷不可用；合法时只交
  付所请求的卷，不重封存、不改归档。

调用说明、目录摘要的独立交接前提与不可用卷/资料不一致边界见
[分卷交接文档](docs/handover.md)。
