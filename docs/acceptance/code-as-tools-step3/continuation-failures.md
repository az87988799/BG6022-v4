# 步骤三继续实施：真实失败记录

本文件保留扩容后的开发失败；不同候选不能合并为同一冻结版本的正式验收。用户于 2026-10-19 将整批上限提升到 50 个 Run，原有八次预约及失败保留。

## live009：初始结构成功，后续决策停滞

Run `run_acb8d2a0e9ed400896f23533af95d9f3` 仅接收原自然语言任务，真实模型自行登记目标、选择 B3LYP/def2-SVP 并建立初始 Plan。身份查询及 OPI 结构准备成功，初始几何目标满足；优化和振动频率目标仍缺少证据。没有写出任务脚本或 ORCA 输入，Python Attempt、内部调用及 ORCA 启动均为零。

准备完成后的三次模型响应均重复请求即时调用已完成的 `structure.resolve`，理由误称它仍是待执行 Step。程序按契约拒绝，最后有界纠正耗尽，Run 状态为 `failed`，交付为 `partial`。当时发送上下文将 resolve 展示为完整普通 Step，却只将 prepare 标为 `immutable_frozen`；空待执行列表被省略。这个可复核的上下文缺陷增加了误判风险，但不能据此保证修复后模型一定成功。

本次共 13 次真实模型调用，输入 119658、输出 2560，合计 122218 token；未知 token 为零。按冻结的不含缓存折扣费率估算 USD 0.0389694，非实际账单。真实结构准备使用 Windows JobObject 的 1 核、1 GiB 上限，耗时 1.657 秒、CPU 1.3125 秒、峰值提交内存 65609728 字节，结束时活动进程为零。该成本与零次生成任务执行分别记录；结构准备不代表 ORCA 科学计算。

候选于 **2026-10-19 11:51:06（香港时间）**冻结；在 Run 失败后、随后修复前，于 **11:53:00** 独立核对全部 62 个冻结源码/依赖文件及驱动，均与冻结哈希一致。此事后核对只覆盖该时点，不将后续工作区修复算作原 Run 的实现。

原驱动已保存 `report.json`，随后 `render_report` 因 `KeyError: observation` 退出，因此原 `report.txt` 和 `outcome.json` 未生成。修复渲染器后另行读取已保存 JSON 得到的报告仅为只读重渲染，**不算原 Run 恢复、原模型最终交付或自主计算成功**；没有补造原 outcome。

完整的小型事实、原请求/响应引用与哈希见 [live009 证据](validation/live009-preparation-and-stall.json)。本次没有真实 DFT 输出、生成代码修复或完整 Opt+Freq 交付证据。

## live010：初始 Plan 未通过准入

Run `run_ff9acb1e7c854a8fb5b5a9bec1732c8e` 完成语义登记及有来源的 B3LYP/def2-SVP 选择，但三份初始 Plan 均被拒。第一、第三份包含 `python.execute`，却未提供必须已存在的 code Artifact；中间一份改为准备和写文件步骤，却把未来的初始几何写成全空字段的 `EvidenceRef`，整个提案未接受。最后有界纠正耗尽，状态为 `failed / partial`，所有目标未满足。

中间提案确实包含模型自行生成的 Python 和 ORCA 输入文本，不能称为“没有生成过代码”；但没有执行 `workspace.write`，未创建这些 Artifact，也未执行任何身份查询、结构准备、Python Attempt 或 ORCA。对未执行文本的只读检查发现 ORCA 输入缺少显式 `RKS`，频率解析未严格限定频率块；这属于潜在文本问题，不能记作真实执行或数值核验失败。

共 9 次真实模型调用，输入 76071、输出 3356，合计 79427 token，未知为零；估算 USD 0.0268485，不含缓存折扣且非账单。原驱动结束记录的源码变化为空，独立事后比较也确认 62 个冻结文件及驱动一致。具体冻结、完成及独立核对时刻保存在 [live010 证据](validation/live010-planning-failure.json)，不将随后修复混入原候选。

本次原始 `report.json`、`report.txt`、`outcome.json` 均正常保存，`report_error=null`，live009 的渲染异常没有复发。这证明失败采证和报告路径通过本次真实调用，不代表自主科学交付通过。没有真实 DFT、生成代码执行或修复证据。

## live011：默认条件未登记，停在多余澄清

Run `run_9bd6283cff824de9bd441b4bf5fc1e36` 的首份语义提案被程序接受，但完整省略了 `conditions`。模型理由称电荷 0、单重态、RKS、气相默认已经生效，实际持久化 Request 中这些条件仍为空或缺失；`semantic_defaults` 仅保留可用模板。方法及基组的自动选择授权正确保留，尚未执行选型。

下一次发送上下文因此只允许 `clarify / stop`，模型再次询问用户是否授权选择方法和基组，并要求确认默认条件，最终为 **`waiting_user / partial`**。这不是执行失败或纠正额度耗尽，也不是完成交付；没有 Plan、Tool 调用、结构、脚本或 ORCA。

共 2 次模型调用、6757 token，未知为零，估算 USD 0.0027930。冻结时间为 2026-10-19 **12:44:16（香港时间）**；Run 返回后、下一轮修复前，于 **12:45:19** 核对 62 个冻结文件及驱动均一致。原 outcome 的源码变化为空，原报告及 outcome 正常保存。原始两份提案、真实 Request 条件、费用与来源哈希见 [live011 证据](validation/live011-default-registration-omission.json)。

## live012：初始结构成功，证据引用修订失败

Run `run_0bd22cf67623455aaa26158eb93f1b6a` 的模型显式登记了默认电荷 0、单重态、RKS、气相，并在查询官方资料后选择 B3LYP/def2-SVP。首份 Plan 因缺少已存在的 code Artifact 被拒；模型改为分阶段 Plan，真实身份解析及 OPI 初始结构准备均成功，初始几何目标满足。优化和频率目标仍缺证据。

准备完成后的第一份修订遗漏了已满足的初始几何目标；后两份虽补回该目标，却仅用 `artifact_id/sha256` 充当 `EvidenceRef`，没有所需的 Run/Result 身份，最终有界纠正耗尽，状态为 `failed / partial`。后两次实际请求均完整带回上一份被拒候选，内容与原始响应一致；因此本次失败不能再归因于缺少前次候选。当前问题是目标证据引用的语义和契约表达。

这三份未接受修订还将初始 XYZ 当作 `orca.run` 的原生输入，并未写出 Python 或 ORCA 输入 Artifact。没有 `workspace.write`、Python Attempt、内部调用或 ORCA 启动，也没有优化结构、Hessian 或频率输出。XYZ 输入误用是未执行提案中的问题，不能记作已发生的 ORCA 执行失败；初始几何资格不等于科学结果通过。

共 13 次真实模型调用，输入 125130、输出 3771，合计 128901 token，未知为零；记录估算 USD 0.0420642，非实际账单。结构准备采用 Windows JobObject 的 1 核、1 GiB 上限，耗时 1.656 秒、CPU 1.171875 秒、峰值提交内存 65654784 字节，结束时活动进程为零。该成本单独保留，不能被零次生成任务执行的计数掩盖。

