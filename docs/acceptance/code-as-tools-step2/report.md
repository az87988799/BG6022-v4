# Code as Tools 步骤 2：受控执行底座交付记录

日期：2026-10-18（Asia/Hong_Kong）。基线：`f1b7fb76bd00fc6032c1dccfbd51f61646f86fde`。步骤 1 已由用户验收通过；本次仅实施步骤 2，未推进步骤 3—7。**串行最小链路和操作系统边界已取得真实证据；MPI 初始化仍受阻，步骤 2 未标为完整兼容性全部通过。等待用户验收。**

## 实际实现

- `python.execute` 运行不可变代码 Artifact 与 manifest 输入副本；`orca.run` 接受原生输入 Artifact、XYZ/GBW 依赖与明确资源声明。实际文件、stdout/stderr、代码 hash、输入、执行身份和资源分别保留。
- 薄 SDK 只传输一个有界 ORCA 请求及回执。当前 Run 协调者校验许可、预算、依赖和执行影响后启动；脚本不能直接访问 Store、CLI 或创建子进程。没有预写构象、扫描等科学求解流程。
- 原 OPI SP/Opt、直接原生输入和 SDK 共用 `managed.launch_orca`；没有无隔离备用执行链。原科学检查器保留，进程成功不会自动授予科学资格；通用结果明确为 `unverified`。
- Python 与内部 ORCA 共用环境租约和父 Job 累计资源，内部 ORCA 另有局部 Job 与启动回执。预算预留先持久化、实际启动只计一次，父 Job 已含子计算成本，Run 不重复累加。内部调用身份不创建新的领域生命周期。
- 输入准入不固定方法白名单；对多作业、外部程序、路径、未知控制块及无法核算的计算影响返回具体拒绝。原生优化及频率尚未准入；原 OPI RHF 优化保留有界迭代契约。完整范围见 [原生输入边界](../../native-orca-input.md)。
- 按真实存在的输出归档，缺少固定 JSON 不使通用操作全局失败；只读 OPI 不启动转换。归档失败保留已导入证据和原文件，执行状态与证据完整性分开记录。

## 真实隔离与资源

本机采用 Windows LPAC，并在进程恢复前检查 token 与内核访问结果；普通 AppContainer 被负例拒绝。必要 Python 标准库、锁定科学库与原生助手复制到私有只读运行目录，不授权整个安装目录、用户 site-packages 或产品源码。任务只获输入读取、任务目录写入及固定 IPC 路径；SDK 请求由固定句柄读取，拒绝重解析点、硬链接和路径重定向。

仅授予 CPython 初始化所需 `registryRead` capability，没有网络 capability。真实进程测试拒绝了文件/注册表凭据哨兵、宿主源码、宿主秘密环境变量、运行库及输入写入、profile 自带存储、目录根重定向、回环与外部网络、父进程访问和未列入继承清单的文件句柄。凭据验证使用人工哨兵，没有读取或保存真实凭据。

Python 通过内核 child-process policy 拒绝 `subprocess`、shell 和直接 `CreateProcessW`；原生 ORCA 的必要助手允许创建，但继承 LPAC 与 Job。系统命令解释器是 ORCA 的实际 Windows 运行依赖，不能把此边界理解为原生环境内没有 shell。独立允许子进程测试验证了子 token 的 LPAC；MPI 探针只验证了工作进程的 AppContainer/Job，不能由此声称所有 MPI 后代均已完成 LPAC 验证。

JobObject 原子绑定进程树，限制 CPU affinity、累计提交内存、期限与取消。单独的真实测试覆盖超时、取消、内存超限、父 Python 与 broker 子进程的合计内存。任务写盘没有文件系统硬配额；输入、IPC、运行库复制和产物归档各有上限。默认任务 Artifact 单文件与累计字节上限均为 256 MiB、数量 200，私有运行库复制上限 4 GiB。不能把归档预算说成运行期间磁盘写入硬限制。

运行库复制在 Python 启动前完成，避免大文件复制阻塞活动任务监控。审查又发现 SDK 返回后同步归档的控制轮询空档；最后补充了 hash、复制分块及 fsync 前后的父 Job 控制检查。该补充通过独立真实进程和单元测试验证，没有追加 ORCA 启动。单次操作系统 I/O 调用的阻塞时间仍不具有实时上界。

