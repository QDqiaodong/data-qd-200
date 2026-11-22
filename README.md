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

本仓提供当前文档描述的业务基线；尚未实现的演进能力见契约中的集成说明，不承诺固定数量的后续任务。
