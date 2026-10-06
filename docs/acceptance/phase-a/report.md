# 阶段 A 实施与验收报告

日期：2026-10-06，Asia/Hong_Kong。状态：**工程交付，等待用户验收**。本轮按用户明确选择连续实施 A-01 至 A-10；本报告不修改原方案的冻结清单，也不代替用户验收。

## 交付结果

已建立不依赖模型的固定 Plan 执行链：结构化输入、七对象、单一工具注册表、OPI 输入与读取、Windows Job Object 受管执行、逐输出检查、持久预算、独立尝试、原始证据和显式恢复。CLI 提供 `doctor/tools/run/status/pause/cancel/resume/inspect`。只读工具与科学工具在同一注册表声明和校验，科学主循环不按具体工具名分支。

当前科学边界为 H₂O / CH₄ 组成、中性单重态、RHF/STO-3G、SP 与无约束严格 Opt。真实证据覆盖两个分子的 SP、Opt，以及水 Opt → SP。优化结构仅表示五项指定 TightOpt 判据通过，不表示已证明无虚频极小值。DFT 泛函、频率、任意体系、自然语言规划、自动修复、远程执行尚未实现，见 [能力矩阵](../../capabilities.md)。

## 环境与版本

Windows 10.0.26200、CPython 3.11.4、uv 0.12.23、OPI（`orca-pi`）2.0.0、ORCA 6.1.1、MS-MPI 文件版本 10.1.12498.18。Python 依赖由根目录 `uv.lock` 固定。ORCA/MPI 安装包不进入仓库，来源和许可证见 [依赖记录](dependencies.md)。

[环境诊断](environment.json) 保存版本和交付源码 SHA-256；各 Run 的 `environment.json` 另保存该次执行时的源码及可执行文件 hash。开发期间源码尚未提交，不能把最终提交冒充所有历史运行当时的版本；最终源码版本由包含本报告的 Git 提交确定。`doctor` 只探测版本，它的 MPI/Job 字段保持“未验证”语义，真实并行证明另见以下执行证据。

## 科学执行与独立复算

本轮有 **13 个不同的真实验收测试通过**：7 个科学流程、4 个独立参考对照、2 个生命周期测试。这里的“通过”指各测试满足预期；SCF/Opt 不收敛测试的科学目标仍为失败，取消和强杀也不是科学成功。这些测试分批执行，不能将其描述为同一条命令一次通过；开发失败和实际全部成本保留在历史索引。

| 用例 | 真实结果 | 主要证据 / Run |
| --- | --- | --- |
| 水 SP | SCF 与逐输出检查通过，能量约 `-74.962991615317 Eh` | `run_61ca5f60325b47c5877fdf7648d916a8` |
| 甲烷 SP | SCF 与逐输出检查通过，能量约 `-39.726715311235 Eh` | `run_0a0d2dfa67a74409b87d8a5863c29828` |
| 水严格 Opt | 五项判据通过，初始文件不变，最终能量约 `-74.965901192194 Eh` | `run_344e38db06f34308812bc6f50b82b535` |
| 甲烷严格 Opt | 五项判据通过，初始文件不变，最终能量约 `-39.726863679792 Eh` | `run_ee3e1bb12de348e8a22839d7334cad22` |
| 水 Opt → SP | 两次启动，消费者绑定具体生产尝试的合格结构 Artifact | `run_0c3d3d5d66ea422394c1d356ac9ba290` |
| 水 SCF 限次 | 真实 SCF 耗尽，无合格能量端口，原始证据保留 | `run_372cbd6c02c24073af475775687f69f4` |
| 水 Opt 限次 | 真实优化未收敛，无优化结构端口；已收敛局部 SCF 能量单独检查并绑定迭代/结构 | `run_c4b5f7c224304da989ab96da60986661` |

表中舍入数值便于阅读；精确值、单位、原文行号、检查版本、输入/输出身份及 hash 以 Result 和原始文件为准。[证据索引](evidence-index.json) 的 `current_cases` 定位每份收据，`history` 定位完整 Run、结果与 Artifact，不通过表格反向生成标准答案。