冻结于 2026-10-19 **12:52:42（香港时间）**；Run 结束后、后续修复前，于 **12:53:55** 独立核对 62 个冻结文件及驱动均一致。原 `outcome.json`、`report.json`、`report.txt` 正常保存，源码变化为空、`report_error=null`。原提案、纠错来源、准备资源与证据哈希见 [live012 证据](validation/live012-evidence-reference-failure.json)。本轮仍未验证真实 DFT、生成代码修复和完整 Opt+Freq 交付。

## live013：修订提案与持久化 Plan 字段混用

Run `run_df0604c3be3e44ebaaa36758873a97c4` 在冻结的 v43 提示下完成条件登记、有来源的 B3LYP/def2-SVP 选择、身份解析及初始结构准备。初始几何目标满足，但优化和频率目标仍缺证据，最终为 `failed / partial`。

准备后的第一份修订直接使用持久化 Plan 的 `id/logical_id/immutable_frozen` 和 Goal 的 `step_id`，并填入不存在的脚本 Artifact 与占位 hash；第二份改用 Step 的 `key`，仍保留错误 Goal 字段和原生输入占位引用。第三份生成了 ORCA 输入文本，但 `EvidenceRef` 同时混入未来生产 Step 和具体 Run/Result/Artifact 身份，另以未来 `producer_key` 代替必须已存在的输入文件引用。程序拒绝这些提案，纠正额度耗尽。

实际发送的 `ACTION_SCHEMAS.revise_plan.steps.items` 只有 `type:object`，没有展开提案 Step 字段；同一上下文的历史 Plan 使用持久化字段，系统又要求保留已满足目标的原绑定。这是可复核的契约展示缺口，不能据此保证后续修复必然成功。前次候选完整回传及 EvidenceRef 的新互斥约束均已实际发送，本轮不能归因为这些信息缺失。

最终候选中确有模型生成的 ORCA 输入文本，但没有执行文件写入，未产生对应 Artifact，也未生成或执行 Python。该未执行文本省略显式 RKS，并选择直接 `orca.run`，尚无同一 Python Attempt 的计算和解析来源链；这些是提案问题，不是已发生的 ORCA 运行失败。Attempt、内部调用、ORCA 启动均为零，没有科学输出可独立核验。

共 11 次真实模型调用，输入 105132、输出 3772，合计 108904 token，未知为零；记录估算 USD 0.0360660，非实际账单。结构准备使用 Windows JobObject 的 1 核、1 GiB 上限，wall 2.203 秒、CPU 1.484375 秒、峰值提交内存 65671168 字节，结束时活动进程为零；与零次生成任务执行分别列账。

候选冻结于 2026-10-19 **13:10:49（香港时间）**；结束后、后续修复前，于 **13:12:11** 独立核对 62 个冻结文件及驱动均一致。原 outcome 和两种报告完整，源码变化为空、`report_error=null`。具体响应、反馈、上下文缺口和成本来源见 [live013 证据](validation/live013-plan-proposal-shape-failure.json)。

## live014：任务文件已生成，反复读取后预算停止

本轮前瞻采用已有 `thinking_low` 配置，输出上限由 8192 调至 16384 token、传输期限调至 180 秒；每 Run 24 次模型调用、120 万 token、2 次 ORCA、400 工作量上限不变。配置变化随新候选冻结，不回写先前运行，也不构成同版本重复通过。

Run `run_af2fbef621234408a20e1a9406f952bf` 自主选择了 **B3LYP/6-31G**，与此前候选的 def2-SVP 不同，选型依据另有官方来源。身份和初始结构准备成功；模型通过 Plan 修订实际写入了 252 字节的 ORCA 输入与 2802 字节、67 行的 Python 脚本，两者均有不可变 Artifact 和真实 hash。这是模型自主生成文件的新证据，但尚未执行 Python 或 ORCA。

随后完成十次读取：七次读 Python、一次列目录、一次读 ORCA 输入、一次读初始 XYZ。Tool 读到脚本全文时，下一模型上下文只显示 1–22 行，并标记 `next_line=23`；一次从 23 行续读返回 45 行，但上下文仅显示 23–39 行，标记 `next_line=40`，先前页已消失。模型随后又读回第一页。逐次实际请求中，每个包含 evidence Result 的上下文都仅保留一个此类 Result；切换到其他文件后脚本页也不再保留。列目录结果则被整体压缩为 `bounded query required`，没有展示 Artifact 元数据条目。

这些记录证明了后续读取上下文的保留缺口和反复读取，不表示模型从未生成完整脚本，也不保证修复读取展示后即可成功。最后一次模型又请求从第 23 行读脚本；该 Tool 已完成，但没有下一次 HTTP 消费结果。三个将 `tool` 错放响应顶层的格式提案曾被拒并继续纠正，它们不是最终停止原因。

最终状态为 **`budget_exhausted / partial`**，诊断为最终解释缺少剩余调用/token/决策/时间预算；保留原诊断，不将 24 个决策轮误记为 24 次 HTTP。实际为 23 次模型 HTTP、输入 261674、输出 41596，合计 303270 token，未知为零，估算 USD 0.1284174，非账单。未生成模型最终解释，确定性报告正常保存。Attempt、内部 ORCA 回执、ORCA 预约和启动均为零；记录中的两次 `analysis_executions` 对应文件写入，不是两次 Python 执行。

结构准备另记 Windows JobObject 的 1 核、1 GiB 上限，wall 2.406 秒、CPU 1.765625 秒、峰值提交内存 65683456 字节，结束时活动进程为零。候选冻结于 2026-10-19 **13:19:35（香港时间）**，结束后、后续修复前于 **13:25:19** 核对 62 个冻结文件及驱动均一致。原 outcome、两种报告完整，源码变化为空、`report_error=null`。

原生成文件、每次实际可见行号、读取回执、费用与哈希见 [live014 证据](validation/live014-read-context-budget-exhaustion.json)。没有真实 B3LYP/6-31G 输出可作独立核验，也未建立实际代码错误修复或完整 Opt+Freq 交付证据。

## live015：脚本已写入，读取参数三次未完成纠正

Run `run_206d6dd02c4f4f13a07cdc806a0b81da` 在冻结的 v45 下接受原文请求，自主选择 **B3LYP/def2-SVP**，完成身份解析与初始结构准备，并在第一版 Plan 中写入 2577 字节的 `ethanol_optfreq.py`。脚本包含模型生成的 ORCA 输入文本；尚未执行脚本，因此该输入没有被写成计算文件。原模型提案、writer Call 和 Artifact 的代码字节完全一致，未由开发者补写或修订。

一次 `evidence.list` 成功返回十件 Artifact。最后三次实际模型上下文都保留了与原 Result 完全一致的十件元数据、coverage 和 inventory hash；v45 的目录展示在本轮已有真实证据。但后续三次 `evidence.text` 提案均被拒：第一份使用压缩键 `b`、`start_line=0`、`lines=250`；第二份改为真实键 `artifact_id`，仍为 0/250；第三份将行数改为 200，起始行仍为 0。三份均未成为实际读取 Call。

原始请求中的 schema **没有丢失数值边界**：`start_line` 的 1–10000 和 `lines` 的 1–200 均以 `SHARED_STRINGS` 字面对象引用保留。实际反馈只给出字段和 `greater_than_equal/less_than_equal` 类别，没有直接给出阈值；前一份被拒候选则完整、准确回传。第一份回复直接复制压缩参数名，后两份未修正起始行，表明压缩协议可读性与弱反馈是可改进点，不能将本次失败描述为 schema 缺 minimum 或运行时边界不一致。此前另一次 146 字符的知识查询因过长被拒，缩短到 77 字符后成功；其原生 schema 确实省略字符串长度约束，与最终数值边界问题分别记录。

