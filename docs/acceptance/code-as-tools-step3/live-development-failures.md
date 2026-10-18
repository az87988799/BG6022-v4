# 真实模型开发失败记录：live-001 至 live-005

日期：2026-10-18。五次运行均从同一句自然语言进入产品，未由开发者注入生产 Plan、代码或 ORCA 输入：

> 请为乙醇生成结构，选择合适的方法和基组，优化结构并分析振动频率，说明计算条件和结果验证情况。

五次均未完成用户目标。累计 **24 次真实模型 HTTP、192782 tokens、零 ORCA、零 Attempt**。本记录保留这些失败，不以修复后的离线回放替代真实重跑，也不宣称步骤 3 验收通过。

[小型 JSON 证据索引](validation/live-development-failures.json) 包含完整 Run/model ID、严格调用顺序、原始文件与提案 hash、来源回执、冻结源码身份及成本。正文的 model ID 为缩写；所有顺序均来自 `run.model_records`，不按文件名排序。

## 运行与成本

| 运行 | Run ID | 最终状态 | 模型 HTTP | 已知 tokens | 冻结费率估算 USD | ORCA / Attempt |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| live-001 | `run_4a510aba347840b18119f25d73cb8f7d` | waiting_user；partial | 2 | 8246 | 0.0036609 | 0 / 0 |
| live-002 | `run_16af3a6652f9422385483d6d4c4ffc2f` | failed；partial | 10 | 82175 | 0.0270114 | 0 / 0 |
| live-003 | `run_b2d36b9e73e440ec93baaa79b4c728a8` | failed；partial | 3 | 12878 | 0.0056463 | 0 / 0 |
| live-004 | `run_fb8ef1ef4ec34ef1bacea2e31861e647` | failed；partial | 3 | 23329 | 0.0181389 | 0 / 0 |
| live-005 | `run_09e8e5fa96454f06bd849356fd4cae55` | failed；partial | 6 | 66154 | 0.0329295 | 0 / 0 |
| 合计 | — | 目标均未完成 | 24 | 192782 | 0.0873870 | 0 / 0 |

模型为各次冻结配置中的 `deepseek-flash`；live-001 至 live-003 的 profile 为 `disabled`，live-004/005 为 `thinking_low`。费用是系统按已知输入/输出 token 和冻结的非缓存峰值费率（每百万输入 $0.30、输出 $1.20）计算的估算，**不是实际账单或已查询扣费**；各报告均记录 `provider_billing_queried=false`、`provider_charged_amount=null`。估算未减去缓存命中优惠。未知 token 占用为零，表示这些模型调用都有用量回执，不表示所有产品成本已获独立审计。每次运行冻结的模型调用上限为 24；live-002/003/004 因单次决策的有限纠正耗尽停止，live-005 因不可重试的截断回复停止，均非单个 Run 的 24 次总额度耗尽。

### 开发预算快照

下表截至 live-005 结束，是原 `version=1`、5 次名额开发批次的快照，不包含任何后续显式批次上限修订。开发账本按每次运行的最坏上限先预约；失败或截断不会退还运行名额或预约额度。原批次已全部预约，**没有可用于新 Run 的剩余名额或额度**。旧 Run 中未使用的调用或 token 不能据此重新授予新 Run。

| 项目 | 原批次总上限 | 五次已预约 | 五次实际使用 | 原批次新运行余额 |
| --- | ---: | ---: | ---: | ---: |
| 运行次数 | 5 | 5 | 5 | 0 |
| 模型调用 | 120 | 120 | 24 | 0 |
| 模型 tokens | 6000000 | 6000000 | 192782 | 0 |
| ORCA 启动 | 10 | 10 | 0 | 0 |
| 内部工作量 | 2000 | 2000 | 0 | 0 |