四个 SP/Opt 成功组合均与**预先单独编写的标准输入**复算比较：原始输出能量差均为 `0.0 Eh`，两个 Opt 距离矩阵最大差均为 `0.0 Å`，小于复算前声明的 `1e-7 Eh` / `1e-5 Å` 门槛。对照读取不使用产品 OPI 输入生成器或解析器；执行仍使用统一受管后端和预算。参考运行分别为 `run_343455727eb742a783109fefe2976f83`、`run_f568ef0cd3f5487e875fa6f237ab9c89`、`run_42fb6ab6abbe439bbf6732800fb75c99`、`run_7584a408d6bf42c5b9425218566d1af9`。这是同引擎工程重复性检验，没有真人化学专家签字，也不是跨引擎或实验精度证明。见 [冻结用例与参考方法](cases.md)、[参考测试](../../../tests/live/test_reference.py)。

## 执行与故障证据

两个 Opt 参考输出的最终五项表还做了独立只读开发审阅，逐行值、阈值、收敛标记与源文件 hash 保存于 [reference-review.json](reference-review.json)。自动参考测试独立比较能量和 XYZ 距离；这份表格审阅另行记录，不把产品自身的收敛检查冒充第二套独立测试。

原子 Job 绑定通过 `PROC_THREAD_ATTRIBUTE_JOB_LIST` 与挂起创建完成，持久化身份后才恢复线程；无法建立此绑定时拒绝启动。非继承 Job 句柄启用关闭最后句柄即终止，禁止 breakaway；CPU affinity 最多 4 个逻辑 CPU，Job 总提交内存 1024 MiB，ORCA 每进程 MaxCore 192 MB。内存口径是提交内存，不是工作集。

原生子进程测试验证父/子/孙进程、超时、取消、驱动退出但后代存活、协调者强杀、启动句柄保存前崩溃、双协调者、PID 复用，以及组合进程树内存额度。真实 ORCA 生命周期测试另外观察到同时存在的 **4 个 `orca_*_mpi.exe` ranks**，逐个记录创建时间、Job 成员身份和 affinity；取消与强杀后相关进程均为 0。采样错误和截断标记如实保留，进程枚举不是完整操作系统事件审计。

当前有效生命周期收据：

- `data/live-lifecycle/cancel-e32f2428eb8744a88e954098ee572d43/lifecycle-evidence.json`：取消后清理和额度释放通过。
- `data/live-lifecycle/kill_coordinator-1ec0ea9c65a24f62808575d9043cf9a4/lifecycle-evidence.json`：协调者强杀后系统清理通过，显式恢复只对账，不重复启动。

同用户本地执行环境共享 `%LOCALAPPDATA%\orca-agent\environment`，不因更换数据目录绕过并发 1。未知启动窗口保留额度；无法证明是否启动时停止，绝不从“句柄缺失”推断可以重跑。暂停完成当前作业后停止后续步骤；取消核实整个树；恢复保留原条件、预算、原始证据和资源使用下界。

## A-01 至 A-10 交付映射

| 项目 | 已交付内容与证据 |
| --- | --- |
| A-01 | Python 工程、锁定安装、默认离线测试、环境诊断、依赖/许可证、Windows 离线 CI 配置 |
| A-02 | [冻结用例](cases.md)、手写标准输入、预先容差；历史预算模板偏差及修正单独记录 |
| A-03 | `models.py` 七对象、`structured.py` 入口、单一注册表；参数、许可、目标、端口校验 |
| A-04 | `store.py` 原子保存、不可变快照、跨进程锁、累计预算、全环境额度与 hash |
| A-05 | 唯一 Windows 受管后端，系统级资源与完整树故障实测；[决定记录](../../decisions/0001-windows-backend.md) |
| A-06 | 两个真实 SP 成功、逐输出科学检查、独立参考复算与来源定位 |
| A-07 | 两个真实严格 Opt、初始几何保护、具体结构绑定、真实静态链 |
| A-08 | 真实 SCF/Opt 失败 fixtures、局部输出、JSON 缺失/冲突、只读无副作用验证 |
| A-09 | 启动窗口、Result/Run 保存窗口、暂停/取消/强杀/恢复、竞争与未知状态故障矩阵 |
| A-10 | CLI、README、能力矩阵、成本和证据索引、本报告；已交付用户验收，未记录用户接受 |