## 真实 ORCA 与 MPI 结果

固定环境为本机 CPython 3.11.4、OPI 2.0.0、ORCA 6.1.1、MS-MPI 10.1.12498.18；没有安装或升级科学依赖。执行环境上限仍为 4 个逻辑 CPU、1024 MiB 总提交内存、MaxCore 192 MiB/进程、环境并发 1。最终通过的科学输入均**显式声明 1 核**，没有将四核失败请求自动重写为串行。Python 保留冻结的最多 4 核上限。

完整 [证据索引](evidence-index.json) 包含全部 10 次 ORCA 启动，失败不会因后续修复被改写：

| 序号 | 实际验证 | 结果与含义 |
| --- | --- | --- |
| 1 | Python SDK → 原生串行 SP | 助手绝对路径访问被拒绝，操作失败；保留原始错误 |
| 2–3 | 相对程序名与预先复制运行库后的 SDK → SP | 操作完成；通用结果科学资格仍未验证 |
| 4–5 | 原 OPI 四核 SP / Opt | MPI Startup 初始化失败，无合格科学输出 |
| 6–7 | 原 OPI 显式串行 SP / Opt | ORCA 进程正常结束；助手 stdout 缺失，原检查器拒绝资格 |
| 8 | 修复控制台输出传播后的 SDK → SP | 操作完成，原始文件与资源齐全；仍为未授予资格的观察 |
| 9 | 同一执行链的原 OPI 串行 SP | 原检查通过，energy 与 dipole_moment 合格 |
| 10 | 同一执行链的原 OPI 串行 Opt | 原检查通过，energy、optimized_geometry 与 dipole_moment 合格 |

最后一组真实测试为 **3 passed / 125.70 秒**。第 8 次父 Python Job 运行 14.328 秒、峰值提交内存 142,049,280 字节；第 9 次为 8.281 秒、115,576,832 字节；第 10 次为 18.156 秒、120,156,160 字节。最终 Opt 的进程身份采样有 5 次瞬时查询错误，原样保留；Job 总量和终止事实有效，但不能声称每个瞬时进程身份均采样完整。

全部 10 次 Run 已记录的运行墙钟合计约 146.921 秒、CPU 合计 65.890625 秒；Python 父 Job 已包含内部计算，不再加一次子 Job。该数字不含运行库复制、环境检查和 pytest 准备时间，不能作为完整开发用时。真实模型调用 **0**、远程/集群运行 **0**。开发者编写脚本和固定水分子 fixture 只证明底座与旧入口回归，不证明 Agent 自主生成代码或自然语言交付。

本文 ORCA 启动计数指提交科学输入的计算尝试。既有 doctor 的有界 `orca --version` 安装诊断不接收任务输入，不计入这些科学启动或 Job 成本；不能把 10 次计算尝试解读为整个开发期间仅创建过 10 个 ORCA 可执行进程。

