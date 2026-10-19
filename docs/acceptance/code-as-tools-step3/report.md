# 前置问题修复与步骤 3 开发记录

实施基线：`7860db02b7fad0be4b491d0a83fab9e9eb885f78`。范围仅为进入步骤 3 前的阻断问题及专项方案步骤 3；不实施步骤 4—7，不记为用户验收通过。

**本轮步骤 3 收尾待验收，不进入步骤 4。** 用户于 2026-10-19 最新明确“就先这样完成步骤三收尾”，停止 NameError 专项。正常路径已有 v55 / Pro-v5 的 live035 completed/complete 及[独立科学与完整交付](validation/live035-independent-scientific-audit.json)：1 次 Python、1 次四进程 ORCA、9 原子 Angstrom 坐标和完整 27 项 cm^-1 频率；首次执行前改稿不算失败修复，也不重标为当前候选重复成功。受控修复后的完整交付仍未通过，但按用户决定不再阻塞本轮交付，不改判失败或宣称用户已验收通过。

live036 的 HTTP 402 与未知用量、live037 提案失败、live038 SDK 准入拒绝、live039 执行前失败均保留。live040 已实际运行 ORCA，但主机归档超过 30 秒收集期限，第二 Python 失败、无观察；用户随后要求暂停，最终 paused/partial。现存原始文件[独立科学通过](validation/live040-independent-scientific-audit.json)不等于完整交付，收集超时是已知未修复限制，不能归因于用户暂停。[首轮全量](validation/continuation-regression-001.json)4231P/23F/179skip 保留，后续全量已按用户要求取消，不称全量 clean；不再追加付费试跑，步骤 4—7 未实施。

2026-10-19 用户将同一开发批次累计预约提高到 50 个 Run，旧预约、失败和消耗保留。[001—040 累计审计](validation/cumulative-budget-live001-040.json)记录 40/50 个预约，余下 10 不再自行消费；live040 为 paused/partial。491 次模型 HTTP 中 490 条有已知用量，输入 5509028、输出 1092843，共 6601871 tokens。live036 另有 73131 未知用量预约，保守占用 6675002；预约不是实际 token，不按零计算。20 次 Python（8 completed、12 failed）、10 次 ORCA、5 次代码重试、1442 工作单位，独立开发者探针另列。

Flash 的 398 次 HTTP 已知费用估算 USD 2.2778307；Pro 的 93 次 HTTP 中 92 条有用量，已知估算 USD 2.63343432。合计仅为已知费用估算小计 USD 4.91126502，完整费用 null/未知，不是实际账单；93 条 Pro 产品成本记录保持未知。累计复验 4220 个原 Run 文件和 3513 个来源引用未变。用户要求的[Flash 切换](validation/model-flash-v6-configuration.json)已在示例、本地和新 v6 开发配置落实，3 份配置禁网 prepare_request 通过，0 API/ORCA；未做付费复验，旧 Pro-v5 与历史 Run 不变。

## 当前验证进度

