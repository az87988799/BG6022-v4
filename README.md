# ORCA DFT Agent

当前 Code as Tools 新任务使用 [config.code.example.toml](config.code.example.toml) 的 `managed-native-1`：方法/基组不再受小范围枚举限制，开放元素、开壳层、离子、溶剂及独立计算阶段；Python 可分析已有证据与跨条件结果。运行与科学资格分别记录，旧 Run 保留原策略。配置本机镜像、模型和有限许可后，以 `orca-agent --config <配置文件> web` 启动；不要用旧的 `config.text.example.toml` 期待启用生成代码。边界和证据见[当前输入契约](docs/native-orca-input.md)及[验证报告](docs/acceptance/code-as-tools-general-admission/report.md)。下文保留各历史交付时点。

阶段 A 提供不依赖 LLM 的本地计算底座：结构化 Request/Plan → 注册 Tool → OPI 输入 → Windows 受管 ORCA → 逐输出科学检查 → 不可变证据及结果。阶段 A 已于 2026-10-06 获得[用户验收通过](docs/acceptance/phase-a/user-acceptance.md)，接受的代码基线为 `236376e`；历史真实证据见[验收报告](docs/acceptance/phase-a/report.md)。

阶段 B 已建立独立参考、受控修订、DeepSeek 适配与单一反馈循环，并完成首个自然语言请求到真实水分子单点的闭环，见 [B-04/B-05 证据](docs/acceptance/phase-b/b04-b05.md)。查询、导入、能差分析及受限 SCF 修复已有真实开发证据；采样追加和模型解释仍有失败，正式三次重复未启动，详见[当前实施状态](docs/acceptance/phase-b/implementation-status.md)。阶段 B 尚未完成或验收通过。

R-01～R-05 的后续修复见 [修复验收记录](docs/acceptance/phase-a-repair/report.md)。新请求采用 `orca-hf-2`；旧 Request/Result 保留原规则和原字节。旧 Run 仍可查看、取消、恢复对账及补收，但未完成的旧规则 Run 不会直接继续计算，需另行明确复验；本轮未实现自动规则迁移。

当前可发布已检查科学结果的范围是 **H₂O / CH₄ 组成、中性单重态、RHF/STO-3G、单点与无约束严格优化**。优化收敛只表示五项指定判据通过；既有生产科学检查不包含 Hessian/频率检查。受管理的原生输入可执行准入范围内的其他方法，但所得观察不自动获得科学资格；DFT、更广体系的科学保证及远程执行尚未实现。逐项状态见 [能力矩阵](docs/capabilities.md)与[原生输入边界](docs/native-orca-input.md)。

Code as Tools 步骤 2 已增加显式选择的本地 `linux-container` 后端。最终固定镜像通过 7 项真实测试：科学库与内核拒绝边界、超时、OOM、取消、薄 SDK 四 rank ORCA，以及既有 OPI 四核水单点和优化。Windows LPAC 的串行能力与 MPI 初始化失败继续作为该后端的事实保留；Linux 路径使用独立的 Open MPI 环境。详见[步骤 2 Linux 验证报告](docs/acceptance/code-as-tools-step2-linux/report.md)。这些是开发验证，等待用户验收；步骤 2 交付批次未包含步骤 3 的真实模型验证。

2026-10-19，用户明确要求“就先这样完成步骤三收尾”，停止 NameError 专项，不进入步骤 4。本轮步骤 3 收尾待验收；正常路径的 [live035](docs/acceptance/code-as-tools-step3/validation/live035-pro-autonomous-optfreq.json)在 v55 / Pro-v5 completed/complete，并通过[独立科学与完整交付核验](docs/acceptance/code-as-tools-step3/validation/live035-independent-scientific-audit.json)：1 次 Python、1 次四进程 ORCA，9 原子 Angstrom 坐标、完整 27 项 cm^-1 频率。观察仍 unverified、qualified_outputs 为空；不把该旧候选改记为当前版本重复通过。

受控错误修复后的完整交付仍未通过，按用户要求不再阻塞本轮交付。v57 显式失败 Step 修复引用已实际启动重跑；v58 仅澄清原有必需原生块指导，准入未放宽。live040 实际执行 1 次 ORCA，但 30 秒产物收集期限耗尽，第二 Python 失败，随后因用户要求暂停，最终 paused/partial、无观察。现存原始输出[独立科学核验通过](docs/acceptance/code-as-tools-step3/validation/live040-independent-scientific-audit.json)，不能替代完整交付；收集超时仍是未修复限制。