最终为 **`failed / partial`**，直接诊断是有限提案纠正额度耗尽，尚未耗尽本 Run 全部模型调用额度。共 13 次 HTTP、13 个决策轮，输入 134967、输出 21718，合计 156685 token，未知为零；估算 USD 0.0665517，非实际账单。初始身份和几何目标满足，优化与频率仍缺证据。一次 `analysis_executions` 是写入代码，不是 Python 执行；Attempt、内部调用、ORCA 预约与启动均为零，没有模型最终解释，原 outcome 和两种确定性报告保存成功。

结构准备另记 Windows JobObject 的 1 核、1 GiB 上限，wall 2.078 秒、CPU 1.53125 秒、峰值提交内存 65527808 字节，结束时活动进程为零。候选冻结于 2026-10-19 **13:40:40（香港时间）**，结束后、后续修复前于 **13:44:54** 独立核对 62 个冻结文件及驱动均一致。原 Run 的 85 个文件在本次只读审计前后 hash 不变。

原始提案、实际 schema 池、目录展示验证、成本与文件 hash 见 [live015 证据](validation/live015-read-proposal-failure.json)。本轮没有成功文本读取可验证多页保留，也没有真实 DFT 输出、代码运行错误修复或完整自主交付证据。

## live016：首次实际 Python 失败，修复停在计划纠正

Run `run_bf6d525d6c6546eab4bb54e2e3119ef7` 在冻结的 v46 下自主选择 **B3LYP/6-31G**，完成初始结构准备、写入 2756 字节的 94 行 Python，并在第二版 Plan 中用真实 Artifact/hash 和 XYZ manifest 启动首个任务。模型原提案、writer 文件、代码 Artifact 和容器 sealed 脚本字节一致，没有开发者补写 Plan、输入或程序。

Attempt `attempt_5ceae1603ff0443f9ae5c382133be2b5` 在受管理 Linux 容器实际启动，随后退出 1。脚本将绝对几何路径传给 `run_orca(..., dependencies=...)`，受管理 SDK 以 `manifest target must be an unambiguous relative task path` 拒绝；这是一次真实任务代码错误和一次被拒的 SDK 请求，但没有生成已接受的 InternalCall，ORCA 预约和启动均为零。原输入另缺 `Opt Freq`，尚未运行到 ORCA，不能把该静态缺项称为已发生的 ORCA 错误。

第 16 次模型调用自主读取失败 stderr；第 17 次真实上下文同时保留原脚本 94 行和 stderr 14 行。模型据此生成相对依赖路径及新 request_key 的修正版，但遗漏已满足的结构 Goal，提案被拒。第 18 次模型自行补上 `Opt Freq`，同时错误地将刚读 stderr 的 Result 当作结构证据，且只提供 result_id、缺 run_id，再次被拒。程序没有替它补源 ID。第 19 次试图保留原准备 Step，却将请求版本、权限版本、reason、parameters 等写成压缩代号，最终耗尽提案纠正额度。两份修复代码都只存在于原模型回复，没有写入 Artifact，也没有第二次 Python Attempt。

后续真实投影暴露另一容量问题：第 18 次退到完整 Proposal 协议，脚本仅显示 26 行、stderr 仅 11 行，导致第 12–13 行的实际错误原因不可见；第 19 次进一步显示脚本 12 行、stderr 6 行，前一被拒候选也标为不完整。Result 身份和 Tool 的原 coverage 仍保留，不能描述为读取记录全部消失。native schema 的字段保护也没有覆盖全部响应 envelope 和旧协议退路，最后回复确实复制了其中代号。这些事实不保证后续提示修复一定成功。

预算沿用同一 Run：累计一个 logical Python Attempt、零次已消耗 code retry、零次 ORCA。失败后每轮仍展示一次代码重试和两次 ORCA 的剩余额度；最终停止原因是计划提案纠正耗尽，不是代码重试成功或重试次数耗尽。共 **19 次 HTTP、287998 token**（输入 214540、输出 73458），未知为零，估算 USD 0.1525116，非账单。最终 `failed / partial`；初始几何目标满足，优化和频率缺证据，未生成模型最终解释，原 outcome 及两种确定性报告完整。

任务容器记录 4 核、1 GiB cgroup 上限，wall 2.75 秒、CPU 0.954549 秒、峰值内存 48144384 字节，OOM 为否；`resource_usage_complete=false`，这些成本仍是终止前最后采样下界。结构准备另记 Windows JobObject 的 1 核、1 GiB 上限，wall 1.844 秒、CPU 1.390625 秒、峰值提交内存 65601536 字节。任务生成的 native input 为 280 字节 LF；SDK 接收端副本为 301 字节 CRLF，仅换行不同，两份原 hash 分别保留，不声称字节相同。

冻结于 2026-10-19 **13:53:31（香港时间）**，结束后、后续源码修复前于 **14:01:16** 独立核对 62 个冻结文件与驱动均一致；原 Run 的 141 个文件在完整审计前后 hash 不变。原脚本、stderr、SDK 拒绝、修复提案、预算时点、上下文窗口与成本见 [live016 证据](validation/live016-python-failure-and-repair-proposal-stall.json)。实际失败和反馈链已建立，但代码修复重跑、真实 ORCA、独立科学核验及完整自主交付仍未完成。


## Live018：受控故障尚未执行，Plan 修正响应截断

Run `run_6851a458680a4dbd96ad82f91a6662d2`，v47/thinking_low、64k 输入/16,384 输出上限，于 2026-10-19 06:42:35 UTC 结束 `failed/partial`。详见 [证据索引](validation/live018-seeded-prelaunch-truncation.json)。本轮在原始乙醇请求后明确附加受控故障说明，与 [live017 原始请求自主成功](validation/live017-autonomous-optfreq.md) 分开统计。

结构准备和 workspace.write 完成。模型第 7 次响应生成的 3,145 字节 Python 与写入 Artifact 完全一致：合法模块文档字符串后为独立未定义名称 `CODE_TOOLS_DEVELOPMENT_FAULT_V1`，随后才导入。没有 Python Attempt、内部 ORCA、实际 ORCA 或真实 NameError；seed gate 为 `not_established`，原因是尚未到达 before_science_reservation。`analysis_executions=1` 仅表示写文件。

第 10 次提案遗漏已完成 resolve Step，同时保留了依赖它的 prepare Step；后验纯图校验重现 `dependency references an unknown step`。实际反馈只有 `value_error`、空 `loc`。第 11 次上下文完整回传此前 1,828 字节候选，但 prepare 的冻结显示省略 `depends_on`；完整 native 协议字段仍在。下一响应 `finish_reason=length/error=truncated`，消耗 16,384 completion tokens；保留的 `raw_content` 为空且 `proposal=null`，不能声称留有可恢复的截断代码。后续 manifest/执行准入问题未被执行或核验。

