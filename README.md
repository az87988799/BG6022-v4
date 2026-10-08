# ORCA DFT Agent

阶段 A 提供不依赖 LLM 的本地计算底座：结构化 Request/Plan → 注册 Tool → OPI 输入 → Windows 受管 ORCA → 逐输出科学检查 → 不可变证据及结果。阶段 A 已于 2026-10-06 获得[用户验收通过](docs/acceptance/phase-a/user-acceptance.md)，接受的代码基线为 `236376e`；历史真实证据见[验收报告](docs/acceptance/phase-a/report.md)。

阶段 B 已建立独立参考、受控修订、DeepSeek 适配与单一反馈循环，并完成首个自然语言请求到真实水分子单点的闭环，见 [B-04/B-05 证据](docs/acceptance/phase-b/b04-b05.md)。查询、导入、能差分析及受限 SCF 修复已有真实开发证据；采样追加和模型解释仍有失败，正式三次重复未启动，详见[当前实施状态](docs/acceptance/phase-b/implementation-status.md)。阶段 B 尚未完成或验收通过。

R-01～R-05 的后续修复见 [修复验收记录](docs/acceptance/phase-a-repair/report.md)。新请求采用 `orca-hf-2`；旧 Request/Result 保留原规则和原字节。旧 Run 仍可查看、取消、恢复对账及补收，但未完成的旧规则 Run 不会直接继续计算，需另行明确复验；本轮未实现自动规则迁移。

当前科学范围是 **H₂O / CH₄ 组成、中性单重态、RHF/STO-3G、单点与无约束严格优化**。优化收敛只表示五项指定判据通过；未进行 Hessian/频率检查。DFT 泛函、更广体系、远程执行尚未实现。逐项状态见 [能力矩阵](docs/capabilities.md)。

## 安装与环境

本机验证环境：Windows 10.0.26200、CPython 3.11.4、OPI 2.0.0、独立安装的 ORCA 6.1.1、MS-MPI 文件版 10.1.12498.18。Python 依赖由 `uv.lock` 固定，ORCA 安装包不随仓库分发。

```powershell
py -3.11 -m pip install --user uv==0.12.23
py -3.11 -m uv sync --locked
Copy-Item config.example.toml config.local.toml
# 编辑 config.local.toml 中本机 ORCA、MPI 的绝对路径。
.venv\Scripts\orca-agent.exe --config config.local.toml doctor
.venv\Scripts\orca-agent.exe tools
```

`doctor` 仅执行有期限的无输入版本探测；本机 ORCA 会打印版本后以退出码 2 报告找不到 `--version` 输入文件，该事实保留在报告中，并未提交科学计算。配置中的 MPI 目录实际加入子进程 PATH，核验时冻结可执行文件 hash。

项目仅启用精确的 ORCA `6.1.1`；未知版本后缀、重复或含混版本证据会拒绝准入。第三方要求的最低兼容版本不等于本项目已验证范围。

## 本地网页

现在可使用本地网页提交已支持范围内的自然语言任务、补充消息、查看条件来源、实际尝试、预算、确定性报告和原始文件，以及暂停、取消和显式继续。当前是[网页局部交付](docs/acceptance/local-web/report.md)，偶极矩合格输出、普通知识问答、联网检索、上传及完整真实网页计算验收尚未完成；阶段 B 原验收义务继续保留。

```powershell
py -3.11 -m uv sync --locked
# 首次使用且尚无本地配置时：复制后核对 ORCA/MPI 路径和有限许可。
Copy-Item config.text.example.toml config.local.toml
.venv\Scripts\orca-agent.exe --config config.local.toml web
# 浏览器打开 http://127.0.0.1:8765
```

已有 `config.local.toml` 时不要覆盖；仅启用所需的 `[text]` profile。密钥继续从后端进程的 `DEEPSEEK_API_KEY` 读取，不填入网页。`web --port 8766` 可选择其他本地端口；不提供公网监听或多 worker 选项。不带 `--config` 可启动只读历史页面，默认不允许提交新文本请求。

网页服务以前台方式运行。关闭或刷新浏览器不会重复提交或停止已启动的任务；退出前台服务会请求暂停，在原期限内收集当前任务后退出。需要立即停止计算时先点“取消”并核对状态。服务重启不自动执行任何 Run，继续须点击“继续处理”。暂停/取消按钮显示收到请求，实际完成状态另行展示。

后续消息只入原 Run，空闲时点击“继续处理”消费；不会重置已用模型/科学额度或期限。待处理队列最多 24 条，历史最多 512 条并分页保留。服务忙碌时新任务可以登记，界面会提示稍后明确继续，不建立隐式执行队列。相同提交编号或消息编号重发不重复创建或消费；中断且身份不明的登记保守拒绝重建。

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