[001—040 累计审计](docs/acceptance/code-as-tools-step3/validation/cumulative-budget-live001-040.json)：40/50 个预约已使用，剩余 10 不再自行消费；491 HTTP、6601871 个已知 token，另保留 live036 的 73131 未知用量预约（不是实际 token），保守占用 6675002。20 次 Python（8 完成、12 失败）、10 次 ORCA、5 次代码重试、1442 工作单位。已知费用独立估算小计 USD 4.91126502，完整费用和 Pro 产品费用未知，非实际账单。[Flash 配置](docs/acceptance/code-as-tools-step3/validation/model-flash-v6-configuration.json)已按用户要求切换并通过禁网本地验证，未做付费复验；历史 Pro Run 不修改。

[第一轮冻结全量](docs/acceptance/code-as-tools-step3/validation/continuation-regression-001.json)为 4231 通过、23 失败、179 跳过；发现问题的定向修复、失败和复测分别保留。用户取消后续全量回归，不能宣称全量通过。本轮不记为用户验收通过，步骤 4—7 未实施。证据、限制和收尾范围见[步骤 3 报告](docs/acceptance/code-as-tools-step3/report.md)与[专项方案](docs/CODE-AS-TOOLS-IMPLEMENTATION-PLAN.md)。

## 安装与环境

Windows 历史基线及 LPAC 后端使用 Windows 10.0.26200、CPython 3.11.4、OPI 2.0.0、独立安装的 ORCA 6.1.1、MS-MPI 文件版 10.1.12498.18。宿主 Python 依赖由 `uv.lock` 固定，ORCA 安装包不随仓库分发。以下配置选择 Windows 后端；当前 LPAC 隔离下仅验证串行科学执行，不能据历史四核验收推断其 MPI 可用。

```powershell
py -3.11 -m pip install --user uv==0.12.23
py -3.11 -m uv sync --locked
Copy-Item config.example.toml config.local.toml
# 编辑 config.local.toml 中本机 ORCA、MPI 的绝对路径。
.venv\Scripts\orca-agent.exe --config config.local.toml doctor
.venv\Scripts\orca-agent.exe tools
```

Windows `doctor` 仅执行有期限的无输入版本探测；本机 ORCA 会打印版本后以退出码 2 报告找不到 `--version` 输入文件，该事实保留在报告中，并未提交科学计算。配置中的 MPI 目录用于相应后端的环境核验，核验时冻结可执行文件 hash；版本探测不证明 LPAC 内 MPI 初始化成功。

Linux 后端需要本地 Docker Desktop/WSL2 及已构建的私有镜像，镜像中固定 Linux CPython 3.11.2、OPI 2.0.0、ORCA 6.1.1 与 Open MPI 4.1.8。按[运行镜像说明](scripts/container_runtime/README.md)准备环境后，使用 [config.container.example.toml](config.container.example.toml) 显式配置 `execution.backend = 'linux-container'`、Docker 路径/context、不可变 image ID 与 manifest hash。示例身份只标识开发机上已验证的私有镜像，不是可下载的公共镜像；执行不会自动拉取或构建镜像。Linux `doctor` 只读核对镜像、manifest 和 daemon 身份，不启动计算。

新 Run 的配置快照固定所选后端与运行身份。旧 Run 不会因更换本机配置自动迁移后端，恢复时应使用其原始环境。

项目仅启用精确的 ORCA `6.1.1`；未知版本后缀、重复或含混版本证据会拒绝准入。第三方要求的最低兼容版本不等于本项目已验证范围。

## 本地网页

自然语言计算已[真实跑通水分子单点链路](docs/acceptance/natural-planning-repair/README.md)：在普通文本框输入“计算水分子初始几何的单点电子能，采用气相 RHF/STO-3G，中性单重态。”，模型解析目标、选择工具与依赖，程序生成执行参数及编号，OPI 准备结构，ORCA 计算并由模型解释结果。当前证据为该用例，不代表任意任务覆盖。

另有“新建计算”→“直接运行水分子单点”的固定预设入口，使用相同计算链且不调用模型；其[此前证据](docs/acceptance/web-geometry-recovery/README.md)与自然语言验证分别记录。