| 运行 | 真实执行和结果 | 证据范围 |
| --- | --- | --- |
| live001—live016 | 均未启动 ORCA；live016 有一次真实 Python 失败，修复提案未执行 | 历史失败全部保留，不能作为修复完成 |
| live017，v47，原始请求 | completed/complete；13 HTTP、159232 tokens、1 次 Python、1 次 ORCA | 首次自主真实交付；独立科学核验通过；code_retries=0 |
| live018，v47，受控故障对照 | failed/partial；11 HTTP、155735 tokens、0 次 Python/ORCA | Plan 遗漏冻结 Step 的依赖，纠正回复截断；故障种子未执行 |
| live019，v48，原始请求 | failed/partial；21 HTTP、340963 tokens、2 次 Python operation completed、2 次 ORCA | 优化 Goal 证据不足；第二份观察 manifest 整体拒绝；消耗 1 次代码重试，修复失败 |
| live020，v49，原始请求 | completed/complete；13 HTTP、174660 tokens、1 次 Python、1 次 ORCA | identity/prepared/optimized/freq 四个 Goal 满足；work=157、code_retries=0；[独立科学核验通过](validation/live020-independent-scientific-audit.json) |
| live021，v49，受控 NameError 对照 | failed/partial；11 HTTP、137330 tokens；0 Attempt/Python/ORCA | [测试观察器误要求最后决策为 call_tool](validation/live021-seeded-observer-assumption.json)，在科学预约前拒绝；不是模型 Python 执行失败，未发生真实 NameError |
| live022，v49，受控 NameError 对照 | failed/partial；16 HTTP、225275 tokens；1 次 failed Python、0 ORCA、0 代码重试 | 首版为 359 字节故障占位代码，真实 NameError 后未执行有效修复；不含完整计算流程，不能算完整任务程序修复通过 |
| live023，v50，原始请求 | completed/complete；16 HTTP、210739 tokens；1 次 Python、1 次 ORCA | 3 个 Goal 满足；四进程 B3LYP/def2-SVP；[独立科学核验通过](validation/live023-independent-scientific-audit.json)；code_retries=0 |
| live024，v50，受控故障 v2 | failed/partial；15 HTTP、227166 tokens；0 Python/ORCA | [保留 Step 依赖和格式提案失败](validation/live024-seeded-retained-plan-failure.json)；种子未执行 |
| live025，v51，原始请求 | failed/partial；7 HTTP、72374 tokens；0 Python/ORCA | [选型引用非连续原文及 JSON 错误](validation/live025-condition-source-quote-failure.json)，没有 Plan 或结构准备 |
| live026，v51，原始请求 | failed/partial；7 HTTP、75155 tokens；0 Python/ORCA | [中文 PubChem 404 后换 ID 重复查询被拒绝](validation/live026-identity-404-duplicate-refused.json)；模型所称 transient 未获事实支持 |
| live027，v51，受控故障 v2 | failed/partial；18 HTTP、317810 tokens；2 次 Python failed、0 ORCA、1 次代码重试 | 完整程序真实 NameError 后同逻辑仅删标记并重跑；原生 `%output jsonpropfile end` 缺标量被 SDK 拒绝，没有修复后成功交付 |
| live028，v52，受控故障 v2 | completed/complete；14 HTTP、206629 tokens；2 Python（先失败后完成）、1 ORCA、1 重试 | [实际修复执行](validation/live028-seeded-repair-execution.json)成立；[原始科学通过但独立完整交付失败](validation/live028-independent-scientific-audit.json)：遗漏六零模式且未说明筛选，坐标无单位 |
| live029，v53，受控故障 v2 | failed/partial；12 HTTP、175461 tokens；0 Attempt/ORCA | [marker 与 future 导入顺序造成预启动 SyntaxError](validation/live029-seeded-prelaunch-syntax-failure.json)，无真实 NameError |
| live030，v53，受控故障 v3 | failed/partial；8 HTTP、109988 tokens；0 Attempt/ORCA | [中文 PubChem 404 后重复查询被拒绝](validation/live030-seeded-identity-404-duplicate-refused.json) |
| live031，v54，受控故障 v3 | failed/partial；13 HTTP、218360 tokens；1 failed Python、0 ORCA、0 重试 | [实际 NameError 后修复响应截断](validation/live031-seeded-repair-response-truncated.json) |
| live032，v54，受控故障 v3 | failed/partial；16 HTTP、256276 tokens；0 Attempt/ORCA | [目标形状、依赖与 retained 字段提案纠正失败](validation/live032-seeded-plan-shape-failure.json) |
| live033，v54，原文 | failed/partial；17 HTTP、257888 tokens；1 completed Python/ORCA | [原始科学和完整观察通过](validation/live033-independent-scientific-audit.json)，[最终 Goal 绑定与交付失败](validation/live033-autonomous-execution-partial-delivery.json) |
| live034，v55，原文 | failed/partial；19 HTTP、311043 tokens；1 failed Python、1 completed ORCA、0 重试 | [原始科学通过](validation/live034-independent-scientific-audit.json)，[输出文件名错误后的修复未进入执行](validation/live034-natural-output-discovery-repair-staging-failure.json)，无最终观察 |
| live035，v55 / Pro-v5，原文 | completed/complete；17 HTTP、224492 tokens；1 completed Python/ORCA、0 重试 | 两项科学 Goal 满足；[独立科学和完整交付通过](validation/live035-independent-scientific-audit.json)；模型首次执行前改写不计实际修复 |
| live036，v55 / Pro-v5，受控故障 v3 | failed/partial；15 HTTP，14 条已知用量共 204099 tokens；0 Attempt/ORCA、0 重试 | [HTTP 402 与未知用量审计](validation/live036-pro-seeded-http402-unknown-usage.json)；73131 为额外未知预约而非实际 token；当时余额不足，用户后告知充值完成，历史失败保留 |
| live037，v56 / Pro-v5，受控故障 v4 | failed/partial；17 HTTP、275841 tokens；1 failed Python、0 ORCA、0 重试 | [完整首程序实际 NameError 后修复提案未执行](validation/live037-pro-seeded-v4-execution.json)，没有科学结果或完整交付 |
| live038，v57 / Pro-v5，受控故障 v5 | failed/partial；20 HTTP、335236 tokens；2 failed Python、1 重试、0 ORCA | [三字段修复引用真实展开并重跑](validation/live038-code-repair-audit.json)，第二次因 `%method Z_MaxIter` 遗漏被 SDK 拒绝，无观察或完整交付 |
| live039，v58 / Pro-v5，受控故障 v5 | failed/partial；9 HTTP、138339 tokens；0 writer/Attempt/ORCA | [选型引文和动作提案失败](validation/live039-pro-seeded-v5-execution.json)，未到 v58 代码执行路径 |
| live040，v58 / Pro-v5，受控故障 v5 | paused/partial；15 HTTP、231809 tokens；2 failed Python、1 ORCA、1 重试、93 work | [真实代码修订与 ORCA 执行](validation/live040-code-repair-audit.json)后主机收集超时；[raw 科学通过](validation/live040-independent-scientific-audit.json)，无观察或完整交付，随后用户暂停 |


前 19 次累计 **196 次模型 HTTP、2215901 tokens、估算 USD 0.9600441、4 次真实 Python（3 次 operation completed、1 次 failed）、3 次真实 ORCA、1 次已消耗代码重试**。其中 operation completed 只表示进程操作完成，不等于科学交付或目标完成。费用按冻结未缓存费率估算，不是供应商账单；失败预约不回收。独立开发者 HF 探针的 ORCA 启动及结构准备进程成本另列，不能并入模型自主成功或忽略。

## 实施与证据口径

前置缺陷的修复、原始失败对应关系和 455 项定向门禁见 [前置记录](prerequisites.md)。这些是进入本步前的证据，不能替代最终候选回归。原始失败文件继续保留，未将测试记录数量当作独立缺陷数量。

本次新增通用名称/目标表达、有来源的条件选择、不可变文件写入、代码 Artifact 引用、受管单次 SDK 优化/解析频率、带来源但无科学资格的观察及确定性交付。模型与传输限制、原生输入准入策略进入 Run 配置。文件写入、实际执行、科学检查和目标完成分别核对；没有预写乙醇求解程序。

本步要求真实模型从以下原文自行生成 Plan、输入和代码并完成计算与交付，另证明一次生成代码错误的有界修正；v55 / Pro-v5 的 live035 已完成前者，live036 未进入执行，真实修复完整交付仍待完成。v56 的非代码展示修复与定向证据单列，不追加一次原文重复，也不将旧成功改记为新版本真实运行：

> 请为乙醇生成结构，选择合适的方法和基组，优化结构并分析振动频率，说明计算条件和结果验证情况。

独立核验的预定规则见 [独立核验方案](independent-review-plan.md)。产品不会读取这些开发参考，也不会把开发评分写成生产 `QualifiedOutput`。

## 历史开发失败：live001—live008