将输出中的实际 ID 替换示例占位符；字段示例须使用 property JSON 的 Artifact ID，键名区分大小写。`run` 表示明确授权执行该文件中的有限科学任务；几何必须位于请求文件所在目录内。条件缺省来源会保留，所有持久 ID 由程序生成。未知工具、任意脚本/原生输入、非法电子态、越界文件、循环依赖及超资源请求在执行前拒绝。

`status` 和 `inspect` 不推进计算、不转换文件。`pause` 让当前作业在原期限内完成并收集，然后停止后续步骤；`cancel` 请求终止整个受管进程树。新 CLI 启动不自动续算，只有 `resume` 才先对账再推进。失败输入不会自动重试，执行身份不足的崩溃窗口保留未知状态与额度，不能据没有句柄就重算。

固定 Plan 可引用一个具体生产 Step 的 `optimized_geometry` 端口；消费前解析到具体尝试、Result、Artifact 和检查版本。未通过优化的最后一帧不能用作此端口。

## 资源、预算与保存

本地每次计算最多 4 核、整个进程树总提交内存 1024 MiB、ORCA 每进程 MaxCore 192 MB。Windows Job Object 原子绑定发生在进程执行前，禁止脱离 Job，协调者消失后由系统清理。CPU 由 affinity 限制；内存指提交内存，不能与工作集或 `%maxcore` 等同。

整个本机用户执行环境共用一个持久额度文件，位于 `%LOCALAPPDATA%\orca-agent\environment`，换项目目录或 Run 不能绕过它。生产 CLI 不提供更换环境额度位置的选项。环境未知占用须恢复对应 Run 并取得终止证据后释放。

每步最多 3 次尝试、每 Run 最多 4 次 ORCA 启动、额外启动最多 3 次；后处理预算为 0。SP 最长 300 秒、Opt 最长 900 秒、Run 最长 1800 秒。默认不自动重试，实际用例可选择更小上限。恢复和暂停不重置期限或累计用量；协调者丢失后不可获知的资源使用明确记录为未知，已有数字只保留下界。

`data/` 中保存 Request/Plan 修订、许可、预算、启动意图、进程身份、原始文件、独立尝试及带 SHA256 的 Artifact 快照。`geometry.xyz` 与 ORCA 作业 `job` basename 分离。原始文件读取与解析后校验归档一致性，损坏、冲突与未验证观察不会变成科学成功。读取缺 JSON 使用明确的同适配器文本能力；损坏/冲突 JSON 保守失败，绝不自动运行转换。

## 验证

```powershell
py -3.11 -m uv run --offline --locked ruff check .
py -3.11 -m uv run --offline --locked pytest -q
# 真实测试不读取 config.local.toml，安装路径通过以下变量指定：
$env:ORCA_AGENT_ORCA = 'E:\orca\orca.exe'
$env:ORCA_AGENT_MPI = 'C:\Program Files\Microsoft MPI\Bin\mpiexec.exe'
# 仅在新工作副本尚无 data/acceptance 证据时，按顺序建立一批真实证据：
.venv\Scripts\python.exe -m pytest tests/live/test_science.py --live-orca -q
.venv\Scripts\python.exe -m pytest tests/live/test_reference.py --live-orca -q
.venv\Scripts\python.exe -m pytest tests/live/test_lifecycle.py --live-orca -q
```

默认测试禁止网络和外部科学程序启动，只允许标记的受管测试子进程验证系统协议。真实测试需要 `--live-orca`，会占用已声明资源并保留原始数据；缺少安装时标记跳过/未验证。参考复算先依赖科学测试保存的证据索引；不使用被测输出反向生成参考值。仅显式运行真实生命周期测试才会强制结束它自己创建的协调者。

本工作副本已保存完整生产与参考证据，可单独执行参考测试以只读核验已有收据及 hash。不要先重跑科学测试再期待旧参考自动重绑：科学测试会创建新 Run，而旧参考仍绑定原 Run，届时将明确拒绝。新一批完整复算应在新的工作副本建立独立数据，保留本次历史证据，并继续共享本机全局计算额度。

## 项目依据

- [唯一现行总蓝图](docs/ORCA-Agent-Project-Blueprint.md)与[阶段 A 方案](docs/PHASE-A-IMPLEMENTATION-PLAN.md)。
- [开发约定](AGENTS.md)、[Windows 后端决定](docs/decisions/0001-windows-backend.md)、[严格优化与 JSON 范围](docs/decisions/0002-strict-optimization-and-json.md)。
- [冻结用例及独立参考](docs/acceptance/phase-a/cases.md)、[第三方版本和许可证来源](docs/acceptance/phase-a/dependencies.md)。

后续按用户逐项下达的详细方案实施，每次完成必要验证后提交并推送指定 Git 仓库，再等待用户验收。
