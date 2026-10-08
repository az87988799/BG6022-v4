# 完整修复周期的执行管理范围

状态：P0/P5 离线执行管理准备。实际采纳记录已由协调者核对并固定为 [budget-approval-repair-cycle-20261008.json](../acceptance/phase-b/budget-approval-repair-cycle-20261008.json)，SHA256 `8e49c7e9151a7ce7bdcf6041533cb3f97713d43f244409010b2a3f394e0ddbef`；本次准备尚未迁移账本、冻结候选或执行新调用。用户的实际采纳文字为“按照方案开始修复，并完成刚才还没有完成的任务”，没有构造新的审批问答。

方案原件保存为 [2026-10-08-best-repair-plan.md](2026-10-08-best-repair-plan.md)，原字节 SHA256 为 `595bd302f2e0f5affc4f29e01f82de1b07c255d872dd82944550c04108655706`。本文件细化其 §9 的管理接口，不扩大分子、科学类型、单 Run 上限或执行并发。交付与恢复决定见 [0019](../decisions/0019-verified-terminal-delivery.md)。

## 唯一账本和授权链

周期标识为 `repair-cycle-20261008`。沿用 `data/phase-b/batch-ledger.json`，当前原始 SHA256 为 `b5b0efbfb7b8a805a904b08fbf3d35e7a325809c897bab3c7c68ede2ae039b13`：累计 324 HTTP、1018912 tokens、USD 0.4065609，ORCA reference/development/formal 为 16/26/0。新累计硬上限为 2068 HTTP、13882912 tokens、USD 20、ORCA 37/68/108/总213；新增查询、准备各22次，执行活动时间345600秒。

迁移直接衔接已应用 r3 批准和回执。r4 未批准、未迁移，不能伪造 r4 链。r1–r4 执行/准备/冻结入口关闭，历史审计和只读重新评分保留。旧所有已知、失败和未知占用均保留；改标签、候选、Run 或恢复不重置。

## 固定分项

| 分项 | 最大 HTTP / tokens | ORCA ref/dev/formal | 查询/准备 |
| --- | --- | --- | --- |
| 13个门槛，最多3个开发候选 | 192 / 1536000 | 0/0/0 | 0/0 |
| 原r4分层科学，6轨迹 | 48 / 288000 | 1/6/0 | 2/2 |
| 原C六类闭环 | 48 / 288000 | 0/16/0 | 0/0 |
| 条件D最多2条 | 16 / 96000 | 0/6/0 | 0/0 |
| 自主E2E开发，水/甲烷各3条 | 48 / 288000 | 6/6/0 | 6/6 |
| 有实际补丁的定向储备 | 48 / 384000 | 2/8/0 | 2/2 |
| 第一轮原正式矩阵 | 624 / 4704000 | 0/0/48 | 0/0 |
| 第一轮E2E正式 | 48 / 288000 | 6/0/6 | 6/6 |
| 第二轮原正式矩阵（条件） | 624 / 4704000 | 0/0/48 | 0/0 |
| 第二轮E2E正式（条件） | 48 / 288000 | 6/0/6 | 6/6 |

以上新增1744 HTTP、12864000 tokens、21 ref+42 dev+108 formal，禁止跨分项借额。单E2E Run暂保持8 HTTP/48000 tokens、一次科学启动、一次查询和一次准备；实际完整序列必须先估算，不能只提高批次额度绕过Run限制。

## 接口和失败边界

管理函数放在现有 helpers 下的局部 `phase_b_repair_cycle.py`，不增加生产对象或第二账本。分项槽位及执行活动预约嵌入原账本；既有 HTTP/ORCA 成本仍由原 `AcceptanceBudget`/`BatchLedger` 表及不可变回执结算。

- `scope()` 返回固定版本化范围；授权记录绑定其完整内容、原方案hash和实际用户文字。实际采纳 pin 已固定；pin 缺失或不符时所有写入/执行入口拒绝。尚未批准的正式增量不得修改该原始scope或pin。
- `apply_limits()` 沿既有受锁迁移链发布 before/receipt，仅增加批准限额及其authority；旧字段逐对象保全。周期管理区在后续实际绑定候选/槽位时才建立。
- `freeze_candidate(kind, number, repair_evidence)`：开发候选最多3、正式最多2。后继候选必须有不同已提交源码及根因、实际补丁、反例和通过的离线证据；同源码换标签不得重抽。候选冻结代码、配置、模型/profile/价格、依赖、二进制、scope、预算及最终manifest。
- `bind_slot(...)`：执行前绑定候选、固定分项、唯一槽、Run/store或独立reference身份，以及完整声明上限。按槽预留查询/准备名额并检查Run内对应上限，不通过未使用名额增加新的槽。旧槽重入只允许对账，不创建第二身份。
- `guard_reservation(...)`：低层HTTP、科学和独立reference预约再次检查同一周期、槽位、候选、分项及活动绑定。仅高层检查不足以关闭绕过入口。旧记录结算、收集与只读检查仍可进行。
- `activity(...)`：只包围实际执行调用，开始前预约活动秒、结束按实耗结算；人类等待、离线开发和审查不计入。崩溃未结算保守占用，先核对原执行状态，不重发。所有Run原deadline、资源和全环境单进程约束不变。
- `record_outcome(...)`：实际grade/review及来源hash绑定失败与共享依赖。普通开发失败关当前Run；同候选不共享缺陷依赖的零ORCA诊断可继续。失败影响不明确时保守阻断；相关科学不得启动，不能由自由文本宣称已修复解除。
- 来源冲突、未知HTTP费用/进程、越权、资源超限或共享科学/交付门禁失效，立即阻断关联执行并对账。普通失败不是新增重试许可。
- 第一正式轮任何必需门槛失败即整体失败；只有修复后的第二候选可用条件预留，完整重验manifest。每轮通过只能来自该轮同一候选，禁止跨轮补槽。第一轮通过不启动第二轮。

