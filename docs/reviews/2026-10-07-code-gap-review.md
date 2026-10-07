# 代码审查与缺口闭合方案

日期：2026-10-07（Asia/Hong_Kong）

审查基线：`6d8e584b0fc070e0876c7f34c96f885bb2ac6d68`

范围：生产代码、默认离线测试、阶段 A/B 契约及截至 v16-approved 的验收记录。

交付性质：审查与实施建议；本次不修改产品代码、不启动真实模型或 ORCA、不变更验收结论。

## 1. 审查结论

**当前应先闭合阶段 B 的正确性和可靠性，再进入远程或扩大科学能力。** 本次发现 5 个可离线复现的代码缺陷，其中 2 个影响目标或科学资格边界；另外确认了模型交付、真实科学闭环、正式重复验收及回归可移植性方面的未闭合项。

已有实现不能被误列为待建设：七对象、单 Agent 主循环、统一 Tool 注册、Windows 受管执行、累计预算、环境级单计算槽、不可变原始证据、逐输出检查、查询/导入/分析和确定性报告均有实质代码及测试。阶段 A 的历史用户验收记录保持原样；本报告没有重新授予或撤销任何历史验收。

| 编号 | 优先级 | 缺口 | 性质 |
| --- | --- | --- | --- |
| G01 | P1 | 初始语义目标可把明确要求的水绑定为甲烷 | 本次复现的代码缺陷 |
| G02 | P1 | 先成功、后失败的优化输出可能发布未收敛结构 | 本次复现的科学检查边界缺陷 |
| G03 | P2 | Request 修订后旧即时查询绑定遮蔽新 Plan 的有效结果 | 本次复现的代码缺陷 |
| G04 | P2 | 电荷/多重度的部分入口接受布尔值、浮点值并改写为整数 | 本次复现的输入契约缺陷 |
| G05 | P2 | “不执行计算”会误触发方法条件的否定检查 | 本次复现的语义可用性缺陷 |
| G06 | P1 | 最新模型配置仍不能稳定交付完整合格响应 | 已有真实失败，尚未闭合 |
| G07 | P1 | 左右采样追加、配对停止及完整模型解释缺少新版通过证据 | 部分实现、真实验证未完成 |
| G08 | P1 | 冻结版本的 210 个正式槽尚未执行 | 验收未完成 |
| G09 | P2 | 部分来源驱动回归依赖本地未提交归档 | 回归覆盖的可移植性改进项 |

P1 表示进入下一轮真实验收前应优先处理的正确性问题或阻塞项；P2 表示应随本地闭合补齐的问题。并非所有 P1 都是代码漏洞，表中已区分性质。

## 2. 审查方法与证据边界

- 对照唯一总蓝图的目标、Tool、反馈、证据、科学检查、恢复及验收契约，重点覆盖第 4—10、12—14 节。
- 并行审查运行/恢复链、科学/证据链、模型/语义与验收链；额外检查报告、CLI、配置和 CI。
- 对新增缺陷使用临时 Store、合成输入或 `ScriptedTransport` 复现；这些结果仅证明代码行为，不是真实模型或真实 ORCA 科学证据。
- 只读核对最新本地批次账本与 v16 检查点一致，没有清零、迁移、追加槽或改变历史失败。
- 默认离线全量测试与 Ruff 的本次结果见第 8 节。测试通过不等于未覆盖的反例不存在。

源代码链接固定到审查提交；后续行号变化时应按函数定位。历史模型结论引用仓库保存的原始评审，不把供应商价格、模型名称或版本说明当作本次联网核验结论。

## 3. 本次发现的代码缺陷

### G01 — P1：初始目标缺少对明确体系身份的核验