11 次 HTTP 200：118,899 输入 + 36,836 输出 = 155,735 tokens，unknown 0；未缓存费率估算 USD 0.0798729，不是账单。请求/响应文件 hash、原 usage 和逐条成本均重验。驱动 outcome 报告无源码变化；06:47:39 UTC 独立核对 62 源/依赖文件及驱动仍匹配冻结。审计前后 78 个原 Run 文件 hash 不变，未补运行；本轮不计为实际代码错误修复证据。

## Live019：两次 ORCA 完成，输出选择与观察清单修正未通过

证据索引：[live019-output-selection-and-delivery-failure.json](validation/live019-output-selection-and-delivery-failure.json)。这是 agent-json-v48 / thinking_low / 64k 输入预算的最终候选原文重跑；与 live017 的 v47 历史首次成功分别保留。原文与方案及 live017 逐字相同，原文 SHA-256 记录在索引中；模型自行提出 Plan、三个 Python 文件和 ORCA 输入，执行的两份代码均与原始模型提案、writer Artifact 和密封脚本逐字节一致，没有人工 Plan 或任务代码注入。

Run 在 2026-10-19T07:07:05.707020Z 结束为 failed / partial。准备结构与频率目标满足，优化结构目标仍为 insufficient_evidence。21 次 HTTP 调用全部结算：273,034 输入 + 67,929 输出 = 340,963 tokens，未知用量 0；按冻结的未缓存峰值费率估算 USD 0.1634250，非账单。两次 Python Attempt 和两次内部 ORCA 均实际执行并完成；每次四进程、MaxCore 192 MB，同容器总内存限制 1024 MB，累计声明工作量 314/400、额外 Python 额度 1/1、总 ORCA 2/2、额外 ORCA 1/1。CPU 累计 338.517854 秒、墙钟累计 152.922 秒，resource_usage_complete=false，保留采样下界限制。

第一次脚本以 next(...endswith(".xyz")) 选中 SDK 文件映射中的初始 ethanol.xyz，把它作为优化结构观察来源；真正的 job.xyz 也已保存但未被选中。程序保留未验证观察，并阻止优化目标满足。模型收到目标缺口后又写了两份脚本，执行第三份；它仍用首个 XYZ 规则，这次选中初始 geom.xyz。第二 Step 仅声明 optimized_geometry，脚本清单却同时发出 vibrational_frequencies，实际诊断为 observation_manifest_rejected / observation name is duplicate or outside the frozen Step，全部观察被拒绝。第二次操作完成不能视为修正成功。

两次 Attempt 属于同一 Run，但 logical ID 不同；原 Request 版本、初始 Artifact 与 SHA、方法/基组/电子态/电荷/多重度/环境保持不变，输入目标文件名与声明观察集合改变。新增 logical ID 没有重置额度：第二次启动前，Run-wide 额外代码计数及 SDK 总启动、额外科学许可和内部工作量门禁继续生效。这里没有用真实第三次启动验证拒绝门禁，也不声称完成了同 logical ID 的失败重试。

最后三次模型回复均 finish_reason=stop，原文分别为 1008、1565、629 字符，均缺外层闭合括号，JSON 解析在 EOF 失败；不是长度截断。第三份回复仅在审计内存补一个括号后可解析，但 reason 仍在 parameters 内；该探针从未提交给原 Run。三个实际上下文均显示真实顶层 RESPONSE_ENVELOPE.reason，后两次反馈仅为 invalid_proposal_json。有限修正次数耗尽，未形成模型 terminal delivery；原始 outcome 和程序报告均正常保存。

原始 LF 输入与 SDK/实际 ORCA 的 CRLF 副本单列哈希：第一次 243→250 B，第二次 240→247 B，每次仅七处换行差异，文本规范化后相同，不宣称原始字节相同。07:08:24.692852Z 的结束复核确认 62 个冻结源码文件和 driver 与运行前一致；309 个原 Run 文件在独立审计前后不变，70 个归档文件索引核对通过。后续候选修复不回填此 Run。独立科学 oracle 的方法与收敛核验另列；本次通用输出没有获得生产科学资格，也没有建立成功修复闭环。

## Live021：受控故障被开发门禁误挡，未执行 Python

证据索引：[live021-seeded-observer-assumption.json](validation/live021-seeded-observer-assumption.json)。本次为明确加入 undefined-name 标记的开发故障场景，不是自然错误，也不是原文成功场景。沿用 v49/64k 冻结候选，Run 最终 failed / partial；准备结构已满足，优化与频率证据不足。

模型真实写出了首行加载 CODE_TOOLS_DEVELOPMENT_FAULT_V1 的 Python Artifact；独立 AST 检查未发现该名称赋值，模型提案、writer 内容与 Artifact 字节一致。但开发 SeededCodeErrorGate 在 before_science_reservation 要求最后一条决策为显式 call_tool(step_id)。真实模型通过 revise_plan 形成 Plan v2，主循环直接执行 ready Python Step，最后的持久化决策是 Plan 接纳记录而无 action 字段。门禁因此在检查代码之前报 seeded gate requires the current model-selected execution Step；这是开发观察器的假设错误，不是实际 Python 故障。

未建立种子门禁，0 Attempt、0 ORCA、0 内部工作量、0 代码重试；没有真实 NameError 或修正成功。候选 Python manifest.entries 为空也是原始事实，但后续执行门禁未到达，不能将潜在输入或脚本问题记作已经发生。11 次 HTTP 使用 117744 输入 + 19586 输出 = 137330 tokens，未知 0，冻结费率估算 USD 0.0588264，非账单。预约仍不可回收，后续修正开发门禁须另启 Run，不能恢复或回填本次。

07:22:18.723847Z 结束独立检查确认 62 个源码文件与原 driver 均与冻结记录一致；81 个原 Run 文件审计前后不变。原始 outcome、报告与 seeded-code-error-prelaunch.json 保留，开发门禁修正不会改变本次 not_established 事实。

## Live022：实际 NameError，首版占位程序与历史失败上下文缺失

证据索引：[live022-seeded-stub-and-unchanged-input.json](validation/live022-seeded-stub-and-unchanged-input.json)。Run `run_e3fcf3e308a9418ebf21a748f065a9a3` 在 v49 / v4 配置下结束 failed/partial，准备结构目标满足，优化与频率不足。开发观察器已识别模型激活 Plan 的首个 ready Step；实际执行 359 字节的模型 Python，在第 3 行产生指定未定义名称的 NameError，退出码 1。Attempt.started=true、execution.not_started=false；零 ORCA 不表示 Python 未启动。

首版程序只有故障占位代码，其 Step 的 manifest、conditions 和 observation_names 均为空，未包含完整科学任务。因此真实 NameError 本身不构成完整任务代码修复证据。第 15 次模型回复正确识别失败，并写出第二版程序，但修订 Plan 时移除了原失败 Step。第 16 次上下文只保留其 Result ID，未保留失败 Result 内容或原冻结执行合同；模型误称第一版尚未执行，又以新 Step/new logical ID 引用原失败代码。Store 的原重复输入门禁在预约前正确拒绝，诊断为 unchanged failed input cannot be retried automatically；没有第二次 Python Attempt，代码重试额度实际未消耗。

共 16 次真实 HTTP，193448 输入 + 31827 输出 = 225275 tokens，未知 0；按冻结未缓存费率估算 USD 0.0962268，非账单。实际 1 次失败 Python，0 内部调用、0 ORCA、0 工作量、0 code retry；CPU 0.720041 秒、wall 1.781 秒，resource_usage_complete=false。源码与驱动结束冻结检查通过，原 Run 文件在只读审计前后不变。后续修复针对上下文保留，不改变 ready 判定、执行状态或重复输入门禁；新测试协议明确首版必须为完整任务，旧失败不回填。