首次真实自然语言 Run 为 `run_4a510aba347840b18119f25d73cb8f7d`，本机证据位于 `data/code-tools-step3/live-001/`。2 次模型 HTTP、8246 个已知 token、0 次 ORCA，最终 `waiting_user`，全部目标证据不足。首份语义提案被拒绝，纠正后的提案仍把方法/基组选择和未注册频率端口当成待用户回答的缺项，并引用旧水/甲烷检查范围。程序未启动计算；这是端到端失败，不是自主计算通过。运行前后 62 项源码/依赖文件 hash 未变。

随后修复代码入口的新旧契约混用和通用语义遗漏。改变的是后续候选，旧 Run、模型原文和消耗不回写。另有一次默认测试收集因新增 live/unit 文件同名中止；已将 live 文件改名，保留第一次收集错误日志。旧输入上下文的 12k 容量问题通过等价 schema 去重处理，未增加历史预算。

第二次真实 Run `run_16af3a6652f9422385483d6d4c4ffc2f` 正确保留一般目标和自动选型授权，完成 4 次资料检索，但官方引文未准确包含所选方法。10 次模型 HTTP、82175 token、0 ORCA，有限纠正耗尽后失败。随后修正文档 HTML 空白、表格标签与说明的整行提取、精确行标签排序，以及逐字段来源错误反馈；未降低逐字引文、hash、官方来源及选定值出现的门槛。

第三次真实 Run `run_b2d36b9e73e440ec93baaa79b4c728a8` 在语义阶段把已授权的待选型动作写入用户缺项，连续纠正无效，3 次模型 HTTP、12878 token、0 ORCA。随后为纠正项增加明确字段路径和修改指导，保留原目标、条件与最低证据；完整真实提案仍被拒绝，离线只移除误写的 unresolved 条目即可通过。前三次运行的冻结源码各自未变，全部保留，不能拼接为端到端通过。

前三次使用 `deepseek-flash` 的 `disabled` 推理配置；后续开发候选使用原协议已有的 `thinking_low`，仍保留同一调用/token/输出/期限上限。配置变化显式记录，不将不同候选当作同一冻结版本的重复验收。

第四次真实 Run `run_fb8ef1ef4ec34ef1bacea2e31861e647` 使用 `thinking_low`，3 次模型 HTTP、23329 token、0 ORCA。首提案缺身份消息引用，后续合法的初始结构 Goal 被程序错误要求具有优化后关系，纠正耗尽。随后修复注册输入 Goal 的语义、验收和交付路径，并保留真实未知项；旧 Run 不回写。前四次合计 18 HTTP、126628 token、估算费用 USD 0.0544575（不等于实际账单），全部未形成自主计算。详见[逐次失败证据](live-development-failures.md)。

第五次 `run_09e8e5fa96454f06bd849356fd4cae55` 使用 v37 冻结候选，完成语义登记、三次知识查询及基于官方原文的 B3LYP/def2-SVP 选择；规划响应达到 8192 completion token 上限，`finish_reason=length`，最终正文为空，程序按不可纠正的截断有界停止。新增 6 HTTP、66154 token、估算 USD 0.0329295，仍为零次 ORCA。前五次合计 24 HTTP、192782 token、估算 USD 0.0873870；五个原开发预约已经耗尽。下一候选使用 `disabled` 推理模式，当时第一次开发上限前瞻修订见[核验方案](independent-review-plan.md)，不修改旧 Run、回收旧预约或宣称第五次恢复成功。

预算修订于 UTC 14:59:25 显式执行，第六次 `run_ed39af66b23f4d23bc15ae599075f0d0` 使用关闭额外推理的新配置。它完成选型，但三份 Plan 引用了尚未生成的文件及 hash 占位符；先缺少原生输入参数，随后被格式校验拒绝，纠正耗尽。新增 10 HTTP、70763 token、估算 USD 0.0251088，仍无 Attempt/ORCA。前六次合计 34 HTTP、263545 token、估算 USD 0.1124958；[独立证据](validation/live006-planning-failure.json)同时核验原五次预约保留和新上限，仅余两个开发预约。后续只增强分步 Plan、实际文件引用、SDK 来源和资源字段的通用说明，不替模型填写计划或放宽校验。

第七次 `run_24a4a3c7acb5403f96e61734fe10f105` 使用 v38，完成有来源的选型、合法分步 Plan 和 4 HTTP 身份查询，但真实 OPI 准备进程被 XYZ 行形状校验拒绝；随后模型反复调用已完成的身份 Step，有限纠正耗尽。新增 11 模型 HTTP、89065 token、估算 USD 0.0288363，0 Attempt/ORCA。准备 helper 的实际 wall 1.547 s、CPU 1.21875 s、峰值提交内存 61931520 bytes 单列；Run 的计算 CPU 0 不代表所有工作 CPU 为零。[原始失败证据](validation/live007-preparation-failure.json)保留 stderr 和回执；当时 XYZ 未落盘，不能补写成原始证据。

随后修复 OPI 末尾空白行与验证器不兼容的问题，仅在校验视图忽略终端空行，原始字节不改；以后被拒绝的有界候选也归档为未合格证据。129 项单元回归通过、1 项显式烟测跳过；另开真实受管 OPI 探针 1 项通过。探针身份为合成夹具，0 模型、0 ORCA，不能算第七次恢复成功。见[独立准备探针](opi-preparation-fix.md)。

提交前将该探针的原始 859 字节 XYZ 送入后续原生输入准入，发现同一尾空行还会在原生入口被拒绝。随后只修正其验证视图和解析频率策略对已验证原子行的读取，原始 hash 保持不变，内部空行、额外原子/帧及非有限数仍拒绝。对应红绿测试和保留字节的离线准入见最终回归记录；未重新生成结构、未启动新的模型或 ORCA，不能称为完整真实执行通过。独立直接调用策略函数发现的空行索引异常仅为离线诊断，不是第七次 Run 的原始错误。