## 最低测试矩阵

| ID | 结论与可复现证据 |
| --- | --- |
| T01 | 环境缺失/版本拒绝：[test_doctor.py](../../../tests/unit/test_doctor.py)，本机版本见 environment.json |
| T02 | 结构/电子态/参数/路径/资源执行前拒绝：[test_structured.py](../../../tests/unit/test_structured.py)、[test_models.py](../../../tests/unit/test_models.py)，CLI 拒绝持久化见 test_evidence.py |
| T03 | 两个真实 SP 与独立对照：current_cases 中 water_sp / methane_sp / reference-* |
| T04 | 水畸变初始结构和甲烷严格 Opt：water_opt / methane_opt / reference-* |
| T05 | 真实 water_opt_sp；不合格/错误来源和检查版本阻断：[test_store.py](../../../tests/unit/test_store.py) |
| T06 | 真实 water_scf_limit 与原始失败 fixture；无合格能量 |
| T07 | 真实 water_opt_limit 与原始失败 fixture；无收敛结构，局部能量单独检查 |
| T08 | 真实 fixture 的缺失 JSON 与格式验证；合成样本的损坏/冲突回归：[test_orca.py](../../../tests/unit/test_orca.py)；归档/解析期间变动：[test_collection.py](../../../tests/unit/test_collection.py) |
| T09 | [test_evidence.py](../../../tests/unit/test_evidence.py)、test_orca.py：有界查看、hash 不变、无外部程序/转换 |
| T10 | test_store.py、test_structured.py：逻辑 ID/输入指纹/总次数/额外次数/期限，不因恢复或改标签清零 |
| T11 | [test_runner.py](../../../tests/integration/test_runner.py)、[test_backend_windows.py](../../../tests/integration/test_backend_windows.py)，另有真实四 rank 取消 |
| T12 | 原生多代树强杀测试与真实四 rank 协调者强杀收据 |
| T13 | test_runner.py 的 after_intent_saved 故障；未知额度保留，无盲目重跑 |
| T14 | test_backend_windows.py 的 after creation / before handle save 故障；线程未执行、系统收束 |
| T15 | test_runner.py 的 after_execution_saved / after_result_saved / after_run_updated 故障；精确重接或收集，无重算 |
| T16 | test_store.py 跨进程协调锁、不同数据目录共享额度；runner 实际进程竞争 |
| T17 | test_backend_windows.py PID 复用/终止不明；[test_recovery_edges.py](../../../tests/integration/test_recovery_edges.py) 成本下界与未知历史 |
| T18 | test_runner.py 参数准备后 Request/Plan/许可/暂停变化，控制锁覆盖实际 ResumeThread |
| T19 | 原生 CPU 与进程树提交内存实测；真实 ORCA 4 rank 成员/affinity 证据 |
| T20 | status 只读、显式 resume 对账、已完成 Result hash 复验；test_runner.py / test_recovery_edges.py |

最终完整离线回归为 **232 passed、14 skipped、0 failed**（73.02 秒）；Ruff 通过，离线 sdist/wheel 构建通过，wheel 包含全部 19 个生产 Python 模块。具体命令、跳过项和构建 hash 见 [validation.json](validation.json)。默认离线运行有 13 个真实测试按显式开关跳过；另 1 个真实符号链接创建测试因本机 Windows 权限跳过，不能计为通过。实际 NTFS junction 越界拒绝已另行实测通过。GitHub Actions 配置已提供，远端执行状态不从本地通过推断。

## 已发现、修复且保留的开发失败