截至本次的[独立累计审计](validation/cumulative-budget-live001-022.json)逐条复算 236 条 HTTP 回执：累计 2753166 tokens、模型费用估算 USD 1.1907360、6 次 Python（4 completed、2 failed）、4 次 ORCA、564 工作量、1 次 code retry，未知用量 0。已使用同批 22/50 Run 预约，余 28；1873 个原 Run 文件和 1019 处来源引用 hash 核对通过。上述统计包含全部历史失败，不是成功率或正式重复验收。

## Live024：任务代码已写入，修订计划在执行前被拒绝

证据索引：[live024-seeded-retained-plan-failure.json](validation/live024-seeded-retained-plan-failure.json)。本次使用 v50、同一 v4 配置与新的 v2 受控故障请求，最后 failed/partial，准备结构满足，优化与频率不足。没有 Python Attempt、ORCA、内部工作量或 code retry；seed gate 尚未到达执行前核验点，不能记为实际 NameError 或修复失败重跑。

第 13 次 revise_plan 只列出新的 Python Step，却在 depends_on 引用三个未包含的旧 Step。materialize_plan 在依赖 ID 映射处先抛 KeyError；此时尚未检查 goal_map。候选也遗漏了 goal_map 引用的 prepare Step，但该分支已有 unknown_goal_step 结构化诊断，不能将其描述为此处裸 KeyError 的来源。第 14、15 次都在 index 1 的 prepare Step 上携带 {key, depends_on}，先触发仅允许 {key} 的保留规则。第 15 次模型误以为只需删掉 writer 的 tool；它完整看到了上一被拒提案，但反馈没有准确指出 Step、字段和合法形状。

index 0 的 resolve 在两份提案中均以 tool/parameters 显式重建，不是本轮 retained 分支首先报错的位置；离线纯内存探针显示它因遗漏 system_id 还会在后续不可变校验中被拒绝。将三个已有 Step 都在内存中改为仅 {key} 后，既有图、预算与不可变校验通过；该定位探针没有激活 Plan、写回 Run 或执行任务，绝不是模型修复证据。三个真实回复均 finish_reason=stop 且 JSON 完整，未发生代码截断。

15 次 HTTP 使用 194050 输入 + 33116 输出 = 227166 tokens，未知为零；冻结未缓存费率估算 USD 0.0979542，非账单。独立结束核对确认 62 项源码及驱动未变，93 个原 Run 文件和 79 处引用审计前后 hash 一致。后续修复限于提供准确的提案诊断，不自动补入旧 Step、删除字段或放宽既有计划和执行校验；后续候选须另启 Run。

## Live025：原文复验在选型引文与 JSON 纠正阶段失败

v51 与 v4 配置的原文复验 `run_c4b936f81c2f4447a1d28b1918a145a0` 为 failed/partial。第 5 次提案将基组原文中的数学标记改为 ζ，第 7 次又删除数学定界符，两次均不满足连续原文引文要求；中间第 6 次回复缺少外层 JSON 闭括号且 reason 嵌套在 parameters，finish_reason=stop，不是截断。已有准确字段反馈仍未使模型完成合法纠正。没有 Plan、结构准备、Python Attempt 或 ORCA；这不构成代码执行或修复证据。

7 HTTP，65528 输入 + 6846 输出 = 72374 tokens，未知 0，冻结未缓存费率估算 USD 0.0278736，非账单。62 项源码与驱动冻结检查通过，34 个原 Run 文件和 30 处来源引用未变。完整差异、实际拒绝和来源见 [live025 审计](validation/live025-condition-source-quote-failure.json)。失败与预约保留，下一开发运行继续使用同一候选与累计预算，不修改引文门槛或旧 Run。

## Live026：中文名称查询 404，同目的换 ID 重复被拒绝

同一 v51/v4 原文复验 `run_2423e174b54644cb99e69e10ab760a68` 为 failed/partial。模型将 query_name 保留为中文“乙醇”，PubChem 返回 HTTP 404、PUGREST.NotFound / No CID found，完整响应已保留。模型随后把失败称为 transient 并用新 Step key 重建相同目的的身份查询；该推断不是已验证事实，持久 Call 门禁拒绝重复查询，未发生第二次外部请求。没有结构准备、Python Attempt 或 ORCA。该次不证明所有中文名称都能解析，也不是实际代码修复证据。

7 HTTP，64597 输入 + 10558 输出 = 75155 tokens，未知 0；估算 USD 0.0320487，非账单。驱动记录运行前后源码不变；独立首次核验发生在结束后，不声称独立抓到了运行中开始快照。独立结束对照的 62 项源码与驱动一致，42 个原 Run 文件和 34 处引用未变。详见 [live026 审计](validation/live026-identity-404-duplicate-refused.json)。后续保持候选与门禁不变，先执行受控完整脚本故障对照，再复验原文任务；全部预约与失败继续累计。

## Live027：完整脚本的故障行已真实修复，原生输入仍被拒绝

同一 v51/v4 的受控 v2 对照 `run_0e30607d1dc4494f8db5fce5d2b02cbe` 最终 failed/partial，只有初始结构 Goal 满足。首版 Python 3987 字节，包含 SDK 调用、最终结构与频率读取、两项观察和来源交付；第 2 行独立 marker 引发真实 NameError，发生时尚无 SDK/ORCA。模型通过 workspace.write 生成 3955 字节修订版，逐字节恰好仅删除 32 字节 marker 行。新的执行 Step 与失败 Step 的 logical_id 相同，除新 id 与 CodeRef 外全部冻结字段相等；两次 system_id 均为 null，单乙醇 Request 和 manifest 的事实保留，没有人工补入体系字段。

模型在修订 writer 时夹带 retained depends_on，被 v51 精确指出后自行改为 bare key；后续补齐漏列依赖，经既有校验后执行第二个 Attempt。第二次真实 Python 已越过故障行并调用 SDK，但原模型 ORCA 输入含 `%output\n  jsonpropfile\nend`，字段缺标量值，被原生准入拒绝。错误信息虽包含“no nested blocks”，真实原因不是出现了嵌套块。原输入没有改写，ORCA 没有启动，不能将这一代码修订和重跑称为成功科学闭环。

18 HTTP，264689 输入 + 53121 输出 = 317810 tokens，未知 0；估算 USD 0.1431519，非账单。2 次真实 Python 均 failed，1 次代码重试，0 ORCA、0 内部工作量；CPU 采样下界 1.626623 秒、wall 6.016 秒，resource_usage_complete=false。终止解释合同 passed/rendered，但终止状态明确 failed、两科学目标不足。62 项源码与驱动结束冻结核验通过；172 个原 Run 文件和 159 处引用未变。字节、冻结合同、实际错误及来源见 [live027 审计](validation/live027-seeded-repair-native-input-refused.json)。

后续 v52 仅补充原生输入合同中的通用块标量语法说明，不改变准入、修复预算或实际任务文件。任何内存中补值的开发探针只验证准入规则，不算该 Run 恢复，更不算真实 ORCA 成功。

## Live028：真实修复执行完成，独立交付完整性仍未通过