第八次 `run_171dba6a3a5d4cd4b9ba38a325171da0` 使用当时冻结的生产候选，3 次 HTTP、13201 token、估算 USD 0.0058206，仍在语义阶段失败。已保存的逐次原因是：缺少物理量原文并把待选型动作写成未知；初始结构 Goal 被添加科学几何关系；删去该关系后又缺物理量并重加伪未知项。程序给出字段级纠正，但模型没有完成合法修正；没有身份/结构调用、Plan 或 ORCA。见[完整提案与反馈](validation/live008-intake-failure.json)。

八次合计 **48 次模型 HTTP、365811 个已知 token、估算 USD 0.1471527、零次 ORCA**；48 次用量回执齐全，按冻结未缓存费率估算，未计缓存折扣，不是实际账单或所有产品成本。各 Run 的 62 项生产源码/依赖文件在自身运行期间不变，但不同 Run 之间有已记录修复和配置变化，不能作为同一版本的重复稳定性结果。八个预约全部保留，不回收失败剩余额度；没有运行人为代码故障种子对照。

交叉审查发现的额外科学条件与几何关系漏核也属于本次修复：无法证明明确温度/标准态等条件时保留未完成，单点输出不能冒充优化后来源。观察数值和科学解释仍未独立认证。

八次模型运行结束后的审查又发现共享 ORCA 额度缺口：SDK 与直接入口切换可漏核追加科学许可或追加额度。离线反例先记录 9 failed、3 passed，随后在两种预约入口和未启动预约的恢复分支统一按持久化 Attempt 顺序、SDK 父绑定及累计计数核对；纯旧入口初始多 Step 的既有分类保留。仅 Python 失败不自动算一次 ORCA，已有科学预约后的新 SDK 科学调用须有追加许可。历史缺父绑定时拒绝新的科学预约并要求对账，不能当作未消费。该修复在第八次之后完成，初版交付时未做新增真实模型或 ORCA 回归；定向正反例和故障窗口结果见当时的回归记录。后续 live017 的单次 SDK 成功不证明实际入口切换门禁。

## 后续候选与首次自主交付

live009—live016 的逐次模型提案、反馈、上下文容量问题与真实失败见[续作记录](continuation-failures.md)。其中 live016 在 Linux 容器运行模型脚本，因 SDK 依赖使用绝对路径而失败；同一模型读到 stderr 并提出相对路径和 Opt/Freq 修正，但修订计划未通过校验，没有第二次 Python 或 ORCA。v47 修复提交字段别名和容量退路问题，保持原生提交形状及完整校验约束；旧失败不回写。

live017（`run_411fb7f2c8ce48249d3fdb6755587142`）从原始中文请求完成有来源的 B3LYP/def2-SVP 选型、身份查询、结构准备、Plan 和现场生成的 Python/ORCA 输入。原模型代码、writer Artifact 与 sealed/task.py 逐字节一致，没有人工代填。气相 RKS、电荷 0、多重度 1；实际 ORCA 为 1 进程、MaxCore 192 MB，容器上限仍为 4 核/1024 MiB。一次 Python 和内部 ORCA 完成，归档优化结构、27 个频率及原始 stdout/Hessian/XYZ。生成的两项观察仍为 unverified，计算 Result 的 qualified_outputs 为空，终止解释合同 passed，交付按 reported_with_limitations 完成。完整执行与成本见[live017 自主运行记录](validation/live017-autonomous-optfreq.md)。

live018 是另一次明确附加故障说明的开发对照；在执行种子前，模型复用 prepare/writer 却漏掉 prepare 依赖的 resolve，Plan 被拒，纠正响应随后截断。其 0 Attempt/ORCA 不构成真实 NameError 或代码修复证据，详见[live018 索引](validation/live018-seeded-prelaunch-truncation.json)。v48 因此展示已完成 Step 的真实依赖，提供准确的缺失依赖反馈；程序不自动补 Step 或放宽图校验。v48 的原文复验 live019 仍未通过，后续受控故障对照须单独记录。

live019（`run_a7212378d09e4165aeecae161136c7bf`）最终 failed/partial，优化 Goal 证据不足、频率 Goal 满足。两次 Python 的 operation completed 和两次原始 ORCA 核验通过，均没有变成完整交付。第一次脚本按 `.xyz` 后缀取 files 中首项，实际读到输入 ethanol.xyz；生产来源门禁正确拒绝把它作为优化结构。第二次模型修订代码仍按后缀取首项；此外 Step 只声明 optimized_geometry，生成 manifest 却还包含频率，collector 因观察名超出冻结声明而整体拒绝，第二个 Result 没有注册观察。不能把生成 manifest 中的错误结构称为已注册的第二份观察。

live019 最后三个 stop 响应均缺少 JSON 闭合，属于 invalid_proposal_json，不是 token 截断；没有接受的最终模型解释。该 Run 消耗 21 HTTP、273034 输入 + 67929 输出 = 340963 tokens、估算 USD 0.1634250、2 次 Python、2 次 ORCA、1 次代码重试及 314 个声明工作单位，真实修复仍失败。执行与回复证据见[live019 交付失败审计](validation/live019-output-selection-and-delivery-failure.json)，原始科学输出和生成解释的区别见[独立科学审计](validation/live019-independent-scientific-audit.json)；原 Run 的 309 个文件 hash 保持不变。

v49 仅澄清通用 SDK 返回文件角色：files 也包括输入和依赖，后缀或映射顺序不能识别输出；须核对实际键、文件存在性和原始来源。不预写任务解法、不自动选择输出、不放宽观察声明或科学门禁。相关定向回归分别为 139 通过和 6 通过，均非完整默认套件。live020 随后在该冻结候选上完成原文真实交付：identity、prepared、optimized、freq 四个 Goal 均满足，13 HTTP、174660 tokens、1 次 Python、1 次 ORCA、157 个声明工作单位，code_retries=0；运行前后源码冻结未变。独立科学核验已通过：实际 B3LYP/def2-SVP、优化与响应收敛、原始结构及完整频率谱相符；模型脚本明确选择 job.xyz，9 原子坐标与原始最终结构逐值相同，27 个频率及 cm^-1 单位一致。Hessian 独立重算最大差约 0.00021624 cm^-1，结构刚体对齐最大差约 6.91e-9 Å，[完整独立科学审计](validation/live020-independent-scientific-audit.json)确认 185 个原 Run 文件 hash 未变，生产两项观察与终止快照逐值一致且仍为 unverified，不改写生产科学资格。live020 的[独立执行审计](validation/live020-autonomous-optfreq.json)记录 148837 输入、25823 输出 tokens，估算 USD 0.0756387。