1. 首个水 SP `run_7e16edbd983f49939ad3cb9b869cbbb9` 中，解析器将 `BASIS SET INFORMATION` 标题误识别为基组，保守拒绝输出。现改为解析实际基组字段，真实文件作为回归保留。该次 OPI 默认设置还请求了 GBW JSON，ORCA 实际调用 `orca_2json`；保留 `job.2jsonout`、原输入/输出/Result 和 **1 次实际后处理成本**，未提高原来的零预算，也不将此历史执行列为符合契约。当前明确关闭 GBW JSON，禁止读取时转换。
2. 水 Opt 早期 `run_fe7073cdcfce4f2fa9375bb84c769ade` 触发 ORCA 默认替代停止条件，没有满足冻结的全部五项阈值，结构端口被正确拒绝。后续显式启用 `EnforceStrictConvergence`，不放宽科学门槛或预算；v1 输入与结果保留，详见 [决定记录](../../decisions/0002-strict-optimization-and-json.md)。
3. 首次真实强杀的清理已成功，但恢复写临时文件撞到 Windows 路径长度限制，原收据保持未验证。缩短原子写临时名后，显式恢复同一个 `run_24bb1a6af202449586b90c1ce7b4962f`，确认终止、保留一次启动后释放额度，再独立复验强杀全流程。被旧额度阻止的 `run_950c6eb84b3f4992861cee9072e5939c` 未创建计算尝试，未另行绕过额度。
4. 早期生产 request 模板的预算上限为方案默认 3/4，与冻结清单的单次预算不一致；实际各单例只启动 1 次、链 2 次，无自动重试。后续模板已收紧为单步骤 1/1/0、链 1/2/0，历史许可/预算/成本不追溯改写，见 cases.md。
5. 最终组合故障审查发现后处理违规可能覆盖 unknown 执行状态，以及已对账完成保存后、释放额度前再次中断可能留下租约。修复要求 unknown 状态优先、违规事实独立留存，重新确认终止才释放旧租约；增加组合故障与再次恢复回归。此项为离线故障检验，不宣称新做了真实后处理违规计算。

## 成本、归档与复现

[evidence-index.json](evidence-index.json) 的 `cost_summary` 累计**全部本地开发科学 Run**，包括成功、预期失败、历史偏差、被额度阻止的 Run、生命周期及独立参考。启动预占与实际启动分列；协调者丢失期间无法取得的资源用量明确标记不完整，时间数字只是已测下界。版本探测、离线测试、依赖安装和开发助手自身时间不混入 ORCA 科学用量。

本次累计 18 个 Run、18 次预占、18 次已确认 ORCA 启动、1 次历史违约后处理；已测作业墙钟时间合计下界 182.58 秒、CPU 时间下界 330.546875 秒，2 个强杀 Run 的资源用量不完整。当前环境额度已确认释放。该成本包含开发失败，不能与 13 个验收测试数量等同。

完整原始数据保存在本机 `E:\BG6022-V4\data\`；独立归档的精确路径、大小、SHA-256、文件数见 [archive.json](archive.json)。ZIP 内 `manifest.json` 逐文件记录路径、大小和 SHA-256，保留 `data/` 相对结构，历史绝对路径作为来源记录。归档不包含 ORCA 安装包、凭据、虚拟环境或锁文件，也不包含旧归档本身。Git 提交包含真实回归 fixture 子集及其 provenance，完整运行产物按方案保留本机，远端仓库不含该完整 ZIP。

最终归档为 `data/archives/phase-a-20261006-final.zip`，1491 个文件、7,360,180 字节，SHA-256：`5262533256f148c58a9d6a18277d157e804a3d207f40e080b1f4ac86038e8a2b`。较早归档也保留，未覆盖历史文件。

安装、CLI 和默认离线/显式真实验证命令见 [README](../../../README.md)。已有独立参考收据再次运行时只验证旧来源 hash；若先重跑生产科学用例生成新 Run，旧参考不会被悄悄改绑或覆盖。复验已有交付可读取/校验现有证据；全新计算应在新的受控数据副本中建立新一批证据，并沿用同一全环境额度与明确预算。

阶段退出项的工程证据已列出。用户接受之前，阶段状态保持**等待用户验收**。