JSON 内嵌该五次运行时点的原账本及文件 hash；其路径可随之后的显式批次修订变化，本记录不据此扩大旧 Run 预算。另有 [开发者编写的原生执行探针](native-probe.md)：1 次真实 ORCA、零模型调用、93 单位声明工作量，独立于上表五次自主运行及其预约额度，不能算作模型自行完成任务。

## live-001：归一化被接受，但错误地停在用户缺项

| 顺序 | model ID 前缀 | 原始动作与结果 | tokens | 当时的拒绝或停止依据 |
| --- | --- | --- | ---: | --- |
| 1 | `model_f766cd0b` | normalize_request；拒绝 | 4134 | 下一请求保存 `StoreError: general quantity must retain an exact user phrase`；频率 quantity 使用英文，未保留用户中文原句 |
| 2 | `model_5edb6a87` | normalize_request；接受后 waiting_user | 4112 | 方法/基组记为 unknown，频率 port 记为 unresolved，并保留待选型/未注册频率端口缺项；没有随后计算 |

第一条回复还要求 qualified；第二条改为一般观察，但仍把用户已经授权的选型当作等待用户回答的事项，并生成了旧水/甲烷范围的 unsupported 通知。原上下文同时存在新通用意图说明与旧范围表，形成冲突。第二条曾被接受是历史事实，不能改记为当时被拒绝。

最终三个 Goal（优化、频率、条件与验证说明）均为 `insufficient_evidence`；没有 Plan、Tool Call、身份查询、结构准备或 Attempt。报告中的条件缺口与 `goal_has_no_evidence_binding` 均保留。

后续修复区分了旧专用科学资格范围与一般执行范围，新增有限语义纠正，并保留真实 unknown、最低证据和原始 Run。该修复不使本次运行变为成功。

## live-002：真实资料取得，但条件选择连续被拒绝

| 顺序 | model ID 前缀 | 原始动作与结果 | tokens | 当时保存的依据 |
| --- | --- | --- | ---: | --- |
| 1 | `model_2a401ef3` | normalize_request；接受 | 4249 | 两个一般 Goal、optimized 几何关系和方法/基组授权均保留 |
| 2 | `model_6d03598c` | knowledge.search `guide_method`；完成 | 6455 | 打包选型指南，无网络 |
| 3 | `model_3f95df19` | knowledge.search `guide_method`；完成 | 7197 | 再次检索同一打包指南，无网络 |
| 4 | `model_e4fdf1b2` | knowledge.search `orca_dft`；完成 | 7715 | 真实 ORCA 官方 DFT 文档 GET，有界片段 |
| 5 | `model_71b7a2eb` | select_conditions；拒绝 | 9240 | 下一请求保存 `selection source hash or exact excerpt does not match retrieved evidence` |
| 6 | `model_b0ac6c12` | select_conditions；拒绝 | 9288 | 下一请求仍保存同一来源匹配错误 |
| 7 | `model_4535bccf` | knowledge.search `orca_basis`；完成 | 8983 | 真实 ORCA 官方基组文档 GET，有界片段 |
| 8 | `model_bc043855` | select_conditions；拒绝 | 9672 | 下一请求保存 `selected method/basis must occur in the quoted official source` |
| 9 | `model_0e1a410d` | select_conditions；拒绝 | 9692 | 下一请求仍保存同一所选值/官方引文错误 |
| 10 | `model_0d2d90dd` | select_conditions；拒绝，随后 failed | 9684 | Run 保存 proposal_rejected / StoreError，随后保存有限纠正耗尽；末次具体错误文本没有单独持久化 |

对原始回复和已保存片段的只读比对显示：第 5、6 次的 method 引文把原片段中的换行改为空格，因此不是逐字子串；第 5 次的 basis 引文不含其选择值，第 6 次改引项目指南，仍不满足官方资料门槛。第 8—10 次 method 选择 `B3LYP`，引用却是讲述 `(meta-)GGA` 成本的段落，引用中不含该选择值。这些比较来自保留的原始字段，JSON 明确标为 `read_only_source_comparison`，不伪装成当时已经保存的精确字段反馈。