live021 使用相同 v49 源码和配置，在请求中另加明确 NameError 故障要求，但开发测试观察器错误地要求最后一个模型决策必须为 call_tool，未识别生产循环既有的已激活模型 Plan 首个 ready Step 路径。因此它在科学预约前 failed/partial，0 Attempt、0 次 Python、0 次 ORCA；虽然模型写出了故障标记代码，真实 NameError 没有发生，不能计为模型 Python 失败或实际错误修复。[独立审计](validation/live021-seeded-observer-assumption.json)记录 11 HTTP、117744 输入和 19586 输出 tokens，共 137330 tokens，估算 USD 0.0588264，原 Run 与失败保留。

随后只修正开发测试驱动，使其只读核验生产既有的 activated model Plan 首个 ready Step；未改生产源码、提示或配置，未改写任何任务代码。gate、budget、outcome 定向测试 122 通过，对真实 live021 的只读回放也未修改任务；这不表示原失败 Run 已恢复或真实修复验收通过。live022 使用同一生产 v49 和 v4 配置，测试驱动与 live020 不同；live020 没有启用该种子观察分支，其原文成功证据仍有效。live022 已 failed/partial，16 HTTP、225275 tokens，真实执行 1 次 Python 并在第 3 行触发 NameError；0 ORCA、0 代码重试、0 声明工作单位。首版只有 359 字节故障占位代码，输入 manifest 和观察声明均为空，没有按受控请求生成完整计算流程。后续未执行有效修复，最终诊断为 `unchanged failed input cannot be retried automatically`；拒绝重复相同失败输入不能算成功修复。仅初始结构 Goal 满足，优化和频率仍证据不足。CPU 采样下界 0.720041 s、wall 1.781 s，resource_usage_complete=false。生产源码冻结未变，原始失败保留；其实际 NameError 不满足完整任务程序的有界修复退出条件，当时完整默认套件仍待运行。

## live023—live027：后续真实复验

v50 保留跨 Plan 的已结算 Python 失败历史与完整 frozen_step；这些事实不使旧 Step 重新 ready，不授修复许可。完整历史若被安全投影删除字段或替换路径，构建上下文直接失败；缺失/错绑 Result 或超容量同样拒绝，不把替换值当作原合同。相关最终定向 190 项通过。live023（`run_eb86a0ede94c4207bfd1668cdd38fbd3`）在该候选 completed/complete，prepared/optimized/frequency 三个 Goal 满足，16 HTTP、210739 tokens、估算 USD 0.0899238，1 次 Python、1 次四进程 ORCA、157 工作单位、0 代码重试。实际 B3LYP/def2-SVP、RKS、气相 0/1、MaxCore 192 MB；[独立科学审计](validation/live023-independent-scientific-audit.json)核对五项优化判据、SCF/CP-SCF、原始最终结构及 27 个频率，Hessian 独立重算谱最大差约 0.00021624 cm^-1。生成坐标和频率与原始输出逐值一致，计数、单位及来源正确，终止快照等于 Result 观察；197 个原 Run 文件未改，生产 qualified_outputs 仍为空。执行与成本见[live023 审计](validation/live023-autonomous-optfreq.json)。

live024 使用 v2 受控故障请求，但保留 Step 的依赖和字段格式提案未通过，15 HTTP、227166 tokens、0 Attempt/ORCA。v51 只增强缺依赖/producer 引用与 retained 格式的结构化诊断，不自动补 Plan 或修改模型提案；未执行旧 Step 的合法重定义继续通过。相关八模块 187 项、旧非代码 12k 容量 6 项通过。后续 live025 在条件选择时改写了官方引文的数学标记，纠正还出现 JSON 未闭合，最终 failed/partial；live026 以中文 query_name 查询 PubChem，真实响应为 404 / No CID found，模型误称 transient 并换 Step ID 重复相同查询，持久 Call 门禁拒绝，没有第二次外部请求、结构准备或 ORCA。三个 Run 的失败及单次费用见表中审计，不因诊断增强而改记历史成功。

live027（`run_0e30607d1dc4494f8db5fce5d2b02cbe`）由模型生成包含计算、解析和交付流程的完整首程序，实际触发受控 NameError；模型读取失败反馈后，以新代码 Artifact、新 Step key、同一个 logical_id 提交仅删除标记的修订，非代码合同保持不变，第二次 Python 已真实执行。随后现有 SDK 拒绝 `%output jsonpropfile end`：jsonpropfile 缺少标量值。两次 Python 均 failed，真实 ORCA 启动、ORCA 预约和内部工作量均为 0，消耗 1 次代码重试；18 HTTP、317810 tokens，最终仅初始结构 Goal 满足。该记录证明真实失败后的代码修订和重跑，不能证明修复后计算成功、科学通过或完整交付，也不能冒充自然发生的模型代码错误。当时 v52 随后补充 profile_contract 的通用块语法说明，未放宽准入；后续真实结果见下节。

## live028—live040：真实候选结果

live028 的实际受控 NameError、仅删 marker 和同逻辑重跑均成立，ORCA 原始方法、收敛、结构及完整 27 模式也通过独立核验；产品 completed/complete 仍不能替代独立完整交付。生成脚本仅保留正频率，交付 21 项，遗漏六个零模式且未披露筛选；坐标文本没有单位。v53 仅澄清完整序列和有来源单位，不增加科学 checker。live029 的 marker 放在 future 导入前，预启动 SyntaxError 无实际 Attempt，随后只前瞻澄清 v3 的合法导入顺序；历史 v2 文件和失败不变。live030 中文查询 404 后换 Step 重试被拒绝；v54 的首次可信英文别名指导不改变身份来源确认或重复查询门禁。live031 发生真实 NameError 后修复响应截断，live032 提案形状和依赖纠正失败，均无成功修复。