`tests/helpers/phase_b_repair_cycle_execution.py` 提供局部组合函数，复用同一模型、Tool、runner、reference 和原始科学评分链：

- `development_manifest(n)` 固定52个开发操作槽（含条件储备），`cycle.freeze_candidate("development", n, manifest=...)` 冻结该候选；`cycle.model_slot(candidate, variant, execute=True, live=True)` 执行一个实际门槛，随后必须独立review并调用 `record_model_outcome`。
- `layered_input(candidate, system, "resolve"/"prepare", execute=True, live=True)`：先完成两种身份查询，再分别准备；`layered_reference` 独立核对本次甲烷XYZ；`layered_science` 每种3条新模型/科学轨迹。水Opt保留原先明确的独立终态参考，不将其称为新XYZ的独立起点计算。
- `e2e_slot(candidate, system, repetition, execute=True, live_model=True, live_orca=True)`：纯文本创建新Run，不预填目标、Plan或XYZ。模型决定实际输入Tool和科学Step；在生产的科学预约前钩子退出同一Run，冻结实际prepared XYZ，执行一个独立SP/Opt参考，然后恢复原Run、原deadline和原待执行决定。引用输入、当前版本或deadline改变即拒绝继续。
- `joint_slot` 复用原C请求；每条新轨迹使用 `grade_trajectory` 与 `record_trajectory_outcome` 保存实际原答六轴、全部proposal事实/语义、终止收据绑定及独立科学评分。原始错误回答不能由正确的程序渲染结果改判通过。
- `formal_manifest(n)` 从新完整 `coverage-repair-cycle-20261008.json` 生成实际矩阵，保留每变体3覆盖槽及共享joint映射；`freeze_formal_candidate`、原 `phase_b_formal_ops.offline`、带formal候选的 `cycle.model_slot` / `joint_slot` / `e2e_slot` 复用同账本。第一轮正式冻结要求当前源码的开发门槛、分层、C和E2E均有实际通过记录；第二轮须实际前轮失败及新补丁，不拼接历史通过槽。

新增 SC-01 的实际正式矩阵已经重算为每轮核心 660 HTTP / 5136000 tokens。用户已批准[固定增补](2026-10-08-repair-cycle-formal-delta.md)，实际问答和完整提案保存于 [budget-approval-repair-cycle-formal-20261008.json](../acceptance/phase-b/budget-approval-repair-cycle-formal-20261008.json)，原字节 SHA256 为 `ccf7ca7f1572acd141a58c77734494565e663e51530ccae55eea12e50bfb07ea`。累计上限为 2140 HTTP / 14746912 tokens，USD 20 及其他额度不变。本批准尚未应用，原采纳 scope/pin、历史消耗及旧分项记录不改写；实际应用前正式冻结仍受原分项守卫约束。

`phase_b_cycle_formal_amendment.proposal()` 只读生成最终manifest对应的增补对象，提案文档固定为 `docs/reviews/2026-10-08-repair-cycle-formal-delta.md`；文档缺失时状态为 `doc_pending`。`apply(approval_path=..., approval_sha256=..., execute=True)` 仅供可信开发操作器在获得实际用户批准后调用，运行时模型没有该入口。批准JSON采用严格字段：`schema_version=1`、`approval_id=repair-cycle-formal-budget-20261008`、`status=user_approved`、逐字 `user_statement` / `question_text`、完整 `proposal` 对象；实际新批准已按上述独立记录固定，仍须在离线验证后显式应用。该操作把原账本当时的全部字节及显式传入的批准原字节复制为不可变before/approval/receipt，仅修改批准limits及authority。后续守卫递归核对已应用的批准hash和旧采纳链，原开发候选源码不会因写入新的批准文档而变化，不需要事后改硬编码pin。仅两轮原formal分项的HTTP/tokens可增，E2E、开发、ORCA、费用和资源上限都不能随之改变。

条件D和定向储备须先有实际修复证据，再用原输入/模型/执行链创建准确Run并 `bind_slot` 到候选中相应固定槽，不能任意增添请求或借用其它分项。已结束但未独立审查的槽会阻止后续调用；未知执行活动按预约全额保守占用，可用 `reconcile_activity` 绑定实际对账证据结算，不能返还机会或重复同槽。文件发布中断造成孤立回执时保守拒绝，须恢复准确原始状态后再对账，不能换身份跳过。

本设计及离线测试不构成真实验证、预算已应用或用户验收通过。
