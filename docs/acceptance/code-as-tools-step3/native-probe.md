# 开发者原生 Opt+Freq 底座探针

本记录只证明受管原生执行和开发者独立核验的局部行为。输入、Python 和初始几何由开发测试准备：**真实 ORCA 启动 1 次、真实模型调用 0 次，不属于自主任务验收**。本次没有重跑 ORCA 来修正核验器，也没有把开发评分写入生产科学资格。

Run：`run_88329c9d3fac449389550837df3d3ee1`。首次测试历史节点为 `tests/live/test_native_analytic.py::test_real_sdk_bounded_analytic_ethanol`；源码后来仅为避免 pytest 与单元模块重名而改名为 [test_live_native_analytic.py](../../../tests/live/test_live_native_analytic.py)，原失败记录未改。

## 执行事实与成本

| 项目 | 记录 |
| --- | --- |
| 科学输入 | 乙醇，HF/STO-3G、RHF、气相、电荷 0、多重度 1；一次 SDK 内 Opt + 解析 Freq |
| 明确上限 | 4 核，总内存 1024 MiB，MaxCore 192 MiB/进程；SCF 128、优化 64、响应 64；脚本 300 秒 |
| 操作 | Python 和内部 ORCA 均完成；退出码 0；归档完整；没有生产 `QualifiedOutput` |
| 启动与工作量 | ORCA 预留/实际各 1；模型调用 0；声明工作单位 93，即 65 个几何评价槽 + 1 个 Hessian 槽 + 27 个响应方向槽 |
| 实际资源 | 父容器墙钟 25.125 秒；累计 CPU 下界 29.736219 秒；观察到的峰值内存 831176704 字节；OOM/OOM kill 均为 0 |
| 完整性限制 | `resource_usage_complete=false`；CPU 来自终止前最后一次 cgroup 累计观测。父容器已含 Python 与内部 ORCA，不能再次相加内部成本 |

声明工作单位不是实际 SCF 或响应迭代次数，MaxCore 也不是总进程树内存硬限制。固定镜像为 `sha256:705838c326773e91f0b1d33bd572b3716931a07828bd66e48e4a0d5aaff8cb7e`，manifest 为 `535a71876cae4b67cbf8a7dd63f051b917a7e6c1b50f2c1afbade8776b1f104f`；准确配置、准入绑定和资源采样见[摘要](validation/native-probe-summary.json)。

## 首次失败与同数据复核

首次真实测试 JUnit 是 **1 failed，37.454 秒**。ORCA 已完成，失败位于开发核验器：将 Hessian 的 Bohr 坐标换算为 Å 后，直接与最终 XYZ 的绝对坐标比较。原始异常、当时断言和原测试节点保存在[规则 1 失败记录](validation/native-oracle-v1-failure.json)。不能把该 JUnit 改称通过，也不能把此次核验器错误说成 ORCA 执行失败。

同一组原始文件显示，转换后的 Hessian 坐标与 XYZ 的平均偏移为 `[-0.3998372534, -0.1182723914, 0.0780536957] Å`，两者使用不同原点。规则 2 保留相同原子顺序，以无反射的刚体对齐比较，并独立比较成对距离；**坐标门槛仍为 `1e-5 Å`，没有放宽容差**。随后只读取同一组输出离线复核，没有新进程或新 ORCA 启动。

[规则 2 结果](validation/native-oracle-v2.json)记录：

- 五项最终优化判据分别满足原输出给定门槛，SCF/响应完成标记存在。
- 9 个原子对应 27 个完整模式，其中 6 个投影零模式、21 个正振动模式；振动频率范围约 `235.962918—4224.273158 cm^-1`。投影零模式本身不是优化质量证明。
- 刚体对齐后的最大坐标误差为 `6.87e-9 Å`；最大成对距离误差为 `1.53e-8 Å`。
- stdout 与 Hessian 频率最大差为 `0.00499210 cm^-1`；由 Hessian 矩阵、原子质量及六个刚性运动投影独立重建的频谱最大差为 `0.000240119 cm^-1`。两者采用预定 `0.006 cm^-1` 门槛，覆盖 stdout 两位小数舍入。

这些事实与该 HF/STO-3G 结构的局部谐近似极小值相容，不证明全局最低能构型。核验器输出方法检查目前只覆盖 HF；本探针没有验证 DFT 组合、模型自主规划、生成解析器正确性或生产频率科学资格。

## 证据范围

[索引](validation/native-probe-index.json)列出本地原文件路径、准确 SHA-256/字节数、发布位置和省略理由。[小型原始文本包](validation/native-probe-raw.json)保存准确 UTF-8 字节可重建的开发 Python、初始 XYZ、原生输入、完整 ORCA stdout/stderr 和实际启动记录。完整 Hessian 与最终 XYZ 复用[已固定的原始测试文件](../../../tests/fixtures/native_analytic_hf/provenance.json)，不重复保存。五个 JSON 总计 307319 字节。发布副本仅将机器本地仓库路径改为仓库相对路径；各副本注明路径脱敏，原始失败信息、数值及本地原文件不变。

ORCA 安装包、运行库、GBW 等二进制产物、完整重复进程采样以及凭据不进入此包。完整本地执行证据仍保留；首次失败、规则修正和同数据离线复核分别列示，不替代后续真实模型开发任务的独立核验。