v52/v4 受控 v2 对照 `run_e545504b9174421e8e0156a4c6568fc3` 在生产记录中 completed/complete、三个 Goal 满足。完整首版 3375 字节的第 1 行 marker 触发真实 NameError，尚无 ORCA；模型自行写入 3343 字节修订版，恰好只删 32 字节 marker 行。同一 logical 的第二 Attempt started/completed，除 id/CodeRef 外全部冻结 Step 字段不变，实际完成一次四进程 B3LYP/def2-SVP Opt+Freq。Step 的 conditions={} 继承固定 Request，体系、输入 manifest、依赖与观察名没有在修复时变更。详见[执行与成本审计](validation/live028-seeded-repair-execution.json)。

[独立科学审计](validation/live028-independent-scientific-audit.json)通过原始计算的实际方法/基组、电子态、电荷/多重度、26 电子、SCF/响应收敛、五项优化判据及完整 27 模式与 Hessian 重算；重算最大差约 0.00021624 cm^-1。原始谱确为 6 个零模式和 21 个正模式，原始最终 XYZ 正确。生产三项观察仍为 unverified、qualified_outputs={}。

独立完整交付验收为 **false**：生成函数按 value>1e-6 筛选，只交付 21 个正值，未说明省略的 6 个零模式；优化 XYZ 虽与原始最终文件逐字相等，unit=null，报告也显示 unknown。21 个正值本身及其 cm^-1 单位、来源和终止快照均正确；没有发现明确虚假的“无虚频”终止结论，但筛选说明和坐标单位缺项仍不能略过。不能以生产 completed/complete 或原始科学通过替代该交付要求。

14 HTTP，165789 输入 + 40840 输出 = 206629 tokens，未知 0；估算 USD 0.0987447，非账单。2 次 Python（一 failed、一 completed）、1 次 ORCA、1 次代码重试、157 声明工作量；CPU 采样下界 156.355410 秒、wall 89.062 秒，资源采样不完整。运行前后 62 项源码和驱动一致，221 个原 Run 文件及最终索引的 197 处引用未变。后续 v53 只增加通用的完整序列、显式筛选限制和有来源单位指导；null 仍是 unknown，不新增科学检查器或改写历史结果。

## Live029：故障标记与 future 导入次序错误，执行前拒绝

v53/v4 的 v2 受控对照 `run_dc6938abfe474b3880bc0cb3f6d5f98f` 最终 failed/partial。首版 4254 字节 Python 把 marker 放在第 1 行、`from __future__ import annotations` 放在第 3 行，开发门禁的真实 compile 检查因此产生 SyntaxError。它尚未预约或启动 Python，不能计为实际 NameError、代码重跑或科学计算；0 Attempt、0 ORCA、0 工作量。模型代码和原始请求均未修改。

12 HTTP，151429 输入 + 24032 输出 = 175461 tokens，未知 0；冻结费率估算 USD 0.0742671，非账单。62 项源码及驱动的独立起止冻结核对通过，87 个原 Run 文件和 72 处来源引用不变。详见 [live029 审计](validation/live029-seeded-prelaunch-syntax-failure.json)。后续 v3 测试请求仅明确 Python 的 future 导入必须在 marker 前，已在独立核验计划中前瞻记录；原 v2 字节、门禁和通过标准保留。

## Live030：中文身份查询再次 404，受控代码未执行

同一 v53/v4 的 v3 受控对照 `run_90596622c6684a6b801bb523d0399328` 最终 failed/partial。模型以中文 query_name“乙醇”查询 PubChem，实际收到 HTTP 404 / PUGREST.NotFound；随后用新 Step ID 重试同一 Request、体系与查询目的，持久 Call 门禁在第二次外部请求前拒绝。另一次文档枚举提案错误已由后续回复纠正，不是终局原因。

实际完成一次 workspace.write，首版代码 3015 字节，其 marker 位置与编译合法；没有 structure.prepare、Python Attempt 或 ORCA，受控故障 not_established。代码静态存在使用 `job.out` 的风险，但没有执行到该处，不能把它写成本次实际错误。8 HTTP，87195 输入 + 22793 输出 = 109988 tokens，未知 0；估算 USD 0.0535101，非账单。62 项源码及驱动独立起止冻结一致，47 个原 Run 文件和 39 处来源引用未变。详见 [live030 审计](validation/live030-seeded-identity-404-duplicate-refused.json)。累计预约已使用 30/50，失败保留；没有通过换 Run 回收任何消耗。

## Live031：真实故障反馈后的模型回复被截断

v54/v4 的 v3 受控对照 `run_1472758d09264a5ca56439c2eb53013a` 已用模型提出的英文 query_name 取得独立身份来源并完成初始结构；中文 requested_names 仍保留，别名仍标为 inferred_translation_or_alias。首版 4954 字节完整程序在第 2 行 marker 产生真实 NameError，实际 Python started/exit 1，发生时 0 SDK/ORCA。程序包含完整计算、输出读取和观察交付，冻结 manifest、条件、目标、依赖齐全；不再是占位故障程序。

随后第 13 次真实模型响应 HTTP 200、finish_reason=length，completion_tokens 恰为 16384，raw_content 为空、proposal=null。既有协议将截断判为 fatal，无法构造可执行修订；没有第二 writer、修订 Artifact 或第二 Attempt。最终“bounded proposal correction or transport retry exhausted”是通用停止文本，不能据此推导这一修复轮用尽普通纠正次数或科学额度。此前一次缺依赖提案已被纠正，与终局截断分开记录。

最终 failed/partial，仅准备结构目标满足；13 HTTP，173121 输入 + 45239 输出 = 218360 tokens，未知 0，冻结费率估算 USD 0.1062231，非账单。1 次失败 Python，0 ORCA、0 code retry、0 工作量；CPU 采样下界 0.704390 秒、wall 1.531 秒，资源采样不完整。62 项源码与驱动独立起止冻结一致，113 个原 Run 文件和 101 处引用未变。详见 [live031 审计](validation/live031-seeded-repair-response-truncated.json)。下一运行仍用相同 v54/v4/v3，不修改输出限额或既定验收标准。

## Live032：执行计划的连续结构纠正未成功

相同 v54/v4/v3 受控对照 `run_52d53e5e8c554be6b77071ee249fab7d` 最终 failed/partial。身份和初始结构已完成，模型已写入完整 Python 与原生输入，但执行计划最后三份提案依次混用 Goal 的 gap 与 Step 绑定、遗漏保留 prepare 所依赖的 resolve、给仅允许 key 的保留 Step 添加 depends_on。反馈分别指出真实形状和位置；同一决策的初始提案及两次纠正仍未通过。三份均 finish_reason=stop、JSON 完整，不是截断。

0 Attempt、0 ORCA、0 code retry、0 工作量，尚未到达受控 marker 执行前门禁；不能称为实际 NameError 或代码修复失败。16 HTTP，221463 输入 + 34813 输出 = 256276 tokens，未知 0，估算 USD 0.1082145，非账单。62 项源码及驱动独立起止核对通过，100 个原 Run 文件和 86 处引用未变。详见 [live032 审计](validation/live032-seeded-plan-shape-failure.json)。现有合同与候选保持不变，下一预约先检查相同候选的原文自主流程；本次失败仍计入累计额度。

## Live033：原始计算与完整观察通过，最终目标绑定失败