live033 原文完成真实 Python/ORCA，独立核对原始计算和完整观察均通过，但模型错误重绑 Goal，随后误称无注册 checker 就不能交付，最终 failed/partial。v55 仅提供准确 Goal、规则和已有路由的有界拒绝诊断，不补参数或改变准入；unsafe/超长规则标签以字符数/hash 代替全文。live034 的 ORCA 原始科学通过，Python 却只查 job.out 而非实际 stdout.out，失败后修复 Plan 未被接纳；1 次失败 Python、1 次完成 ORCA、0 重跑，无最终观察和模型解释合同。自然错误本可按既定方案提供有界修复证据，但该次没有完成。

随后仅用既有配置改为 deepseek-v4-pro / thinking_low 的 Pro-v5，保留 v55 生产源码、调用协议和逐 Run 预算。[配置与价格冻结](validation/model-pro-v5-configuration.json)和逐次原始回执分别保存，不把不同模型或版本作为同一配置重复稳定性样本。live035 从原文完成两个科学 Goal：实际气相 B3LYP/def2-SVP、RKS、0/1、4 进程、MaxCore 192 MB；一次 Python/ORCA，五项优化、SCF/CP-SCF、Hessian 独立谱及最终 XYZ 全部通过。生成 9 原子坐标与原始最终结构逐值相同，顶层 Angstrom 明确；频率完整 27 项、6 零/21 正，cm^-1，来源 hash、代码 hash 和终止观察均一致。201 个原 Run 文件未改。模型实际执行的是第二份脚本；第一份 nested units/null 草稿在首次执行前被改写，不能报告成单位未知的最终交付，也不算实际失败后修复。

live036 在同一 v55 / Pro-v5 候选上的预声明 v3 受控故障对照已 failed/partial，最后请求 HTTP 402，没有 Python Attempt、ORCA 或终止模型解释；gate 未到 before_science_reservation，未执行代码的静态风险不能当作实际失败。该次没有科学原始计算，不新建科学审计 JSON。当时 v3 要求完整首程序、实际 NameError、仅删 marker、同逻辑冻结合同、真实重跑和完整交付，原失败仍按当时协议保留。

随后在新 Run 前明确采用[前瞻 v4 协议](independent-review-plan.md)：保留完整首程序、首实际 NameError 前零 SDK、同逻辑和非 CodeRef 冻结合同，取消预先给出“只删一行”的修法；模型根据真实反馈自行决定代码修改，仍须真实后续 Attempt 和完整独立科学交付。这个受控简单故障对照不证明自然复杂错误诊断成熟度；若原文自然错误已完整修复则无需另设对照。当前不再追加原文重复，live035 保留原身份，后续真实修复尚未完成。

live037（`run_2a94bfb23c984971b6a94860f738815d`）于 2026-10-19 12:23:19 UTC 以 v56 / Pro-v5 与 v4 受控协议启动，12:35:35 UTC 已 failed/partial。完整首版 Python 在任何 SDK 前真实触发 NameError；模型随后独立写出新代码，但重抄冻结 Step 时先后混入 retained 字段、互斥 Goal 路由和改动输入 manifest，修复提案未获执行。17 HTTP、275841 tokens、1 failed Python、0 ORCA、0 代码重试；没有第二 Attempt、科学结果或最终完整交付，不创建科学审计空文件。未执行修订代码的静态风险不当作真实运行失败。原始冻结、执行、失败与费用见[live037 审计](validation/live037-pro-seeded-v4-execution.json)，已计入 001—038 累计；没有仅删 marker 的门槛，也没有据此放宽原任务合同。

v57 已按[0035](../../decisions/0035-declarative-code-repair-reference.md)实现显式 `{key, repair_of, code}` 提案：模型选择失败 Step 和新代码，程序从本 Run 最新已结算失败 Attempt 复用可信冻结非代码字段，形成候选后仍走全部原修订、Artifact/hash、权限、预算和执行校验。依赖闭包与目标路由仍须明确提交，不自动更改科学条件或写修复代码。引用与反馈最终定向 102 项通过；完整上下文、旧 schema 和拒绝边界证据见下节。v5 请求只调整提案表述，原 v4 和历史失败不变。

live038（`run_49eef7d8c16b4c02a8e84228ea830c18`）在 v57 / Pro-v5、受控 v5 下于 2026-10-19 13:06:22 UTC 已 failed/partial。完整首程序首先真实 NameError；模型独立写修订代码，随后明确采用新三字段形态，程序仅替换新 Step ID 和 CodeRef，保留同 logical_id、完整非代码冻结合同，真实第二 Python Attempt 已启动。第二次 SDK 因原生输入遗漏 `%method Z_MaxIter` 而拒绝，ORCA 预约和启动均为零。20 HTTP、335236 已知 tokens、2 failed Python、1 代码重试、0 工作单位；没有观察或科学结果，不创建科学审计空文件。实际提案、源码差分、冻结合同及 173 个原 Run 文件不变见[代码修复审计](validation/live038-code-repair-audit.json)。频率解析的静态风险尚未执行，不把它当作实际结果或已修问题。该次证明新接口与真实有界重跑，未证明成功科学计算或完整交付。

v58 按[前瞻决定](../../decisions/0034-first-generated-code-delivery.md)仅明确必需原生块的条件和冻结数值界限，旧 required_blocks 已包含 Z_MaxIter，原 admit_profile 未改。原生指导定向 51P；上下文 33P/1F 为旧版本文字断言，该项更新后 1P；[独立审查](validation/v58-independent-native-guidance-review.json)确认准入源文本及 AST 未变，历史 writer 离线输入界 57016≤64000。批次不相加，均不冒充真实科学成功。live039 未执行代码；live040 在同候选和同 v5 请求下真实进入 ORCA。

