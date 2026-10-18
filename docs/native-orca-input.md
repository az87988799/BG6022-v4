# 本地原生 ORCA 输入的已实现边界

此文分别记录历史 `native-orca-input-1` 与显式配置 `bounded-analytic-1` 后使用的 `native-orca-input-2`。准入不等于科学检查通过；运行仍必须经过统一执行入口、固定资源与预算、实际操作系统隔离和原始证据归档。以下首先保留版本 1 的范围；新版本见后文。

原生输入以不可变 Artifact 引用与 SHA-256 绑定。适配器在复制前校验原始输入和全部依赖的准确字节，保存为独立尝试中的 `job.inp`、依赖文件和 `input-manifest.json`。已存在的尝试输入不可覆盖，源文件不可为符号链接、重解析点或硬链接；已有工作目录仅允许协调者预先保存的 `intent.json`。输入上限为 1 MiB；全部依赖合计不超过 10 MiB，最多 32 项。

当前接受的输入：

- 一般简单科学关键词，包括方法、基组、常见收敛和溶剂关键词；没有 HF/STO-3G 方法白名单。未知方法名仍由 ORCA 自己报告语法或科学错误，不能因准入而宣告方法有效。
- 一个 `* xyz charge multiplicity` 笛卡尔坐标块，或一个引用 manifest 中单帧 XYZ 的 `* xyzfile charge multiplicity filename.xyz`。
- 显式 `%maxcore`；可选的唯一 `%pal nprocs N end`，缺省按串行记录。声明的核数、MaxCore 及其乘积必须在 Run 固定配置内。MaxCore 不能代替进程树总内存限制。
- 必须包含 `NoAutoStart`。显式 `%moinp "initial.gbw"` 和 `MORead` 可读取 manifest 中已固定的 GBW；没有依赖引用的 `MORead` 被拒绝。
- `%scf` 的有限标量字段（迭代上限、常见收敛与数值阈值）、`%output` 的 JSON property 开关及打印级别、`%tddft`/`%cis` 的根数/迭代/TDA 标量、`%cpcm` 的介电/折射率/SMD 标量。具体字段以唯一准入实现为准；其他块、字段、嵌套语法返回具体不支持原因。

当前拒绝的执行影响包括外部程序接口、Compound/多作业、扫描、频率/数值导数、NEB、GOAT、MD、DOCKER、SOLVATOR、QM/MM、外部 DMRG/MRCC/NBO、xTB 接口和自动检查点继承。原生优化暂不准入：任意方法的梯度回退与内部计算核算需要额外契约。已有 OPI RHF 优化继续通过原来的科学参数与检查器验证，并使用同一个输入准入/启动 manifest；其明确优化迭代上限加一次最终能量评价作为申报上界，不扩大原有科学保证。

依赖目前仅接受工作目录内的 XYZ 与 GBW 普通文件名，且每个文件必须实际被输入引用。禁止目录、绝对路径、`..`、ADS、重定向、`%base` 改名、动态库/脚本/可执行文件和多帧 XYZ。拒绝重复声明及带有效尾部语法的 `#comment#`，避免把 ORCA 的注释后输入误当无效内容。`JSONGBWFile` 转换被拒绝；已有 property JSON 的只读解析不执行转换。

准入器不是完整 ORCA 解释器。它不为任意未知简单关键词证明不存在其他内部工作，也不证明任何方法的科学适用性。运行边界必须独立阻断越界文件、网络和未许可进程；不能用关键词检查替代这些真实测试。输出文件按实际存在情况保留，缺少 property JSON 不代表整个原生执行失败。

依据是 ORCA 6.1 官方资料：