现在可使用本地网页提交已支持范围内的自然语言任务、补充消息、查看条件来源、实际尝试、预算、确定性报告和原始文件，以及暂停、取消和显式继续。当前为[网页局部交付](docs/acceptance/web-properties-knowledge/README.md)：水优化后偶极矩已有真实合格输出，知识和官方资料检索已接通但解释质量未通过；上传和完整网页验收尚未完成，阶段 B 原验收义务继续保留。

```powershell
py -3.11 -m uv sync --locked
# 首次使用且尚无本地配置时：复制后核对 ORCA/MPI 路径和有限许可。
Copy-Item config.text.example.toml config.local.toml
.venv\Scripts\orca-agent.exe --config config.local.toml web
# 浏览器打开 http://127.0.0.1:8765
```

已有 `config.local.toml` 时不要覆盖；仅启用所需的 `[text]` profile。密钥继续从后端进程的 `DEEPSEEK_API_KEY` 读取，不填入网页。`web --port 8766` 可选择其他本地端口；不提供公网监听或多 worker 选项。不带 `--config` 可启动只读历史页面，默认不允许提交新文本请求。

网页服务以前台方式运行。关闭或刷新浏览器不会重复提交或停止已启动的任务；退出前台服务会请求暂停，在原期限内收集当前任务后退出。需要立即停止计算时先点“取消”并核对状态。服务重启不自动执行任何 Run，继续须点击“继续处理”。暂停/取消按钮显示收到请求，实际完成状态另行展示。

后续消息只入原 Run，空闲时点击“继续处理”消费；不会重置已用模型/科学额度或期限。待处理队列最多 24 条，历史最多 512 条并分页保留，累计控制文件最多 8 MiB，达到上限前拒绝追加并保留可读历史。服务忙碌时新任务可以登记，界面会提示稍后明确继续，不建立隐式执行队列。相同提交编号或消息编号重发不重复创建或消费；中断且身份不明的登记保守拒绝重建。

证据查看、字段发现和下载都绑定所选 Run 的 Artifact，并核对原始 hash；下载最多 16 MiB，超限保留本地原路径供查看。历史科学结果按原规则展示，不能自动满足新版目标；“Run 已完成”与当前证据是否满足目标分别显示。旧验收批次 Run 不能从网页绕过原批次驱动器和累计账本继续执行。

## 结构化计算与控制

自然语言入口接收原文、独立目标、条件、证据和许可，不接受预制 Plan。真实模型使用进程环境 `DEEPSEEK_API_KEY`；设置用户环境后须重新启动终端，或将该用户变量读入当前进程。密钥不写入配置、日志或计算子进程。SDK 固定 openai 2.28.0 / httpx 0.28.1，关闭隐式重试。

```powershell
.venv\Scripts\orca-agent.exe --config config.local.toml ask tests/fixtures/phase_a/water_sp/agent-request.json
.venv\Scripts\orca-agent.exe --config config.local.toml message RUN_ID '补充或更改条件' --update-file user-update.json
.venv\Scripts\orca-agent.exe --config config.local.toml report RUN_ID
```

`message` 的更新文件只能包含用户明确修改的 Request 字段；不会扩大许可或重置预算。`report` 根据已有证据确定性生成报告，不调用模型。

`ask`/`resume` 的 Run JSON 保持原有脚本接口，是包含模型原答的审计记录；CLI 会在 stderr 标明这一点。
不能将 `decisions[-1].reason` 当作已验证科学结论。新的终止交付使用 `terminal-delivery-1`，
程序核对逐目标事实、解释关系、阻断和后续动作，关键数值与状态由确定性报告渲染。
合同通过、自由 reason 的独立评审、科学目标完成和报告生成分别记录；历史回答不会批量升级为合同通过。
已接受终止在崩溃恢复时先幂等补交报告。失败交付后的显式 `resume` 若重开工作，会保留原收据，
重新决策并复核当前条件、许可和余额，不直接执行旧 ready Step；任何预算均不重置。

无手工 XYZ 的水/甲烷输入可使用 `ask --text` 或 `ask --stdin`；二者与 JSON bundle 互斥。按 `config.text.example.toml` 在本地配置中显式启用 text profile，并核对 ORCA/MPI 路径、工具许可与各项预算后使用：

```powershell
.venv\Scripts\orca-agent.exe --config config.local.toml ask --text '优化水分子并给出优化后的电子能'
'计算甲烷初始几何的单点电子能' | .venv\Scripts\orca-agent.exe --config config.local.toml ask --stdin
```