程序当时的错误只给通用句子，缺少 `choices.method.sources[n].quote` 或 `choices.method.value` 的定位。后续反馈修复逐项定位 result ID、文档 hash、逐字引文和选择值；检索渲染也统一 HTML 空白、保留表格行的标签与说明，并按通用完整标签匹配排序，避免展示本身破坏可引用内容。新回执标明渲染版本，原文档 hash 与片段 hash 分开保留；旧回执不改写。逐字、官方来源及所选完整标识出现于引文的门槛仍保留，不提供替代配方。离线副本中的成功引用仅验证这个门禁，不证明所选方法的科学适用性。

最终两个 Goal 均为 `insufficient_evidence`，缺 `goal_has_no_evidence_binding`。method/basis 未提交选择，Plan、身份查询、结构准备、Attempt、内部 ORCA 调用均为零。

### 实际资料与 hash 的核验范围

四个 knowledge.search Call 中，两次读取打包指南，两次执行真实官方文档 GET。以下 hash 为原回执记录的完整文档 SHA256；JSON 另记录每个 Result 文件和片段 hash。

| 资料 | 来源类型 | 文档 SHA256 |
| --- | --- | --- |
| Method selection guide 1（检索两次） | project_guide；无网络 | `63c605df3b2d6629c734512d5fbfe3ba1612650224f7bc5171ac2e48b3ef952b` |
| [ORCA 6.1 Density Functional Theory](https://www.faccts.de/docs/orca/6.1/manual/contents/modelchemistries/DensityFunctionalTheory.html) | official_document；真实 GET | `94424fff221bde28abf4a32b7f1649771dcfde5585360082816107d4a6032c0c` |
| [ORCA 6.1 Basis Sets](https://www.faccts.de/docs/orca/6.1/manual/contents/essentialelements/basisset.html) | official_document；真实 GET | `48750fa1c9dc25992c9e339648e52ef0f63423a7ad196e33b78609eb7143c4b6` |

本次整理重新计算并核对了 Result 文件与已保存 `excerpt_sha256`。完整远端文档字节没有留存在这批 Run 的原始文件中，因此不能独立重算其全文 hash；上表只能称为当时检索记录的值。片段的 `coverage.complete=false`，不表示覆盖整本手册，也不证明数值或方法选择科学通过。此次文档整理没有重新联网取证。

## live-003：待执行动作仍被写成 unresolved

| 顺序 | model ID 前缀 | 原始动作与结果 | tokens | 当时保存的依据 |
| --- | --- | --- | ---: | --- |
| 1 | `model_6247e5e0` | normalize_request；拒绝 | 4223 | 下一请求保存 correction：path 为 `unresolved`，reason 为 `available_selection_or_observation_action_is_not_a_missing_user_answer`，没有逐项 instruction |
| 2 | `model_9729a3a3` | normalize_request；拒绝 | 4324 | 仅把 `method_and_basis_selection_pending_knowledge_search` 改为 `method_and_basis_selection_pending`；下一请求保存同一 correction |
| 3 | `model_c07efb6e` | normalize_request；拒绝，随后 failed | 4331 | 改写为更多 Goal，遗漏逐字 quantity，并把频率验证标准记为未明确；Run 保存 ProposalError 类别与最终纠正耗尽，末次完整拒绝细节未单独持久化 |

前两条其余字段已表达目标与自动选择授权，却因错误的待执行事项阻塞归一化。原错误没有清楚要求移除该条目，模型只改措辞。后续把每项反馈改为精确 `parameters.*` 路径和可执行说明：已授权选型/检索是后续 Agent 动作，删除对应假缺项；没有真正未知时 `unresolved=[]` 或省略该字段，保留 automatic_selection、Goal 和最低证据，不能直接填入方法/基组。

原 Run 仍停在 Request v1 的 `raw_request`，其 Goal 为 `insufficient_evidence`，没有 Tool Call、Plan 或 Attempt。后续离线回放确认：首条原提案只清空或省略 unresolved 即可被接受，原 notices 无须删除；这是程序门禁验证，**不是第四次模型调用或本 Run 的恢复成功**。

## live-004：程序把输入几何误当作优化结果要求

| 顺序 | model ID 前缀 | 原始动作与结果 | 输入 / 输出 tokens | 当时保存的依据 |
| --- | --- | --- | ---: | --- |
| 1 | `model_569c6d6b` | normalize_request；拒绝 | 3585 / 4692 | 下一请求保存参数 schema 错误：`parameters.identities[0].message_id` 缺失 |
| 2 | `model_dd0070cf` | normalize_request；拒绝 | 3647 / 3504 | 下一请求要求 `parameters.goals[0].geometry_relation=optimized`，reason 为 `preserve_explicit_geometry_relation`；但该 Goal 的 port 是 `prepared_geometry` |
| 3 | `model_c4ce5564` | normalize_request；拒绝，随后 failed | 3719 / 4182 | 改把有 optimized 关系的优化 Goal 排在第一，仍保留独立输入几何 Goal；Run 仅保存 ProposalError 类别与有限纠正耗尽，末次精确拒绝路径未单独持久化 |

首条回复确实遗漏身份来源的必填 message ID，程序按 schema 拒绝有据。第二条修正后，把 `prepared_geometry` 输入目标（quantity 为“结构”、geometry_relation 为 null）与 `optimized_geometry`、振动频率输出目标分别表达，后两者具有 optimized 关系。程序却因该输入 Goal 的逐字用户引文还包含“优化结构”，要求输入准备也标记为 optimized；这是程序将输入注册与计算输出混淆的缺陷。第三次请求保存的明确反馈足以证明这次错误要求，不能把它归为用户没有说明需求。

末次提案把优化目标移到首位，保留 prepared_geometry 为另一个 Goal，但仍被拒绝。没有下一次请求保存末次详细错误，因此本记录不从后续源码或重放反推其精确路径。原 Run 停在 Request v1 的 `raw_request`，最终 `failed / partial`；缺项仍为 `missing:goal_definition` 和 `goal_has_no_evidence_binding`。没有 Plan、Tool Call、身份查询、结构准备、资料检索、Attempt、内部调用或 ORCA 预约/实际启动。driver 的 `error=null` 只表示驱动未抛出异常，不表示产品目标成功。

后续输入 Goal 契约修复须保持原输入检查，并区分准备与优化；它不能改写这次失败，也不能替代新的真实模型运行。

## live-005：选型成功，后续回复截断且没有可用提案

| 顺序 | model ID 前缀 | 原始动作与结果 | 输入 / 输出 tokens | 当时保存的依据 |
| --- | --- | --- | ---: | --- |
| 1 | `model_6d0713f8` | normalize_request；接受 | 3618 / 4316 | Request v2 保留优化、振动频率和条件说明三个 Goal，以及 method/basis 自动选择授权 |
| 2 | `model_a4294f72` | knowledge.search `guide_method`；完成 | 8266 / 314 | 一次打包指南读取，无网络 |
| 3 | `model_8c28933e` | knowledge.search `orca_dft`；完成 | 9016 / 302 | 一次真实官方 DFT 文档 GET；`html-readable-2` 片段保留 B3LYP 表格行 |
| 4 | `model_55cf99a9` | knowledge.search `orca_basis`；完成 | 9972 / 331 | 一次真实官方基组文档 GET；`html-readable-2` 片段保留 def2-SVP 标签及说明 |
| 5 | `model_2731c011` | select_conditions；接受 | 10528 / 1082 | Request v3 持久化 `method=B3LYP`、`basis=def2-SVP`，来源均为 auto_selected，绑定官方 Result、文档/片段 hash 与逐字引文 |
| 6 | `model_04939b28` | 没有解析出的动作；fatal 截断拒绝 | 10217 / 8192 | HTTP 200，`finish_reason=length`、`error_category=truncated`、`retryable=false`；保存的 `raw_content=""`、`proposal=null` |

第 5 次选择的持久化 decision 与 Request、官方回执、逐字引文及片段 hash 已独立核对。实际值为带连字符的 `def2-SVP`。这证明本次引用与选择门禁通过，不证明方法适用性或科学计算通过；选择理由仍是模型的判断。资料回执的 `coverage.complete=false`，全文远端字节没有保留，不独立宣称重算了其文档全文 hash。

归一化和选型已完成后，第 6 次响应用满冻结的 8192 completion token 上限，返回 length 结束。模型记录的实际消耗为 18409 tokens，估算 $0.0128955，计入本 Run 累计；空正文不等于零用量。原始响应记录没有完整提案，也没有保留隐藏推理正文，因此不能断言这 8192 tokens 的具体内容或模型原本准备执行的动作。**不存在可重放的有效计划、代码或提案，不得把截断当作可续用的成功响应。**

Run 的最后 decision 保存 `recovery_kind=fatal`。最终通用诊断虽为 `bounded proposal correction or transport retry exhausted`，但本次截断后没有新调用，不能据此说发生过多轮纠正或单个 Run 的总调用额度已耗尽。原 Run 保持 `failed / partial`，三个 Goal 均缺 `goal_has_no_evidence_binding`；没有 Plan、身份查询、结构准备、Attempt、内部调用或 ORCA 启动。

## 冻结、完整性与后续验证

五次启动冻结时间分别为 UTC `13:57:17.570045`、`14:12:03.115281`、`14:22:31.901983`、`14:36:50.578020`、`14:52:12.893590`；live-004 于 `14:37:47.662305`、live-005 于 `14:53:42.030889` 结束。各自 outcome 的 `source_changed_during_run=[]`，这是驱动的前后 hash 比对记录，本次整理未重建旧源码树。五次同属 Git 基线 `7860db02b7fad0be4b491d0a83fab9e9eb885f78`，但工作树源码不同，必须以各次 freeze 中的逐文件 hash 为准，不能只凭 Git HEAD 宣称源码相同。每次冻结清单有 62 个源码/依赖条目；live-001 至 live-003 提示版本为 `agent-json-v35`，live-004 为 v36，live-005 为 v37。JSON 保存冻结文件 hash、源码条目数量及关键模块 hash；完整冻结清单保留在各次本地运行目录。

本次逐一核对了 24 次 HTTP 200 调用的保存响应记录 hash、canonical 请求 hash、调用顺序、token/费用求和以及报告状态；23 条非空提案与原始 JSON 逐值相等，末次空正文与截断状态单独核验。导出前后 153 个原始 Run/证据文件字节未变；审计只读取原批次账本并留存快照，没有改写它。provider 原始 HTTP 响应的 hash 单独标为记录值；未取得原始 HTTP 全文时不声称重新核验它。导出仅使用仓库相对路径和必要摘要，不含凭据、绝对用户路径、完整请求上下文或冗长全文。

各次修复当时的离线证据包括：live-001 回放及旧通知回归 29 passed；live-002 来源反馈相关回归 63 passed；live-003 反馈与相关回归 68 passed。这些是各自源码版本的历史测试结果，不是最终候选的全套结果。夹具和日志 hash 见 JSON 的 `post_run_offline_evidence`，其中也保存 live-004 原提案夹具的身份。第五轮首次通过归一化及选型，但其最终截断失败仍原样保留。离线测试不消耗真实模型调用或 ORCA 额度，不改变五次原始 Run、原始提案或历史成本。

这五次已证明一次有来源的自主选型提交，仍未证明模型可自行完成结构取得、任务代码编写、内部 ORCA 执行、振动频率观察与有来源的最终交付。任何后续开发批次和真实模型结果须单独记录，不能从独立原生探针、离线回放或零 ORCA 的入口运行推断。