v54/v4 原文复验 `run_3dc3f7fe404c40038c8c6a95f0ce124c` 实际完成一次模型生成 Python 和一次四进程 ORCA B3LYP/def2-SVP Opt+Freq。最终结构观察明确 angstrom，频率观察明确 cm^-1，包含完整 27 模式（6 零、21 正、0 负）。[独立科学与观察审计](validation/live033-independent-scientific-audit.json)通过实际条件、SCF/CP-SCF、五项优化判据、Hessian 重算、最终结构及完整数值/单位/来源；stdout 频率逐值相等，与 Hessian 表的最大舍入差约 0.00485845 cm^-1，在预定 0.006 容差内。生产观察仍为 unverified，qualified_outputs 为空。

实际 Plan 仍将两个科学 Goal 指向 gap。模型随后提出 evidence.producer_step_id 路由但未声明 rule_version，被最低规则校验拒绝；第二次补入版本却遗漏外层 port，被明确形状诊断拒绝。最后模型误认为缺少注册性质检查器意味着不能交付，主动 stop。终止解释合同通过，但状态如实为 failed/partial，两个科学 Goal 的 answer=null、证据不足，不能称为原文自主交付成功。

只读内存对照将首份交付候选改为已有完成 Step 的 step_key+port，原 materialize_plan 和 validate_revision 均通过。该对照未激活 Plan、写回 Run 或启动计算，不计作真实模型修复。后续 v55 仅完善“最低规则”错误的字段与通用观察契约说明，不降低科学检查要求。

17 HTTP，215126 输入 + 42762 输出 = 257888 tokens，未知 0；估算 USD 0.1158522，非账单。1 次 Python completed、1 次 ORCA、157 声明工作量、0 代码重试；CPU 采样下界 160.286721 秒、wall 92.172 秒，资源采样不完整。62 项源码及驱动起止冻结一致，201 个原 Run 文件和 183 处引用未变，见[执行审计](validation/live033-autonomous-execution-partial-delivery.json)。科学审计包装脚本第一次未处理失败 Goal 的 answer=null 而报 TypeError；增加只读 null guard 后完成，原科学 oracle、阈值和 Run 均未改，调用历史已记录。

## Live034：自然代码错误已发生，修复代码草案未进入执行

v55/v4 原文 Run `run_baee349801f04fa69549710619fcbcf7` 的首个实际 ORCA 正常结束，随后 2281 字节模型 Python 在第 13 行查找 `job.out`，由 pick 函数抛 RuntimeError: missing expected output file。[独立 raw 科学核验](validation/live034-independent-scientific-audit.json)通过实际 B3LYP/def2-SVP、RKS、气相 0/1、SCF/响应、五项优化判据、完整 27 模式及 Hessian/最终 XYZ。原谱有 6 零、21 正、0 负，仅支持该局部结构的谐振结果；这不弥补 Python 解析失败或缺少最终观察。

模型确已识别文件名错误，三个未接受的修复 writer Plan 中分别有 2940、2665、3339 字节修订代码文本。它们依次因科学 Goal 混用 gap+step_key、prepared Goal 引用未列入 Plan 的旧 Step、prepared Goal 仅剩 port 而无绑定被拒；没有新代码 Artifact、第二 Attempt 或重算。三个文本草案不计作实际执行修复，也没有触发 v55 最低规则诊断。最终 failed/partial，observation_records 和 qualified_outputs 为空，terminal_deliveries 为空、模型解释合同 unavailable；原文自然修复路径仍未通过。

19 HTTP，259912 输入 + 51131 输出 = 311043 tokens，未知 0，估算 USD 0.1393308，非账单。1 次 Python failed、1 次 ORCA completed、157 工作量、0 code retry；CPU 采样下界 161.186339 秒、wall 95.672 秒，资源采样不完整。62 项源码和驱动独立起止核对一致，207 个原 Run 文件和最终 189 处引用不变。[执行及修复草案审计](validation/live034-natural-output-discovery-repair-staging-failure.json)保留了三份完整差分，未替模型写入或激活修订。后续新模型配置独立冻结，历史失败不回收。

## Live036：执行前模型接口支付类拒绝，实际消耗仍有未知项