- [输入格式、重复赋值、闭合注释、MaxCore、BaseName 与多作业](https://www.faccts.de/docs/orca/6.1/manual/contents/essentialelements/input.html)。
- [运行类型与方法类别](https://www.faccts.de/docs/orca/6.1/manual/contents/essentialelements/basics.html)。
- [外部优化接口](https://www.faccts.de/docs/orca/6.1/manual/contents/structurereactivity/optimizations.html)及 [FACCTs 外部程序接口](https://github.com/faccts/orca-external-tools)。

单元测试只证明输入准入、hash、路径和旧适配器回归；真实 ORCA、实际 OS 隔离、MPI 和资源行为必须分别依据执行验收记录。

## 显式有界解析导数策略

新 Run 可在 `linux-container` 执行配置中冻结 `native_input_policy.profile = "bounded-analytic-1"`，使用 `native-orca-input-2`。省略策略时字段仍不进入历史序列化，保留版本 1 行为、配置指纹和旧结果事实。新策略拒绝不支持的输入，不回退到版本 1；它同样约束单点输入，不能借单点绕过条件绑定。

版本 2 的有限兼容集合只用于判断可管理的执行影响，不能解释为科学方法推荐或科学资格白名单。唯一规则源是 `orca/native_profile.py`，其 `profile_contract()` 同时提供模型可见契约：

- 允许一个单点，或同一次 ORCA 调用内一个 `Opt`/`TightOpt`/`VeryTightOpt` 加 `Freq`/`AnFreq`；SDK 仍只允许一次调用。不接受独立频率、多作业、扫描、数值频率、检查点继承或外部程序。
- 当前方法为 HF、BP86、PBE、BLYP、PBE0、B3LYP、B3LYP/G。HF 必须明确 RHF，DFT 必须明确 RKS，均只支持显式单重态。B3LYP 与 B3LYP/G 保持区别。基组限 STO-3G、3-21G、6-31G、6-31G*、6-31G**、def2-SV(P)、def2-SVP、def2-TZVP、def2-TZVPP；`6-31G(d)`、`6-31G(d,p)` 分别规范为 `6-31G*`、`6-31G**`。元素限 H—Ar，不接受 ECP、相对论或自定义基组。
- 必须显式 `NORI`、`NoAutoStart` 和唯一 `TightSCF` 或 `VeryTightSCF`。DFT 网格只允许可选的唯一 `DefGrid2` 或 `DefGrid3`。未列入契约的关键词和块直接拒绝；因此气相条件来自闭合语法，而非看到未知溶剂语法后猜测。
- `%scf` 必须显式 `MaxIter`、`ConvForced true`、`AutoTRAH false`。优化频率必须另设 `%geom MaxIter`、`EnforceStrictConvergence true` 与 `%method Z_MaxIter`。策略默认上限分别为 256、128、128 次和 32 个原子；Run 可在允许范围内冻结更小上限。资源仍由 Run 核数、MaxCore、总进程树内存和期限共同约束。
- 只接受 `* xyzfile` 的任务内普通 XYZ 文件名。依赖字节必须等于当前 Request 对应的已准备或已注册几何 Artifact；原子数相同不足以通过。方法、基组、电荷、多重度、电子态和环境由准入器从输入解析，并与当前 Request、适用 Goal 和 Step 的明确条件比较，不能由任务脚本的声明代替。

准入保存 `actual_conditions_origin: "admitted_native_input"`，以及带同样 `origin` 的 `actual_conditions`；其中包含 `request_version`、`system_id`、`input_geometry_sha256`、规范条件和 `run_type`。这些是已接受输入的条件记录，并非独立输出核验。完整 `input_binding` 另保存 Request/Step 身份、条件来源和几何 Artifact/hash。

工作量先预留再执行：单点为 1；优化频率为 `(geom MaxIter + 1) + 1 + 3N`，分别表示几何评价槽、Hessian 任务槽和响应方向槽。这是保守声明单位，**不是实际 SCF、梯度或响应迭代次数**。SCF 与响应迭代各有自己的显式上限；系统期限和总内存继续提供独立硬边界。直接原生调用在同一次 Run 原子保存中写入不可变 `Attempt.native_workload` 和累计用量；SDK 仅在内部调用回执中收费，不重复计入直接调用。恢复、未知状态或更换输入不能重置该预留。

实现依据是 ORCA 6.1 的[解析频率范围与数值导数区别](https://www.faccts.de/docs/orca/6.1/manual/contents/structurereactivity/frequencies.html)、[CP-SCF 响应上限](https://www.faccts.de/docs/orca/6.1/manual/contents/essentialelements/cpscf.html)以及 [NORI 与近似的区别](https://www.faccts.de/docs/orca/6.1/manual/contents/essentialelements/RI.html)。准入集合并不等于全部组合已经实测；目前开发底座只实际运行了一次四核、1024 MiB 总内存下的乙醇 HF/STO-3G 优化加解析频率，不能据此声称 DFT 组合或模型自主交付已经验证。

该开发调用的 ORCA 和 Python 操作完成，实际 ORCA 启动 1 次、预留 93 个声明工作单位、没有 OOM。首次真实测试在开发者独立核验器的绝对坐标比较处失败：Hessian 的 Bohr 坐标转换为 Å 后与最终 XYZ 使用不同原点。失败记录保留，未重跑 ORCA。规则 2 对同一原始文件使用相同原子顺序、无反射的刚体对齐和独立成对距离检查，仍维持 `1e-5 Å` 门槛；同时从 Hessian 矩阵与质量独立重建完整频率谱并比较 stdout。离线复核通过，27 个完整模式中有 6 个投影零模式和 21 个正振动模式。此核验器仅为开发证据，目前输出方法核验只覆盖 HF；它没有建立生产频率科学资格，也不证明全局最低能构型。

## Windows LPAC 私有运行库的范围

Windows 后端的 `private-task-runtime-1` 不把整个 Python 安装、用户 `site-packages` 或仓库授予脚本。它复制基础 CPython 可执行文件、Python DLL、标准库及扩展 DLL，以及明确的安装分发依赖：OPI、RDKit、NumPy、Pillow、platformdirs、Pydantic、pydantic-core、annotated-types、typing-extensions、typing-inspection、semantic-version。采用安装分发的文件清单复制，排除测试目录、`.pth`、宿主入口程序和无关包；专用 `python311._pth` 只列私有运行库中的目录，不初始化 `site`。薄 SDK 作为单个受信任文件另行复制。

ORCA 私有运行库只复制已安装主程序以及 `orca_startup`、`orca_guess`、`orca_leanscf`、`orca_prop`、`orca_scfgrad`、`orca_util` 的串行与 MPI 版本。Windows 原生驱动使用命令解释器启动内部模块，因此同时复制系统 `cmd.exe` 的准确字节供 manifest 固定；实际清理后的 `ComSpec` 仍指向 `System32\cmd.exe`，它是受 LPAC/Job 约束的 Windows 运行依赖，不能声称实际仅执行私有副本。Python 任务由操作系统禁止子进程。不能把这一事实表述为“ORCA 环境不含命令解释器”。原生输入的路径和控制语法校验、ORCA 子进程继承的 LPAC 与 Job 约束仍是必要边界。

每次私有复制上限为 4 GiB，并记录每个文件的 SHA-256、字节数、源位置与 Python 分发版本。目标文件为新文件；系统源文件即使由 WinSxS 硬链接管理，也只读出并复制其字节，不修改源文件 ACL。运行库保存完整 manifest；本机源程序、大型运行库和运行产物不进入 Git。

可选的本地 MS-MPI 复制仅包含固定四个文件：`mpiexec.exe`、`smpd.exe`、`msmpi.dll`、`msmpires.dll`，并将其 hash 和所选安装版本写入同一个运行库 manifest。没有复制或启用 MPI Launch Service，没有自动授权远程节点或网络。`stage_mpi_runtime` 可单独建立这四个文件的私有目录；`stage_orca_runtime(..., mpi_path=...)` 将它们合并进 ORCA 私有目录，避免把宿主 MPI 安装目录授权给任务。

2026-10-18 的独立真实进程烟测使用 MS-MPI `10.1.12498.18`，`-n 2` 启动两个不进行科学计算的 Python 程序。父启动器验证为 LPAC；两个工作进程均报告 AppContainer token 和 Job 成员身份，宿主采样确认启动器、`smpd`、两个 Python 和三个 `conhost` 均属于同一个 Job、亲和掩码为 3。总计 7 个进程，约 1.156 秒、峰值提交内存 24,424,448 字节，结束后没有遗留 MPI 进程。另一条 `-localonly` 探针因该 MS-MPI 版本不支持此选项而返回错误，没有启动工作进程；它不构成 MPI 功能失败或通过的证据。探针未开启网络 capability，也未变更服务或宿主目录 ACL。

这项烟测只证明本地 MPI 启动链的进程兼容与受管事实，没有调用 `MPI_Init`、验证 MPI 消息交换或替代真实 ORCA 并行验收。选项依据 [Microsoft 的 mpiexec 文档](https://learn.microsoft.com/en-us/powershell/high-performance-computing/mpiexec?view=hpc19-ps)；实际可用选项与诊断以已安装版本为准。

随后两次独立的真实 MPI 初始化探针揭示了兼容性阻塞。两个 Python 工作进程通过 `ctypes` 加载私有 `msmpi.dll` 并调用 `MPI_Init`，预定再执行 rank/size、`MPI_Allreduce` 与 `MPI_Finalize`。默认传输设置在 `MPI_Init` 中失败，报告 `NdStartup failed with 0x8007271d`；按 Microsoft 文档设置 `MSMPI_DISABLE_ND=1`、`MSMPI_DISABLE_SOCK=1`、`MSMPI_DISABLE_SHM=0` 的共享内存探针仍在 `MPI_Init` 失败，报告 `errno11001`。两次退出码均为 -4，耗时分别约 0.422 秒和 0.328 秒，均未到达 rank、消息交换或 finalize。宿主在每次运行观察到的 7 个进程均属于同一 Job，没有残留 MPI 进程。测试没有放宽 LPAC 网络 capability、文件授权或服务权限。

因此，在上述 **Windows LPAC 后端** 中仅有 MPI 进程启动成功的证据，没有可用的 MPI 科学计算路径；该后端当前仅验证串行科学执行。这一失败不适用于下述独立的 Linux 容器/Open MPI 路径。不能用启动成功替代初始化成功，也不能自动改用串行结果冒充请求的并行运行。环境变量含义见 [Microsoft 官方传输配置](https://learn.microsoft.com/en-us/powershell/high-performance-computing/environment-variables-for-the-mpiexec-command?view=hpc19-ps)。[Windows 官方错误码](https://learn.microsoft.com/en-us/windows/win32/winsock/windows-sockets-error-codes-2) 将 10013 标为访问被拒绝、11001 标为主机名未找到；这说明诊断类别，并未单独定位完整根因。应保留这个失败事实，不能为通过验收而添加网络权限或调用未隔离的宿主 MPI。

上述 Windows 私有模块集并非完整 ORCA 安装，也未验证所有方法组合。需要其他计算模块、转换程序或外部程序的输入可能被准入器拒绝，或由 ORCA 返回缺失模块的真实失败；程序不得自动复制未知助手、调用宿主未隔离安装或模拟结果作为回退。Windows MPI 服务与系统运行库依赖必须单独验证，复制 `_mpi.exe` 不能证明并行科学路径已可用。库导入烟测仅证明执行底座兼容性，不证明 Agent 自主解题。

## Linux 容器运行库与已验证范围

`linux-container` 是新 Run 可显式选择的本地后端，使用固定 Linux CPython 3.11.2、OPI 2.0.0、ORCA 6.1.1 与 Open MPI 4.1.8。配置入口见 [config.container.example.toml](../config.container.example.toml)，私有镜像构建、依赖锁和许可证说明见[运行镜像 README](../scripts/container_runtime/README.md)。执行绑定不可变 image ID、manifest hash 与本地 Docker daemon 身份，不自动拉取镜像，不把宿主 ORCA、Store 或用户目录挂入容器。旧 Run 的环境快照不会自动迁移到新后端。

已验证配置把任务 Python 与内部 ORCA 放在同一个容器 cgroup 的 4 核、1024 MiB 总内存边界内；两者使用不同 UID，Python 无权读取或执行 `/opt/orca`，须经薄 SDK 向可信协调者提出有限 ORCA 请求。根文件系统只读、容器网络为 `none`，任务 Python 的附加 seccomp 禁止子进程和 socket；本地 MPI 只使用容器内的隔离环境。任务文件位于专用持久卷，通过固定 helper 有界传输，证据卷没有硬磁盘配额。ORCA 实际启动须由同 PID/start_ticks 的 `/proc/PID/exe` 正证据确认；仅创建或放行启动器不计实际 ORCA 启动。

步骤 2 的最终固定镜像通过 7 项真实验证：科学库及操作系统拒绝边界、超时、OOM、取消、Python 薄 SDK 四 rank ORCA、既有 OPI 四核水单点与优化。准确镜像身份、原始证据及限制见[步骤 2 Linux 验证报告](acceptance/code-as-tools-step2-linux/report.md)。这些历史验证没有扩大 `native-orca-input-1` 准入范围：该版本的原生优化、频率等仍按上文拒绝；既有 OPI 优化继续使用原科学检查器。原生执行和 SDK 原始输出只作为未获科学资格的观察，不能凭退出码或正常终止标记自行宣告科学通过。步骤 2 的记录不包含步骤 3 的真实模型解题验证；新策略及开发证据单独记录于前文。