该 profile 允许默认 RHF/STO-3G、气相、中性单重态，并记录默认来源；用户明确条件和未知优先。身份查询会联系官方 PubChem，结构准备经 OPI/RDKit 生成初始 XYZ，随后模型可按冻结许可发起 ORCA，所有影响各自计费并保留证据。“算水的能量”会先澄清 SP 或优化后能量；只登记或禁止执行仍按原文处理。准备结构不代表优化通过。输入链的离线与真实证据边界见[本批报告](docs/acceptance/bounded-gap-batches-4-5/report.md)。

```powershell
.venv\Scripts\orca-agent.exe --config config.local.toml run tests/fixtures/phase_a/water_sp/request.json
.venv\Scripts\orca-agent.exe --config config.local.toml run tests/fixtures/phase_a/water_opt_sp/request.json
.venv\Scripts\orca-agent.exe --config config.local.toml status RUN_ID
.venv\Scripts\orca-agent.exe --config config.local.toml pause RUN_ID
.venv\Scripts\orca-agent.exe --config config.local.toml cancel RUN_ID
.venv\Scripts\orca-agent.exe --config config.local.toml resume RUN_ID
.venv\Scripts\orca-agent.exe --config config.local.toml inspect --run-id RUN_ID
.venv\Scripts\orca-agent.exe --config config.local.toml inspect ARTIFACT_ID --start-line 1 --lines 40
.venv\Scripts\orca-agent.exe --config config.local.toml inspect PROPERTY_JSON_ARTIFACT_ID --field Calculation_Info.Charge
```

将输出中的实际 ID 替换示例占位符；字段示例须使用 property JSON 的 Artifact ID，键名区分大小写。`run` 表示明确授权执行该文件中的有限科学任务；几何必须位于请求文件所在目录内。条件缺省来源会保留，所有持久 ID 由程序生成。未知工具、未获许可或未通过准入的任务代码/原生输入、任意 shell、非法电子态、越界文件、循环依赖及超资源请求在执行前拒绝。

`status` 和 `inspect` 不推进计算、不转换文件。`pause` 让当前作业在原期限内完成并收集，然后停止后续步骤；`cancel` 请求终止整个受管进程树。新 CLI 启动不自动续算，只有 `resume` 才先对账再推进。失败输入不会自动重试，执行身份不足的崩溃窗口保留未知状态与额度，不能据没有句柄就重算。

`export RUN_ID --output E:\exports\delivery.zip` 显式导出已有报告、验证说明和获授权的原始文件，不调用模型或 ORCA。目标目录须已存在，文件不能覆盖历史；hash 损坏、越权或超限会拒绝整个导出。文件和字节上限及包内清单见[交付导出说明](docs/delivery-export.md)。

固定 Plan 可引用一个具体生产 Step 的 `optimized_geometry` 端口；消费前解析到具体尝试、Result、Artifact 和检查版本。未通过优化的最后一帧不能用作此端口。

## 资源、预算与保存

本地默认资源上限为 4 核、整个计算进程树 1024 MiB、ORCA 每进程 MaxCore 192 MB。Windows 后端在执行前原子绑定 Job Object，禁止脱离 Job，协调者消失后由系统清理；CPU 由 affinity 限制，内存指标是提交内存。

Linux 后端把任务 Python、内部 ORCA/MPI 与固定 helper 放入同一容器 cgroup，核验 CPU quota/cpuset、1024 MiB 内存上限、禁用 swap 和 PID 限制。容器无网络、根文件系统只读，不挂载宿主任务或 Store 目录；Python 另受禁止进程创建及网络调用的 seccomp 约束。keeper 在协调者心跳失联或绝对期限到达后退出，清理整个 PID namespace；身份或终止无法确认时保留租约。cgroup 内存与 Windows 提交内存的统计口径不同，二者都不能用 `%maxcore` 代替。资源账保留已观测下界；持久证据卷尚无硬磁盘配额，产物额度只约束收集与归档。

整个本机用户执行环境共用一个持久额度文件，位于 `%LOCALAPPDATA%\orca-agent\environment`，换项目目录或 Run 不能绕过它。生产 CLI 不提供更换环境额度位置的选项。环境未知占用须恢复对应 Run 并取得终止证据后释放。

