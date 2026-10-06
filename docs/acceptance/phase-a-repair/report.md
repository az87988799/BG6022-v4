# 阶段 A 最小修复验收记录

日期：2026-10-06，Asia/Hong_Kong。基线：`62a457b`（代码基线 `d68df8c`，新增修复方案）。状态：**修复交付，等待用户验收**。

本轮实施 [最小修复方案](../../PHASE-A-MINIMAL-REPAIR-PLAN.md) R-01～R-05 及必要的旧规则兼容。保留现有七对象、注册表、OPI 适配、文件存储和 Windows 后端；未扩展体系、方法、资源、预算、自动重试或执行平台。[原阶段报告](../phase-a/report.md) 与旧环境/证据索引保留，新结论独立记录。

## 修复与验收映射

| 项目 | 实际修复 | 对应回归 |
| --- | --- | --- |
| R-01 | Artifact 核验、输入准备、准备回执写入置于后端调用前的普通异常边界；失败回执明确 `not_started=true` / `handle=null`，attempt 和 Run 失败。只对暂停使用可恢复 `not_started` 状态。部分文件/收集错误保留，最低持久化失败时保守保留占用。 | `test_runner.py`：几何、输入、准备记录及 TypeError 注入；重复 resume 无重试；另一个 Run 可获得额度；部分文件与收集双失败；无法持久化；原硬崩溃未知占用。 |
| R-02 | 最终能量、坐标、SCF 标记绑定同一唯一片段，记录行号及片段范围；成功/明确失败/证据缺失分别为 passed/failed/unverified。后续阶段缺能量时，前值仅为观察，不能冒充最终值。 | `test_orca.py`：多周期末段缺 SCF、明确失败，一致/缺失/冲突 JSON；多候选、坏能量、缺坐标；末阶段缺能量；正常末尾结构报告不被误认成新计算。`test_science.py` 加强 Opt 限次局部能量及来源断言，但本轮未运行 live 测试。 |
| R-03 | 确认终止后补收原始文件，追加带 `supersedes_result_id` 的 Result，保留旧 Result/执行回执。仅接受同身份、合法关系且唯一末端的链；先 Artifact/Result、后 Run 引用、最后结算/释放。旧 finished unknown 可补收，不再次计费。 | `test_recovery_edges.py`：旧 unknown 已保存/未保存/已结算，归档可经 evidence 工具读取；新 Result 保存后再崩溃可重接；Run 保存后释放前再崩溃；多次 resume 幂等；分叉、环、跨 attempt、错 Step、无关联候选拒绝；缺损输出不伪成功。 |
| R-04 | 对账后保留 cancel，只清除本次 resume 对应且没有变化的旧 pause。终止未知时取消记录、未知状态和额度均保留；已完成事实不被迟到取消改写。 | `test_runner.py`：两步任务 pause → 离线 cancel → 重复 resume，总启动仍为 1；旧 pause 可继续；恢复期间新 pause/cancel 有效；在线取消与未知取消、迟到取消。 |
| R-05 | 共用纯版本声明同时用于诊断、启动准入和输出检查，仅启用精确 `6.1.1`。保留完整 token，重复/残缺/含混声明拒绝；冻结环境版本和输出版本必须一致。 | `test_doctor.py`、`test_orca.py`、`test_runner.py`：6.1.0/6.1.2/6.2.0、未知后缀、缺失/重复/混合残缺声明、输出/环境不一致；不安装或运行其他 ORCA 版本。 |

源码责任边界与版本关系见 [决定记录 0003](../../decisions/0003-check-version-and-recovery-results.md)。普通准备失败释放并发槽，但累计尝试和预占不返还；进程未知仍不能因缺 PID 而释放。

## 先复现、再修复

新增测试先运行于对应未修复实现，并保留失败日志。关键红测汇总：执行链 5 failed / 1 passed；科学片段 9 failed；版本及 schema 10 failed / 77 passed。独立审查再补两组反例：混合完整/残缺版本声明 2 failed / 13 passed；最后周期缺能量 2 failed / 1 passed。红测中通过项是原有正确行为或配对对照，不将其当成新增缺陷。

修后定向回归和完整离线回归的精确命令、通过数、跳过原因、日志 hash 见 [validation.json](validation.json)。正常 Windows 辅助子进程测试继续用于验证进程/故障协议，与科学执行分开计数。默认网络和科学程序启动禁令保持，原硬崩溃、未知额度、MPI 管理底层、hash 与只读边界没有被删除或放宽。