live040（`run_cfc6d8457b524df8a2b696109b312495`）先实际 NameError，再由模型重写代码并通过显式引用同逻辑重跑。ORCA 进程 exit 0、16 个文件已导出到主机，但逐文件不可变归档只完成 11/16 项即超过 30 秒期限，SDK 抛出收集不完整错误；生成解析/观察路径没有执行。收集失败回执结束于 13:38:47 UTC，早于用户成本控制暂停（13:39:03）；最终 13:39:29 为 paused/partial，两个目标均 insufficient_evidence，没有终止模型解释。只读诊断发现每次文件 import 重扫整个 Store 元数据，相关性能问题保留为已知未修复限制；按用户要求不新增优化、补归档或付费复现。

[独立科学审计](validation/live040-independent-scientific-audit.json)使用原 oracle 和容差核对现存 stdout、Hessian、XYZ：实际 B3LYP/def2-SVP、RKS、气相 0/1、4 进程、MaxCore192，五项优化及 SCF/27 方向 CP-SCF 通过；完整27模式=6零/21正，Hessian独立谱最大差约0.00021624 cm^-1、几何约6.91e-9 Å。192个原Run文件及22个已注册归档文件hash未变。Hessian已注册，stdout/XYZ仅在native目录保留；无生成观察可比，独立完整交付=false。审计初次对LF任务输入与CRLF封存输入字节相同的错误假设已保留，核实仅换行差异，实际封存/native/CodeRef hash一致，未改原文件或科学标准。执行、成本和暂停顺序见[完整执行审计](validation/live040-pro-seeded-v5-execution.json)。

## 分层真实证据

开发者编写的原生探针完成 1 次真实四核 ORCA，0 次模型调用；首次独立核验失败以及同一数据的修正核验结果均保留，详见 [探针记录](native-probe.md)。这只证明执行底座和独立检查方法，不能作为 Agent 自主证据。

历史 Opt+Freq 探针覆盖 HF/STO-3G。随后 live017 取得独立的真实 B3LYP/def2-SVP 自主计算证据，开发核验器直接对原始输出检查实际 DFT 组成、基组、电荷/多重度、26 电子、SCF 与响应收敛、五项最终优化判据，并核对最终 XYZ、完整 27 模式以及由 Hessian/质量独立重建的频率谱。核验通过：6 个投影零模式、21 个正振动模式，符合该构型的局部谐振极小值；不证明全局最低构象。生成观察与原始结构、频率、单位标签及终止快照一致，181 个原 Run 文件在核验前后 hash 不变，见[独立科学核验](validation/live017-independent-scientific-audit.json)。此核验未读取生成解析器作为期望值，未新增 ORCA，也未授予生产科学资格。

独立核验相关回归为 **187 passed、1 skipped**；跳过项是未启用真实容器测试，不计为通过。这组结果不等于最终项目全量回归，也不证明一般 DFT 组合均兼容。实际代码错误后的成功完整交付仍未完成；live036 未进入执行；live027 后续输入准入失败，live028 原始科学通过却有交付缺项，均不能与 live035 等原文成功拼接为修复通过。

中文名称查询另通过生产 Tool 实际取回公开来源，记录社区维护的名称映射、PubChem CID/SMILES 和图检查，明确不保证样品或电子态。开发检索探针实际取回两份官方方法/基组文档；这些探针没有填入真实模型 Run，未提供人工 Plan、脚本或选择结果。

## 保留边界

- 当前每个 Python Attempt 最多一个 SDK ORCA 请求；请求键在整个 Run 内不可复用，没有多计算、已完成回执复用或检查点恢复保证。已提交过 SDK 的失败脚本修复需要新的请求键，并继续累计原预算；未知执行先对账。
- 当前保守契约要求已有科学预约后的新 SDK 请求具有追加科学许可；代码重试额度本身不授予再次计算权限。混合入口追加用量按历史记录取下界，缺少可核对的父绑定时不能继续新增科学预约。
- 通用科学目标的条件来源当前要求同一 Python Attempt 的 SDK 原生回执。独立 `orca.run` 后再用另一 Python 读取其 stdout 可以产生未验证观察，但尚不能通过本契约完成科学目标；原始文件仍可保留与只读查询。
- 结构准备使用既有 Windows Job helper（1 核、1 GiB、30 秒上限），任务 Python 与内部 ORCA 使用所选 Linux 容器；这是两个明确记录的执行范围。OPI 烟测没有证明 Windows helper 的网络隔离，不能借容器隔离声明替代该事实。
- 执行准入限冻结的气相闭壳层方法/基组组合及有界优化和解析频率；数值频率、扫描、未知外部程序和未管理的执行影响拒绝。
- 生成观察、单位和来源位置均明确为未验证；缺生产频率检查器，不自动确认极小值，更不证明全局最低构象。明确更高科学证据要求不能降级满足。
- 极端非法的 100K 依赖名可放大为约 200K 诊断，使纠正上下文超出 64k；系统在 HTTP 发送前失败关闭并保留拒绝，Run 有界失败。该限制不表示越权或无限重试，本轮未修改冻结 v55 来扩容。
- CPU 成本是采样下界；没有每 Attempt 磁盘硬配额；未知执行保守占用，先对账。Windows LPAC 的 MPI 初始化限制保留，不切换为无约束执行。
- 计划层不同但等价的基组拼写仍可能保守拒绝；当前原生策略不支持的额外物理条件不能判为已满足。新能力不会自动升级历史结果。
- 远程集群、任意分子/电子态、正式重复稳定性、跨任务保留题和后续界面能力不在本次验证范围。

## 历史回归与当前验证口径

初版默认全量测试第三轮记录 **3765 passed、2 failed、178 skipped、1 warning**。两项失败是旧提示版本及说明文字断言；保留原始失败，修正两条测试预期后的四个完整模块 **138 passed**，原有 schema、来源与权限断言未删除。该全量测试运行期间五个生产文件发生变更，测试文件也未纳入启动时的 62 文件冻结表，故不能称为最终冻结版本全量通过。