每步最多 3 次尝试、每 Run 最多 4 次 ORCA 启动、额外启动最多 3 次；后处理预算为 0。SP 最长 300 秒、Opt 最长 900 秒、Run 最长 1800 秒。默认不自动重试，实际用例可选择更小上限。恢复和暂停不重置期限或累计用量；协调者丢失后不可获知的资源使用明确记录为未知，已有数字只保留下界。

`data/` 中保存 Request/Plan 修订、许可、预算、启动意图、进程身份、原始文件、独立尝试及带 SHA256 的 Artifact 快照。`geometry.xyz` 与 ORCA 作业 `job` basename 分离。原始文件读取与解析后校验归档一致性，损坏、冲突与未验证观察不会变成科学成功。读取缺 JSON 使用明确的同适配器文本能力；损坏/冲突 JSON 保守失败，绝不自动运行转换。

## 验证

```powershell
py -3.11 -m uv run --offline --locked ruff check .
py -3.11 -m uv run --offline --locked pytest -q
# Linux 测试读取显式冻结配置；每批使用新的 basetemp 路径：
$env:ORCA_AGENT_CONTAINER_CONFIG = 'E:\path\config.container.local.toml'
.venv\Scripts\python.exe -m pytest tests/live/test_container_execution.py --live-container --live-orca -q
```

默认测试禁止网络和外部科学程序启动，只允许标记的受管测试子进程验证系统协议。真实 ORCA 测试需要 `--live-orca`；Linux 容器测试另需 `--live-container`，两类开关独立，均不启用真实模型。测试会占用已声明资源并保留原始数据；缺少环境时标记跳过/未验证。历史参考复算依赖其原科学测试保存的证据索引，不使用被测输出反向生成参考值。仅显式运行真实生命周期测试才会强制结束它自己创建的协调者。

本工作副本已保存完整生产与参考证据，可单独执行参考测试以只读核验已有收据及 hash。不要先重跑科学测试再期待旧参考自动重绑：科学测试会创建新 Run，而旧参考仍绑定原 Run，届时将明确拒绝。新一批完整复算应在新的工作副本建立独立数据，保留本次历史证据，并继续共享本机全局计算额度。

## 项目依据

- [唯一现行总蓝图](docs/ORCA-Agent-Project-Blueprint.md)与[阶段 A 方案](docs/PHASE-A-IMPLEMENTATION-PLAN.md)。
- [开发约定](AGENTS.md)、[Windows 后端决定](docs/decisions/0001-windows-backend.md)、[严格优化与 JSON 范围](docs/decisions/0002-strict-optimization-and-json.md)。
- [冻结用例及独立参考](docs/acceptance/phase-a/cases.md)、[第三方版本和许可证来源](docs/acceptance/phase-a/dependencies.md)。

后续按用户逐项下达的详细方案实施，每次完成必要验证后提交并推送指定 Git 仓库，再等待用户验收。

## 优化后性质与只读追问（2026-10-09 局部交付）

本地网页 `http://127.0.0.1:8765` 使用同一执行循环。新建任务可输入：

> 优化水分子的几何结构，并给出优化后电子能和偶极矩，采用气相 RHF/STO-3G，中性单重态。

此原文已完成一次真实身份查询、OPI 初始几何生成和 ORCA 严格优化；同次计算获得能量和偶极矩，没有额外性质单点。结果区显示三个目标、科学检查、来源和原始文件。初始结构与优化结构分别保留。

在已有任务中输入问题后点 **只读追问**，建立关联原计算证据的查询任务，科学启动预算为零；“发送消息”仍用于补充原任务条件。追问独立计入模型调用和查询预算，不迁移原计算额度。`config.text.example.toml` 列出新增知识工具和文档查询限额，升级已有本地配置需同步这些字段；旧 Run 的冻结许可不会自动扩大。

基础知识回答与已有结果追问已各完成三次真实复核；五份版本化 ORCA/OPI 官方文档读取已接通，实际引用链完成三次，摘录未覆盖的输出细节会在网页显示范围说明。知识解释不产生合格科学数值。原文件自然语言追问的文件索引与协议已修复，但最终完整模型复核尚未通过；可在“原始证据”中直接查看文件。完整方案仍有未完成项，详见[本批交付与剩余清单](docs/acceptance/knowledge-query-completion/README.md)，此前失败见[上一批记录](docs/acceptance/web-properties-knowledge/README.md)。