**位置：**[semantic.py:299—305](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/semantic.py#L299-L305)；对照同文件 [449—455](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/semantic.py#L449-L455)。

`_goals()` 只确认 `system_refs` 已登记且不重复，没有核对它是否符合该目标原文明确指定的体系。后续澄清绑定使用了 `_grounded_systems()`，初始目标创建路径没有等价约束。

**复现：**从原始 bundle 开始，不预填 Goal 或物理条件，登记 `water`、`methane` 两份合成 XYZ。用户原文为：

```text
Calculate the single point electronic energy of water using RHF/STO-3G
in gas phase, neutral singlet.
```

候选条件逐项使用正确原文依据，但目标提交：

```json
{
  "key": "energy",
  "port": "energy",
  "text_basis": "electronic energy of water",
  "geometry_relation": "fixed_initial",
  "system_refs": ["methane"]
}
```

生产 `initialize_bundle → commit_candidate` 接受该候选，保存结果为：

```text
normalization_status = normalized
Goal.original_text = electronic energy of water
Goal.system_ids = [methane]
Request.unresolved = []; Goal.unresolved = []
model_calls = 0; orca_starts = 0; permission unchanged
```

**影响：**规范 Request 已发生目标对象替换。后续按该 Request 校验 Plan，不能恢复用户原意。这里已经证实错误规范化；未运行真实计算，不能声称已发生错误 ORCA 启动。

**闭合方案：**在初始与替换目标路径，共用按“该 Goal 的原文证据、消息、登记体系及别名”核验身份的规则。明确冲突应拒绝；无法唯一确定时保留歧义并请求关键澄清。不能简单将整条消息提到的所有体系塞进每个 Goal，也不能只改提示词。

**验收：**水/甲烷配对错绑、只有不匹配体系、多个目标分别指向不同体系、中文/英文别名、歧义指代均有正反例；错误候选不激活 Request/Plan、不增加科学启动。对应 P07/P08、蓝图 4.1—4.3。

### G02 — P1：优化收敛证据没有绑定最终优化阶段

**位置：**[adapter.py:269—284](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/orca/adapter.py#L269-L284)、[checks.py:39—43](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/orca/checks.py#L39-L43)、[adapter.py:428—431](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/orca/adapter.py#L428-L431)。

文本解析使用全文件 `bool(opt)` 判断优化收敛，并从最后一次成功标记之前选阈值表；能量和末几何则来自最终片段。两组证据可能来自不同优化周期。

**复现：**在全新临时尝试中生成合成 stdout：第一周期五项阈值通过且有优化成功标记；随后第二周期改变一个 H 坐标，SCF 成功，但优化五项均为 `NO`，出现 `OPTIMIZATION DID NOT CONVERGE`，最后正常终止。`job.xyz` 与第二周期几何一致。使用受支持的无 JSON 文本读取路径，无 JSON 创建或删除，通过生产 `collect_result → read_outputs → check_outputs` 收集。

```text
qualified_ports = [energy, optimized_geometry]
all_structure_checks_passed = True
all_archived_hashes_match = True
diagnostics = []
ORCA_actual_starts = 0
```

**影响与限制：**首次归档前的异常多段输出可把后一个未收敛结构发布为合格结构；hash 一致并不能弥补解析绑定错误。这是合成故障反例，尚未证明固定正常 ORCA 单作业会自然生成该布局，也不能据此宣称历史真实优化结果错误。

**闭合方案：**在现有适配器内将优化周期、阈值表、成功/失败标记、最终能量、坐标和正常终止绑定到可证明的同一阶段；正确处理合法的 stationary-point 最终能量求值。存在后续失败周期或无法唯一绑定时扣留 `optimized_geometry`，保留原始观察及诊断。独立合格的局部能量可按原有能量契约处理。

**验收：**先成功后失败、后续不完整周期、缺坐标周期、相互冲突标记均不得发布合格优化结构；真实严格 Opt 及其最终能量求值正例继续通过；未合格结构不能进入下游 SP。记录科学资格影响及是否需新检查版本，旧检查记录不得覆盖。对应 P11、蓝图 8.5/9.1。

### G03 — P2：目标修订后旧即时查询绑定持续遮蔽新证据

**位置：**[store.py:935—939](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/store.py#L935-L939)、[runner.py:73—78](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/runner.py#L73-L78)；同类选择也见 [agent.py:169—179](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/agent.py#L169-L179) 和 [report.py:186—195](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/report.py#L186-L195)。

Request 修订只重置目标状态，没有失效旧的 `run.goal_evidence`。运行、决策及报告选择证据时，直接绑定仍优先于新 Plan 的结果。

**复现：**对 `{"a":1,"b":2}` 即时查询 `a` 并完成；通过 `enqueue_message` 与生产 `apply_user_update` 保持目标 ID、将路径改为 `b`；恢复后新 Plan 成功读到 `b`。离线脚本使用 `tests/unit/test_agent.py` 的 `make_run` 和 `ScriptedTransport`。

```text
first: completed, {'a': 'satisfied'}
second: failed, {'a': 'insufficient_evidence'}
fresh_value = 2; validate_goal_evidence(fresh_result) = True
stale_goal_evidence = True
```

**闭合方案：**修订时按新目标失效或重新核验当前直接绑定，保留历史 Result。集中实现证据选择规则，供运行、模型事实和报告调用；不适用的直接绑定不能遮蔽有效 Plan 结果，也不能按“最新结果”盲选。

**验收：**同 ID 改查询路径、物理量、条件及无关更新四类，覆盖即时/Plan 查询、连续两次恢复。新证据充分时目标与报告一致完成，历史证据仍可追溯，无重复查询或计费。对应 P07/P09、蓝图 6.2/8.5。

### G04 — P2：电荷/多重度严格类型约束在入口处被转换绕过

**位置：**[models.py:39—40](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/models.py#L39-L40)、[models.py:136—137](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/models.py#L136-L137)、[structured.py:53](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/structured.py#L53)。

`CalculationParameters` 的 `Literal[0]/Literal[1]` 接受数值相等的 `false/true`、`0.0/1.0`；Request 的普通 `int` 还会转换数字字符串。下游收到的已经是整数，严格类型检查无法识别原输入。

**复现：**复制水 SP 结构化请求至临时目录，把 `charge=false`、`multiplicity=true` 交给生产 `prepare_task`；保存的 Request/Plan 都变成 `[0,1]`，来源仍记为 `explicit`。自然 bundle 的原始 conditions 字典另有保护，本缺陷不能概括为所有入口均失守。

**闭合方案：**在类型转换前拒绝布尔、浮点和数字字符串；合法整数再执行范围约束。工具 schema 与执行校验继续来自同一参数定义，不另加一套同义协议。

**验收：**结构化请求、模型工具参数、Request JSON、用户修订均覆盖 `false/true/0.0/1.0/"0"/"1"` 的拒绝，合法整数正例不变；历史已规范化文件按既有历史策略读取。此项是输入契约缺陷，并未证明发生数值错误的科学计算。

### G05 — P2：执行否定被误当作科学条件否定

**位置：**[semantic.py:233—245](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/semantic.py#L233-L245)。

`_field()` 在包含条件值的分句中扫描任意否定词，没有区分否定对象。`Use RHF.` 可接受；以下同样明确指定 RHF、只禁止执行的原文却被拒绝：

```text
Use RHF/STO-3G for neutral singlet water but do not execute this calculation.
→ explicit condition has a negated or uncertain value basis
```

**闭合方案：**把否定依据绑定到具体条件命题；将“保留/登记条件但不执行”作为控制语义处理。含混的条件仍保持未决，不能简单删除否定检查或全面放行 `not/no/不`。

**验收：**“用 RHF 但不执行”“只登记中性单重态”“不要用 RHF”“尚未确定是否用 RHF”中英文配对；前两类条件可保存且零执行，后两类不能被登记为明确采用。对应 P08/P13、蓝图 4.2/4.4。

## 4. 已有实现但尚未闭合的验证缺口

### G06 — P1：模型交付与语义可靠性仍阻塞开发门槛

最新事实见 [v16 独立评审](../acceptance/phase-b/repair-thinking-v16-review.json)。N06 在 `thinking_low` 下的首发和一次协议纠正均为 HTTP 200，但都用满 2000 completion tokens，`finish_reason=length`，最终正文为空，没有任何已接受 Proposal。这证明交付/协议失败；没有正文可供判断，不能把它改称科学事实或语义错误。

相关代码：[llm.py:31](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/llm.py#L31)、[64](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/llm.py#L64)、[357—366](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/llm.py#L357-L366)。截断拒绝和用量结算本身是有效保护，应保留。

还存在协议表达的改进空间：[SemanticCandidate.questions](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/src/orca_agent/semantic.py#L87) 同时承载声明式范围通知和需用户回答的问题，策略靠文字区分。建议在现有局部候选 schema 中明确区分两者，并映射到对应的交付/等待状态；不增加长期领域对象。

**完成方案：**先完成 G01/G04/G05 与局部响应契约，压缩无关 schema/重复指令并验证有界上下文，固定一个可测试的 profile。输出上限保持现状时，不承诺仅靠提示就能解决；如果需要扩大单次 completion，必须同步记录决策、修改配置/校验/预算预约及评测契约，重新核算最坏成本。不能只提高整批 token 总额，也不能自动切换模式或接受截断 JSON。

当前 N06 机会已使用，最新包按失败条件停止；剩余额度不等于新的尝试许可。下一次真实验证须有明确的有限范围/机会安排。首先要求 N06 接受完整规范化及准确范围说明，独立事实/语义/交付评审通过，再验证其余四项诊断和原八项 raw 场景；不覆盖旧失败。

### G07 — P1：成功反馈再规划与解释质量尚无完整新版通过证据

见 [B-08/B-09 开发记录](../acceptance/phase-b/b08-b09.md)、[实施状态](../acceptance/phase-b/implementation-status.md) 和 [v15 评审](../acceptance/phase-b/repair-revalidation-v15-review.json)。已有真实三点计算、分析、SCF 修复成功及耗尽停止事实；左右追加闭环仍未通过，充分停止及耗尽解释仍有缺项/错误历史。v15 三项均未通过，新离线修复不能自动替代对应真实结果。

**完成方案：**在 G06 开发门槛通过后，使用固定代码/profile、原独立参考和原累计账本，完成六类科学开发：初始规划控制、SCF 修复成功、SCF 耗尽停止、左加密、右加密、充分停止；仅在实际科学代码改动影响范围内追加必要复验。

**验收条件：**

- 左右例均由三个初始 SP 的真实结果触发不同方向的合法 Plan 修订，再执行最多一个新 SP，最终分析满足有限采样目标。
- 充分例不追加；SCF 耗尽例不出现第三次启动；目标物理量、方法和检查标准不被降低。
- 逐条审查中间提案和最终解释；最终修正不能抹去首次事实错误。
- 解释覆盖物理量、单位、每个相关成员的条件、来源、限制、下一步；历史来源合格与当前适用性分别判断。
- 新证据如实记为开发类别，不能占用正式槽；不把合成反例算作真实科学证据。

### G08 — P1：正式冻结与三次独立重复仍未执行

[修订覆盖映射](../acceptance/phase-b/coverage-repair-v2.json) 当前为 **70 个变体 × 3 = 210 槽**：111 个模型槽、78 个离线故障槽、21 个联合变体槽；联合变体共享明确映射的 **18 条独立轨迹**，不是 21 次或 210 次 ORCA。最新检查点正式 ORCA 为 0，开发通过记录不能补正式槽。

**完成方案：**前述代码、模型和科学开发门槛通过后，冻结源码提交、依赖、有效配置、prompt/schema、用例、独立参考及累计预算。使用既有 `phase_b_freeze.py`、`phase_b_formal_ops.py`、`phase_b_acceptance_report.py` 完成现有流程，不重建评测框架。

**验收：**冻结的必需场景三次均达预期，首次成功率与纠正后成功率分别报告；每次独立身份、请求、响应、Result、原始产物、来源及成本可追溯。失败保留，并以新冻结版本重新评测受影响的完整必需集合，预算不清零。最后更新 README、能力矩阵和阶段报告，提交推送，等待用户验收。

### G09 — P2：来源驱动回归在干净检出中有缺口

[v16 干净检出回执](../acceptance/phase-b/repair-v16-approved-offline-clean.json) 记录 49 个用例因本地归档不存在而跳过；它们与 143 个未启用真实测试、1 个符号链接权限跳过不同。例如 [test_context_actual.py:22—24](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/tests/unit/test_context_actual.py#L22-L24) 和 [test_context_fact_contract.py:187—198](https://github.com/az87988799/BG6022-v4/blob/6d8e584b0fc070e0876c7f34c96f885bb2ac6d68/tests/unit/test_context_fact_contract.py#L187-L198)。

这些回放已明确设计为补充验证，仓库也有可移植合成反例，不能据此称 CI 全无覆盖。但干净 CI 无法自动重验全部已暴露的真实输入形状和跨记录组合。

**完成方案：**优先选取本次 G01—G05 与既有模型失败对应的最小必要样本，形成小型可移植 fixture；来源副本保留 provenance/hash，脱敏或结构改造后的样本明确标为派生回放，不再冒称原始字节。真正需要完整历史归档的检查单列为可选审计。禁止提交密钥、完整本地环境或大体积产物。

**验收：**新缺陷回归在无 `data/` 的检出、默认无网络环境稳定执行；核心用例不因本地归档缺失跳过。仍依赖完整归档的项目单列 `unverified`，不要求为消除 skip 去伪造历史证据。

## 5. 推荐实施顺序与每批退出条件

| 批次 | 范围与修改位置 | 依赖 | 退出条件 |
| --- | --- | --- | --- |
| W1 目标与输入正确性 | G01/G03/G04/G05；semantic、models、修订与证据选择 | 无真实环境依赖 | 新反例转为预期行为；合法输入/多轮/恢复回归通过；原证据和累计预算不变 |
| W2 科学资格绑定 | G02；统一 adapter/checks、必要决策及规则版本 | 可与 W1 的独立部分并行 | 合成多段失败不发布结构；真实历史严格 Opt 只读回放通过；标明是否仍需真实重验 |
| W3 可移植回归与模型协议 | G09、G06 的局部 schema/上下文/profile | W1；W2 纳入统一回归 | 干净检出离线门禁通过；明确问题/通知；严格 JSON、截断、费用及冻结反例通过；不新增长期对象 |
| W4 有界真实模型开发 | G06 的 N06、四诊断、八 raw | W3；有效的有限执行安排 | 所需开发场景逐条独立评审通过；任何失败按门槛停止并保留成本，不自动追加 |
| W5 真实科学闭环 | G07；必要的科学重验 | W2/W4 | 六类闭环及所需重验达标，来源/方向/停止/解释均有证据 |
| W6 冻结正式验收与交付 | G08 | W1—W5 | 210 槽及其独立轨迹按冻结契约完成；文档与证据一致；推送后等待用户验收 |

建议下一次实施先明确授权 **W1 + W2 + W3 的离线部分**，以可复现问题全部闭合为退出条件。真实执行单独按已确认的范围和机会安排推进；本报告本身不分配新机会，也不扩大预算。

避免以“增加提示词并继续试到通过”作为完成方案，也不为这次修复建设 Kernel、事件总线、数据库层或多 Agent 产品架构。

## 6. 预算与执行安排

本次只读核对本地 `data/phase-b/batch-ledger.json`，SHA256 为：

```text
e8ecff9891321d32bd7ee02208e12f4fedf96c8fe9f30c3426e27b65f1d524d9
```

与 [v16-approved 检查点](../acceptance/phase-b/repair-checkpoint-v16-approved.json) 一致：

| 项目 | 已记录累计 | 当前累计上限 | 说明 |
| --- | ---: | ---: | --- |
| 模型 HTTP | 318 | 1068 | 总余额不是额外 Run 许可 |
| 已知 tokens | 993584 | 6590000 | 未知 tokens 为 0 |
| 模型费用上界（USD） | 0.3941835 | 10 | 冻结计价估算，不是供应商账单 |
| ORCA 独立参考 | 16 | 16 | 不得重建账本获取新参考额度 |
| ORCA 开发 | 26 | 48 | 剩余预算不自动解除开发门槛 |
| ORCA 正式 | 0 | 48 | 尚未启动 |
| ORCA 总计 | 42 | 112 | 各类别不可任意重标 |

本审查新增真实模型 HTTP **0**、ORCA **0**。本地测试进程不属于科学计算。后续若调整模型输出上限/模式或增加尝试，须重新核算逐 Run 和整批最坏预约，并保留全部已用量及失败身份。

## 7. 蓝图中的后续能力：未实现，但不属于本轮缺陷修复

| 能力 | 当前状态 | 后续完成方案与最低退出条件 |
| --- | --- | --- |
| Linux/服务器/Slurm | 未实现；当前仅本地 Windows | B 完成后选择一种后端，复用 Tool/Run/预算/Artifact；真实提交、取消、断线、重复恢复和资源限制通过 |
| DFT/更多基组/溶剂及更广体系 | 未启用；当前 H₂O/CH₄、中性单重态 RHF/STO-3G | 按具体用例扩局部参数、兼容性、OPI 适配与检查；每个启用组合具备独立真实正反例，不按名称一次放开 |
| Freq/Hessian/热化学 | 未实现 | 先选有限频率用例，绑定结构/质量/方法/Hessian，检查模式、阈值、温度和标准态；自由能目标不能由电子能替代 |
| 光谱/响应、TDDFT/NMR | 未实现 | 每项独立定义输出、态/原子映射、科学保证及参考；不能仅因原始 JSON 有字段就宣称可用 |
| 构象、适应性扫描、TS/NEB/IRC | 未实现；当前有限离散采样不等于完整能力族 | 在所需科学前置后逐项实施，真实验证集合缺项、去重/探索边界、路径连接及有界成本 |
| 后台常驻/更大并发 | 未实现；当前前台协调是明确设计 | 有具体需求后另记生命周期决定，验证唯一协调者、全环境额度及失联对账；不作为 B 的补齐前置 |
| 版本化知识检索入口 | 蓝图包含方向，当前未建立独立检索模块 | 随已授权科学扩展接入少量版本化官方资料；资料只能提供数据，不能授予工具执行资格 |

不建议现在一次性实施上述全部能力；按蓝图 13.1，B 是后续远程和常用科学扩展的前置。

## 8. 本次验证记录与最终退出判据

| 验证 | 本次结果 | 能证明的范围 |
| --- | --- | --- |
| `py -3.11 -m uv run --offline --locked pytest -q` | **1888 passed、144 skipped**，598.84 秒，退出码 0 | 当前工作副本的默认离线回归；未启用真实开关 |
| `py -3.11 -m uv run --offline --locked ruff check .` | **通过**，退出码 0 | 当前源码与测试的静态检查 |
| G01—G05 的隔离反例 | 均复现当前缺陷 | 临时数据/脚本验证；没有修改测试断言把缺陷当作通过 |
| 真实模型 / ORCA / 远程执行 | **本次均未执行** | 历史真实记录仅只读引用，不产生新的真实验收结论 |

144 个跳过由 130 个真实模型/联合用例、13 个真实 ORCA 用例及 1 个 Windows 符号链接权限用例组成。与历史干净检出的 1839 passed、193 skipped 相比，本工作副本多执行了 49 个依赖本地归档的用例；两次结果不可相加，也不能将较高通过数解释成新增真实能力。

既有测试全部通过与 G01—G05 可复现并不矛盾：现有回归没有完整覆盖这些输入类型或组合路径。下轮应将这些隔离反例转换为固定回归，再修复代码。

本方案全部完成需要同时满足：

- [ ] G01—G05 的代码反例均按契约处理，正例不退化。
- [ ] 目标/物理条件/当前证据绑定保持一致，历史记录原样可追溯。
- [ ] 无法证明最终优化阶段收敛时，不发布 `optimized_geometry`。
- [ ] 模型开发门槛、科学闭环、正式重复验收分别通过，互不替代。
- [ ] 所有失败、跳过、未知、成本与限制如实保留；凭据不进入仓库或产物。
- [ ] 真实范围内的能力矩阵与代码、证据一致；后续能力继续明确未实现/未验证。
- [ ] 完成本批相关提交及推送核对，最终交付标注“等待用户验收”；只有用户接受后才记验收通过。

**本报告交付状态：等待用户验收。以上待办是闭合标准，并非本次已经实施完成。**