另有 4 次不启动 ORCA 的真实 MPI 诊断：一次 `mpiexec -n 2` 仅启动工作进程通过；一次不支持的 `-localonly` 选项失败；两次实际 `MPI_Init`（默认、官方共享内存配置）均失败，分别记录 `0x8007271d` 和 `errno11001`，没有进入消息交换。探针结束无 MPI 残留。详见 [MPI 说明及官方依据](../../native-orca-input.md#私有运行库的范围)。

因此当前统一入口对 `cores > 1` 在创建进程前返回 `parallel_execution_unavailable`，工具目录也声明相同限制。历史版本四核通过的证据不等于新的 LPAC 链通过。后续若改用其他隔离平台、MPI 实现或受限 IPC 方案，须单独实施和真实验收；本次没有放宽网络、启用宿主服务或无隔离回退。取舍记录见 [ADR 0032](../../decisions/0032-managed-task-execution.md)。

## 回归验证

| 验证组 | 实际结果 | 说明 |
| --- | --- | --- |
| 契约、Store、SDK、原生输入、运行库及科学解析单元回归 | 311 passed，1 skipped，79.52 秒 | 唯一跳过项需要 Windows 创建符号链接权限；硬链接与 junction 另有真实边界测试 |
| Runner、消息门禁、恢复、旧后端及隔离集成回归 | 88 passed，225.42 秒 | 含真实非科学子进程和明确标识的合成生命周期替身，不当作 ORCA 科学证据 |
| 最后一次完整 LPAC 真实进程回归 | 15 passed，71.02 秒 | 在补入归档控制检查后执行；含实际运行中的取消和超时 |
| 真实 ORCA 最终三例 | 3 passed，125.70 秒 | SDK、旧 SP、旧 Opt；归档控制检查补充之后未新增 ORCA |
| Ruff、Git 差异检查 | 通过 | 最终源码与测试目录静态检查 |

测试组存在重叠，不相加声称独立测试总数。最终 XML 位于 [validation](validation/) 并在证据索引记录准确 hash。此前开发集成回归为 86 passed、1 failed：控制锁测试替身未同步统一入口的短锁生命周期；修正测试替身后定向 3 项及后续 88 项回归通过。失败 XML 同样保留。旧隔离取消例在最后一轮是创建前取消，实际运行中取消的证据来自新增 coordinator-work 测试，未混用两者。

复现命令（在仓库根目录，真实 ORCA 需明确开关；`--basetemp` 应使用新的 ASCII 路径以免覆盖历史证据）：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_managed_attempt_store.py tests/unit/test_managed_execution.py tests/unit/test_task_sdk.py tests/unit/test_native_orca.py tests/unit/test_runtime_files.py tests/unit/test_code_contract_planning.py tests/unit/test_run_configuration.py tests/unit/test_code_contracts.py tests/unit/test_store.py tests/unit/test_revision_store.py tests/unit/test_orca.py tests/unit/test_optimization_final_stage.py -q
.venv\Scripts\python.exe -m pytest tests/integration/test_runner.py tests/integration/test_message_launch_gate.py tests/integration/test_recovery_edges.py tests/integration/test_backend_windows.py tests/integration/test_isolation_windows.py -q
.venv\Scripts\python.exe -m pytest tests/integration/test_isolation_windows.py -q
.venv\Scripts\python.exe -m pytest tests/live/test_managed_sdk.py --live-orca -q
.venv\Scripts\python.exe -m ruff check src tests
```

## 限制与未验证项

1. MPI 科学路径不可用；当前新旧 ORCA 入口均要求明确串行输入。默认四核旧请求会被拒绝，不能沿用旧能力表推断当前并行可用。
2. 每次 Python 尝试最多一次新 SDK ORCA 调用；不支持第二次调用、已完成调用重放、检查点恢复或自主反馈补算。已有 Store 回执可只读审计，不能据此声称步骤 4 已完成。
3. 协调者崩溃后的未完成内部回执保持 `unknown`，保留租约、原文件和累计预算，不盲目重跑；自动内部对账及孤立 profile 清理未实现。正常退出清理 profile，清理异常保守保留未知状态。
4. 私有 ORCA 模块是已验证助手子集，不保证任意方法/关键词能运行；未知依赖、外部程序及未核算工作量被拒绝或产生有界真实失败。没有扩大原有科学检查器保证。
5. 本轮只验证本机 Windows，未验证 Linux、远程、Slurm、其他 Python/ORCA/MPI 版本或并发大于 1。动态库导入通过不等于所有库功能可用。
6. 未验证真实模型生成代码、自然语言端到端、乙醇优化/频率、通用交付界面、多次计算算法、正式多次重复矩阵。没有通过模拟结果宣称这些能力完成。
7. 本轮不运行全仓库所有历史测试；定向测试覆盖改动责任链及必要回归，最终命令和实际统计单独列出。

原始小文件采用 `.gitattributes -text` 保留准确字节及 SHA-256；大型运行库、二进制中间文件和完整工作目录留在本地 `data/`，未提交 Git。证据索引同时记录本地位置、大小与 hash。用户已有 `docs/acceptance/repair-cycle-development-4/` 未纳入本次改动。