最终完整离线回归为 **303 passed、14 skipped、0 failed**，耗时 162.47 秒，比原 232 项通过增加 71 项。13 个 live 测试因未开启真实执行而跳过；1 个符号链接测试因 Windows 权限跳过，实际 junction 拒绝测试继续通过。Ruff 与离线 sdist/wheel 构建通过，wheel 内 20 个生产 Python 模块均逐字节匹配当前源码。远端 CI 状态需在推送后另行核验。

## 新旧检查版本

新建结构化 Request 的最低规则、科学 Tool 和新 Check 显式绑定 `orca-hf-2`。历史文件缺少版本字段仍读取为 `orca-hf-1`，新增 Result 替代关联默认空；没有将模型默认值直接改成新版。当前目标不接受旧版 passed，当前下游不消费旧规则结构，版本不匹配给出明确诊断。

历史 Run 可查看、取消、安全对账、补收；尚未完成的旧规则 Run 在新尝试预占前停止并提示需要复验。本轮未实现把旧 Run 无缝迁移为新版的流程；只读回放也不会自动授予继续计算的许可、替换目标或重置预算。

## 真实历史产物只读回放

[replay.json](replay.json) 为独立不可变复验记录，包含 15 个历史科学尝试（含 4 个参考复算），逐项关联原 Result、Artifact、原始文件 hash、旧端口、新检查/观察/片段定位和差异。每份记录还绑定本次源代码 hash。回放在禁止进程和网络调用的上下文内执行，调用尝试计数为 0。

| 历史证据 | 新检查器回放结论 |
| --- | --- |
| H₂O / CH₄ SP，各自独立参考 SP | 能量通过新规则检查，数值不变 |
| H₂O / CH₄ 严格 Opt，各自独立参考 Opt | 能量和按五项判据收敛的结构通过；不宣称极小值 |
| H₂O Opt → SP 两个具体尝试 | 两侧能量与优化结构检查保持，仍关联各自原尝试 |
| 真实 SCF 耗尽 | 没有合格能量 |
| 两次历史 Opt 限次 | 保留同片段合格局部 SCF 能量，没有优化结构 |
| 早期替代停止 Opt | 能量保留，五项门槛未全满足，仍无合格优化结构 |
| 首个开发水 SP | 新读取器可识别实际基组并得到能量；原 Result 的基组检查失败不改写。此解析修正已发生于原阶段开发，不算本轮新增科学成功；历史额外后处理的违规事实和成本继续保留。 |

这不是新 ORCA 运行，也不是新的独立数值参考。当前片段损坏/删除标记的测试在临时副本上进行，属于故障注入，不把它们登记为真实科学失败。原科学归档继续由 [旧 archive.json](../phase-a/archive.json) 定位；本轮不覆盖旧 ZIP、Request、Result、Run、原始产物及成本。

[integrity.json](integrity.json) 核对 1500 个既有历史文件均未变化；此后原阶段报告只追加修复链接，原字节完整保留为前缀，另记于 [report-link-append.json](report-link-append.json)。其余历史证据、环境及源码快照不修改。当前环境并发槽为空，回放没有取得科学执行额度。

本次没有修改 OPI 输入构造函数、原生进程创建/清理或 MPI 机制；验证记录包含与基线的函数/模块 AST 比较。因此本轮**新增 ORCA 启动为 0**，无需追加实际计算来证明本次解析及协调修复。真实四 rank、资源和清理能力仍只引用先前已记录的真实证据，不将离线辅助进程冒充 MPI。

## 复现与限制

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check .
# 以下显式只读回放依赖本机历史产物，输出必须使用尚不存在的新文件名：
.venv\Scripts\python.exe tests/helpers/replay_history.py --index docs/acceptance/phase-a/evidence-index.json --output data/repair-validation/replay-another.json
```

只读回放需要原目录或按来源路径恢复的历史归档；远端仓库不包含全部原始计算文件。复验文件不可覆盖；重做时使用新的记录文件名，原运行身份和预算始终不变。能力范围仍限于既有 HF/STO-3G 小体系，修复不等于扩大化学方法或体系保证。

本轮修复与证据已交付；阶段状态保持**等待用户验收**。
