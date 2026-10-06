# ORCA DFT Agent

阶段 A 提供不依赖 LLM 的本地计算底座：结构化 Request/Plan → 注册 Tool → OPI 输入 → Windows 受管 ORCA → 逐输出科学检查 → 不可变证据及结果。阶段 A 已于 2026-10-06 获得[用户验收通过](docs/acceptance/phase-a/user-acceptance.md)，接受的代码基线为 `236376e`；历史真实证据见[验收报告](docs/acceptance/phase-a/report.md)。

阶段 B 实施尚未开始，具体任务、范围和退出条件以用户逐次提供的方案为准。

R-01～R-05 的后续修复见 [修复验收记录](docs/acceptance/phase-a-repair/report.md)。新请求采用 `orca-hf-2`；旧 Request/Result 保留原规则和原字节。旧 Run 仍可查看、取消、恢复对账及补收，但未完成的旧规则 Run 不会直接继续计算，需另行明确复验；本轮未实现自动规则迁移。

当前科学范围是 **H₂O / CH₄ 组成、中性单重态、RHF/STO-3G、单点与无约束严格优化**。优化收敛只表示五项指定判据通过；未进行 Hessian/频率检查。DFT 泛函、更广体系、自然语言规划、自动修复、远程执行尚未实现。逐项状态见 [能力矩阵](docs/capabilities.md)。

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

## 结构化计算与控制

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