初版后续上下文、文件引用反馈、OPI 准备、开发预算及共享 ORCA 预约修复分别采用当时候选定向回归。各批有重叠，不将数量简单相加。准确范围、源文件 hash、失败与复测关系见[初版回归记录](final-regression.md)及[机器可读证据](validation/final-regression.json)。文件名中的 final 不表示当前续作候选已通过全量回归；续作定向结果见[续作验证记录](continuation-validation.md)。未启用真实模型、ORCA、容器开关，或缺少 Windows 测试权限而跳过的项目均不算通过。

最后的共享预约门禁覆盖九个模块，**250 passed、1 skipped**；其中新增 21 项覆盖双向入口切换、换 ID、历史少计、旧入口兼容和五个预启动故障窗口。跳过项为 Windows 符号链接创建权限不足。中途测试夹具不符合旧适配器条件/组成而失败的记录同样保留，修正夹具没有放宽生产校验。Ruff 与差异检查通过，未重跑最终冻结版本的完整默认套件。

之后的 OPI 原始 XYZ 到原生准入兼容修复，离线红例为 **4 failed、9 passed**，相关扩大回归 **271 passed**；同一已保留真实 XYZ 的准入和 hash 复核通过。此处只验证读取与准入，没有新增结构生成或真实 ORCA 计算。

## v56 定向修复与用户收窄的验证范围

首轮冻结全量于 2026-10-19 11:36:17—12:14:05 UTC 完成，**4231 passed、23 failed、179 skipped**，9570 个冻结源文件和显式历史输入前后不变，见[公共摘要](validation/continuation-regression-001.json)。23 个失败中，15 项是历史时钟或精确展示断言，1 项涉及已声明的代码 writer 12k 限制，7 项暴露真实非代码容量回归；该分类不把失败改记通过。未启用真实调用的跳过项仍未验证。

v56 将新增完整拒绝候选、执行回执视图及配套指导限于代码授权场景，非代码保留原错误、ready、Plan/Result、来源、权限和预算；其末级恢复原有 schema 列式和共享。旧 `_schema` 显示合同省略 default 等注释，但原 registry 定义、实际默认及校验约束不变；代码分支完整字面合同不受此恢复影响。

查询反馈模块最终 **10 passed，8.10 s**。历史纠正输入由 11654 降至 11128；加当前输出 2000 与原最终回答预留 12000，总计 25128，仍低于原剩余 25303，余 175。V-07 四次输入 9612/11359/11703/8094，在原 12k/32k 预算下完成；其 300 tokens 是离线脚本回执，不是真实模型用量，且 0 Attempt/ORCA。两项非法反馈调用继续不能覆盖 Plan 或执行错误 Tool，原值、路径、来源、单位、科学资格及预算断言均保留。新 native view 分别精确检查 ready step_id 动作和命名 reader，不强迫已合法分开的动作回到同一 schema 分支。首次 4F、两个不足原型的 2P/2F、诊断 helper 的路径/终止元数据问题和新增断言 matcher 不支持数值关键字的失败均保留；不算产品或真实科学成功。

可提交的[查询修复摘要](validation/query-feedback-repairs-v56.json)记录初始失败、原型不足、最终 10 项通过及日志/XML hash；完整诊断索引为 `data/code-tools-step3/query-regression-diagnostic/query-regression-diagnosis-v56.json`（SHA-256 `9f12dc54ed9c2adb80e86132365b990ef5c364301c04638f432581c615a65986`），最终日志/XML 为同目录 `v56-query-module-r2/`。其他定向批次及代码授权上下文等价证据见[续作验证记录](continuation-validation.md)，重叠数量不相加。用户明确要求不再全量回归，因此取消后续全量计划；定向通过不代表最终候选全量通过。live035 不重标为 v56 原文运行，后续仍缺真实修复后的完整交付。

## v57 定向与显式修复入口

[引用与反馈定向](validation/v57-declarative-code-repair-final.json)最终 102 passed，23.40 s；[独立只读审查](validation/v57-independent-repair-reference-review.json)的 18 个负例全部拒绝，旧非代码 native schema 2173 字节逐字一致，真实 live033 的旧提案拒绝反馈重放完全相同。错误历史基线的探针失败保留并明确纠正，不当作产品故障或新的通过记录。

[上下文兼容定向](validation/v57-context-compatibility-final.json)先为 193P/1F，修正同一测试第一处过时文字断言后为 32P/1F，再修正它继续到达的第二处过时文字断言，两种容量参数最终 2P。原权限、字段、来源、低容量拒绝和预算断言保留；批次相互重叠，不相加成整体通过。首轮日志显示包装器的编码错误单独保留，不算额外 pytest 失败。此前全量 23F/179skip 不被这些定向结果重写；按用户要求没有再次全量。

live038 是 v57 的新接口真实执行证据，但科学与完整交付失败；live035 仍是 v55 原文成功，不改为 v57 原文或同候选重复证明。

## 本轮收尾范围与保留限制

用户最新明确只完成步骤 3 收尾，不进入步骤 4；停止 NameError 专项并取消其作为本轮交付阻断条件。正常路径 live035 已真实完整通过；受控修复的完整交付仍未通过，040 的收集超时仍未修复，二者如实保留。本轮等待用户验收，不把该范围调整写成修复成功或用户验收通过。

Flash 配置已完成纯本地验证，未新增付费 API/ORCA；40 个开发预约及全部历史成本不清零，余下 10 个不再自行消费。后续全量按用户要求取消，首轮 23F/179skip 与各批中间失败保留；定向通过不等于最终全量通过。提交与远端核对结果由本轮最终交付消息记录。

生产频率检查器、自然复杂错误成熟修复、正式跨任务重复可靠性、步骤 4 的多计算/恢复尚未实现或验证；步骤 4—7 未实施，不在本轮继续开发。资源采样下界、无每 Attempt 磁盘硬配额、完整实际账单未知等既有边界继续保留。