v55 / Pro v5 / 受控请求 v3 的 `run_738360e148fc46e6aec6735a84be7d09` 已完成身份查询、结构准备及模型代码写入，最后第 15 个模型请求却收到 HTTP 402，`retryable=false`、`usage=null`、无响应正文。既有协议按 fatal 有界停止，最终 failed/partial；通用的“bounded proposal correction or transport retry exhausted”不能据此解释为已经用完普通纠正次数。官方[错误说明](https://api-docs.deepseek.com/zh-cn/quick_start/error_codes/)将 402 定义为余额不足，账户充值属于外部待处理条件，不通过换 Run 盲目重试。

14 条已知响应合计 204099 tokens；最后调用保留 56747 输入预约 + 16384 输出预约 = 73131 未知 token 额度，不能记为零或当作实际消耗。Pro 产品费用仍未知；独立价格估算只能覆盖已知 usage，不能称为完整费用或账单。0 Python Attempt、0 ORCA、0 code retry、0 工作量；受控 marker 门禁尚未到达，没有真实 NameError 或实际代码修复。

首版静态存在频率数值与独立单位 token 的解析风险，但该程序未执行，不将其写成本次实际错误。62 项源码和驱动独立起止核对一致；起始独立观察在 Run 启动后约四分钟、最终 outcome 之前进行，不冒充启动瞬间的观察。累计预约 36/50，剩余 14；全部失败、已知消耗及未知预约保留。live035 的同候选原文交付已经通过，步骤 3 仍缺成功的实际错误修复证据。


## Live037：真实首个 NameError 后修复合同重抄失败

v56 / Pro-v5 / 受控 v4 请求的 `run_2a94bfb23c984971b6a94860f738815d` 在 Linux 中执行模型生成的 2592 字节完整首程序，在第 2 行触发预声明的 NameError；该时点没有 SDK 调用或 ORCA。随后模型通过 workspace.write 写出 3275 字节新代码，除删除 marker 外还调整路径和解析逻辑；完整差分及模型/Artifact 来源见[代码审计](validation/live037-code-repair-audit.json)。新代码确已写入，但没有第二次 Python Attempt；未执行代码的潜在格式或单位问题只作静态风险，不冒充实际运行故障。

修复 Plan 最后三次提案依次因 retained Step 多余 depends_on、Goal 混用 gap 和 step_key、输入 manifest 的第一项 Artifact/hash/target_path 改动被拒。最后一项违反既有仅改 CodeRef 的冻结合同。有限纠正耗尽后终局 failed/partial，两个科学 Goal 均证据不足；0 次代码重试、0 次真实 ORCA、0 声明工作量，不构成修复成功。gate 只证明实际首错误位置，不证明后续计算。

17 HTTP、275841 已知 tokens、未知 0；本次已知 usage 的独立估算 USD 0.50578308，Pro 产品费用仍未知。62 项源码和驱动起止冻结一致，131 个原 Run 文件未变，[执行摘要](validation/live037-pro-seeded-v4-execution.json)保留实际反馈和拒绝。[001—037 累计审计](validation/cumulative-budget-live001-037.json)：37/50 次预约、447 HTTP、5896487 已知 tokens，另保留 live036 的 73131 未知预约；16 次 Python（8 completed、8 failed）、9 次 ORCA、3 次代码重试、1349 工作量。已知部分独立费用估算 USD 3.57733902，不能称为完整账单。

随后先记录 [0035](../../decisions/0035-declarative-code-repair-reference.md)，再实施显式引用冻结失败 Step 与新 CodeRef 的小范围接口修复。它减少模型重抄不变字段，但不替模型修脚本、补依赖或改 Goal；新候选的验证另记，旧失败不重判。

## Live038：显式修复实际执行，第二次在原生输入准入处失败

v57 / Pro-v5 / 受控 v5 的 `run_49eef7d8c16b4c02a8e84228ea830c18` 于 2026-10-19 13:06:22 UTC 结束为 failed/partial。首程序真实执行，在第 2 行触发预声明 NameError，早于任何 SDK 调用。模型自行写出修订版，删除 marker、改正输入文件名并调整来源路径；随后明确采用三字段 repair_of 提案。程序从真实失败 Attempt 复制冻结合同，新 Step 与旧 Step 的 logical_id 及所有非 CodeRef 任务字段逐项相同，第二次 Python 确实启动。完整前后代码、差分、模型回复与执行来源见[代码审计](validation/live038-code-repair-audit.json)。这证明新接口已在真实模型链中获准执行，但不等于原目标完成。

第二次代码运行至 SDK 时，其 ORCA 输入遗漏 `%method Z_MaxIter`，现有冻结上限校验拒绝：`managed_orca_request_rejected: explicit %method Z_MaxIter exceeds or omits the frozen bound`。两次 Python 都以 exit 1 失败，累计 1 次代码重试，0 ORCA、0 内部计算预约、0 工作量，没有科学观察或合格输出。因此不创建空科学审计，也不把尚未执行的频率解析静态风险记为实际故障。运行没有人工改稿、补输入或放宽校验。

20 HTTP、335236 已知 tokens、未知 0，源码 62 项与驱动的起止冻结一致；173 个原 Run 文件未变。[001—038 累计审计](validation/cumulative-budget-live001-038.json)记录 38/50 次预约、剩余 12，467 HTTP、6231723 已知 tokens，另有 live036 的 73131 未知预约；18 次 Python（8 completed、10 failed）、9 次 ORCA、4 次重试、1349 工作量。已知部分独立费用估算 USD 4.18836702，完整账单与 Pro 产品费用仍未知。原文成功 live035 保持 v55 身份，不能与本次拼接为同候选全部通过。

## Live039：方法选型纠正耗尽，未进入代码路径

v58 / 原 Pro-v5 配置 / 原受控 v5 请求的 `run_7c664f9ceab74c8c984d1653a866817e` 于 2026-10-19 13:24:29 UTC 结束为 failed/partial。第 7 次模型回复为 def2-SVP 引用的官方片段仅含 def2-XVP；第 8 次将 knowledge.search 误作顶层 action；第 9 次仍引用没有准确基组名称的通用 `Basis <BasisName>` 片段。三次提案均未通过已有选择或 action 校验，有限纠正耗尽。实际 0 writer、0 Python、0 SDK、0 ORCA、0 重试、0 工作量，没有首程序、代码差分或科学结果可核验。不能据此判断 v58 代码路径成功或失败，更不能把它计作修复成功。

[执行记录](validation/live039-pro-seeded-v5-execution.json)与[代码侧零执行审计](validation/live039-code-repair-audit.json)保留全部实际拒绝，41 个原 Run 文件不变。单次 9 HTTP、138339 已知 tokens、未知 0；已知部分独立估算 USD 0.29642316。[001—039 累计](validation/cumulative-budget-live001-039.json)为 39/50、剩余 11，476 HTTP、6370062 已知 tokens、73131 未知预约；累计 Python/ORCA/重试/工作量仍为 18/9/4/1349，已知部分独立费用估算 USD 4.48479018，完整账单未知。

随后在剩余已授权开发预算内启动 live040，沿用同一 v58 源、配置、请求、预算和验收标准，不针对这次模型选型失败增加生产改动。039 保留为该候选的失败样本，不通过挑选后续成功将其删除或改判。

## Live040 与本轮收尾：计算完成，归档超时，用户停止专项试跑

相同 v58 / Pro-v5 / 受控 v5 的 `run_cfc6d8457b524df8a2b696109b312495` 首次 Python 触发预声明 NameError；模型自行生成新程序并以明确 `repair_of` 引用启动第二次 Attempt，同逻辑身份和所有非 CodeRef 任务合同保持一致。第二次 Python 经 SDK 启动真实 ORCA，计算正常结束、exit 0；随后本机归档超出 30 秒期限，SDK 抛出 `ORCA evidence collection incomplete: task artifact collection deadline exhausted`，观察解析没有执行。不能把修复了首个错误或 ORCA 正常结束表述为完整修复交付通过。详见[代码审计](validation/live040-code-repair-audit.json)和[执行审计](validation/live040-pro-seeded-v5-execution.json)。

[独立科学审计](validation/live040-independent-scientific-audit.json)仅检查现存原始文件：B3LYP/def2-SVP、SCF、五项优化条件、CP-SCF、完整 27 模式（6 零、21 正）、Hessian 独立频谱及最终 XYZ 通过原判据；没有观察或终端交付，因此完整交付仍 false。只读核验没有补归档、改写历史或授予生产科学资格。

归档失败发生于 13:38:47 UTC，早于用户暂停信号 13:39:03 UTC。最终 Run 为 paused/partial，不能归因为暂停导致计算失败。最后模型记录创建时间 13:36:25 UTC；共 15 条已知回执、231809 tokens，无暂停后的新模型记录。两次 Python 均失败、1 次真实 ORCA、1 次代码重试、93 声明工作量。独立起止核对 62 项源码与驱动一致，192 个原 Run 文件未变。

只读根因检查发现：每次 `Store.import_artifact` 都扫描整个 Store 的 Artifact 元数据；777 份元数据的一次扫描实测约 2.376 秒，连续归档间隔约 2.76–3.24 秒。16 个原始文件已经导出至宿主，但第 11 个归档完成后在下一项前耗尽期限。这是本机归档瓶颈，不是 ORCA 科学失败或 Docker 导出失败。按用户最新“就先这样完成步骤三收尾”的指令，保留为**已定位、未修复**限制；没有提高期限、预算或新增优化实现。只读诊断文件与 hash 已收入代码审计。

用户停止 NameError 专项，不再要求以其完整通过作为本轮收尾条件；最终明确不进入步骤四。正常任务的 live035 完整交付成功保持原 v55 / Pro 身份，不能改称本次 v58 或 Flash 复验。当前及示例配置按用户要求切换 Flash，见[离线配置验证](validation/model-flash-v6-configuration.json)；没有新的付费 API 或 ORCA 调用。

[001—040 最终累计](validation/cumulative-budget-live001-040.json)为 40/50 次预约、491 HTTP、6601871 已知 tokens，另有 live036 的 73131 未知预约；20 次 Python（8 completed、12 failed）、10 次 ORCA、5 次代码重试、1442 声明工作量。已知部分独立未缓存峰值费用估算 USD 4.91126502，完整费用未知，不是账单。剩余 10 个预约不再自动使用；没有 live041。本轮交付等待用户验收，步骤 4—7 未实施。
