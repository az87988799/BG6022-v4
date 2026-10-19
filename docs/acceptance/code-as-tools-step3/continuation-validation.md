# 步骤 3 续作：v39—v49 局部验证记录

记录日期：2026-10-19。本记录汇集 `agent-json-v39` 至 `agent-json-v49` 的局部验证，各候选的冻结范围和中间失败按下文保留；它不是最终候选全量回归。v47 的 live017 首次完成原文乙醇请求的自主真实计算和带限制交付，随后[独立科学核验](validation/live017-independent-scientific-audit.json)通过，相关回归为 187 passed、1 skipped，仍不授予生产科学资格。live018 未执行故障种子，live019 为 failed/partial；v49 原文请求 live020 completed/complete 且[独立科学核验通过](validation/live020-independent-scientific-audit.json)。live021 在首次计算前被开发测试驱动的错误选择假设阻止；驱动的窄修和只读重放见文末，原失败不回写。实际代码错误的完整有界修复与最终回归仍待完成，不能记为步骤 3 完成或验收通过。真实运行和总体验证以[续作记录](continuation-failures.md)和[主报告](report.md)为准。

以下测试集合重叠，不能将通过数相加为独立测试覆盖率。测试中的脚本、纠正后的候选和模拟工具结果均为离线验证；原真实模型回复、原 Run 与失败记录没有被改写。模型是否采用反馈、是否完成真实 ORCA 和科学交付，必须另以真实运行证明。

## 实现与失败对应

| 候选 | 已确认问题 | 窄修与保留边界 |
| --- | --- | --- |
| v39 | live008 初次候选同时缺量名、把初始结构准备声明为科学几何关系、把已授权选型写成用户未知；纠正只逐次暴露部分错误，导致反复回退。 | 聚合独立的语义纠正；输入端口仍按原注册检查处理，一般科学 Goal 保留用户原始物理量；不补量名、不填物理值、不删除 Goal。解释实际结果的既有 `explain_results` 交付要求与独立知识问题分开说明。 |
| v39 | 代码上下文要求遵守冻结资源，但未显示具体资源；SDK 文件回执与 Artifact 元数据易混淆；失败日志位于通用前三个 Artifact 之外。 | 只投影冻结资源白名单；明确 SDK 实际返回键、原始文件名与只读副本路径的区别、输入统一 `job.inp`、观察来源绑定规则；增加 `diagnostic_artifacts` 的已登记 stdout/stderr 索引。索引不读取文件，不宣称验证，也不增加权限。 |
| v39 | 加入全部修复提示后，一个既有 12k 结果反馈上下文超限。 | 只在真实已有失败 Python Attempt 时展示修复专用约束，保留通用代码契约；没有增加旧 Run 容量。 |
| v40 | live009 已完成身份解析和结构准备，但 selected Results 未再包含身份结果；空 ready 集被省略，模型重复提议即时执行解析。 | 显式保留协调者传入的空 ready 集；按当前 Step 与真实 Call/Attempt 收据显示执行状态；原生 `call_tool` schema 只枚举确知 ready 的 Step 和合法立即工具。没有 ready 事实的历史投影不假造空集；failed/unknown/settled 不等于科学通过。 |
| v40 | 准备结果的真实 XYZ hash 被通用来源摘要隐藏；立即工具额度过滤误伤知识查询，或改变已规划读步骤的调用契约。 | 对已注册 `write_input_artifact` 输出且 Artifact ID 属于 Result 的真实合法 sha256 直接展示；实际消费仍校验。立即读用 evidence_reads，知识工具仍按其 knowledge_queries；已规划读步骤保持原调用形状，由 Store 做实际成本门禁。 |
| v41 | live010 三次初始规划中，第一和第三次缺代码 Artifact，中间一次虽改为 writer，却把未来准备输出写成 EvidenceRef。每轮只发送错误，没有上一份被拒绝候选。 | 从已通过不可变回执校验的同一 logical request、相同 basis 的最近纠正回复恢复候选，放入 `DATA.rejected_proposal`。只保留一个候选，不放进 AUTHORITY/CONTROL，不自动修改提案；原错误卫生和执行门禁不变。未实施工具目录裁剪。 |
| v41 | 原候选可能包含大段任务代码、路径或不可信权限文本。 | 优先完整 JSON；CodeRef 和任务文件名保持字面值。敏感或主机路径文本整字段明确省略并附原值 hash，不把改写脚本冒充原文；超大文本或候选明确标为不完整。仍受原输入预算约束，原回执保持不变。 |
| v42 | live011 首个语义提案省略全部条件，却在 reason 中声称配置默认值会自动应用；Request 实际仍未知，下一轮转入澄清。 | 对必需科学 Goal 中确实遗漏、且现有 `_field(source=default)` 门禁允许采用的配置条件，返回显式采用要求；配置默认只是模板，模型必须输出 value/source/default_rule。只给配置中的合法值，不写入 Request。 |
| v42 | 默认遗漏反馈可能误覆盖真实未知、跨分子条件或纯文件任务。 | 复用现有用户文字、scope 和 provenance 校验；已提供的全局/分子/Goal 条件、现有值及 automatic_selection 不被替代。知识、只读、输入准备和通用 artifacts/observations 集合不因附带系统元数据而新增电子计算默认要求。 |

## 真实回复的离线保留方式

- live008 使用既有 `validation/live008-intake-failure.json`，文件 SHA-256 为 `0d56df8c1638c9fa9d39cb88c46b8f485ca9083e7efc5c94946747918d76e24d`。回放仅重新绑定测试消息 ID；纠正值在测试中显式构造，不能算真实模型自主修复。
- live009 的可选本地回放读取保留的 `run_acb8d2a0e9ed400896f23533af95d9f3`，只构造上下文并比较前后原模型文件 hash。便携的核心正反例不依赖该本地 Run；该次定向运行未跳过此回放。
- live010 第二次被拒绝提案保存于 `tests/fixtures/code_contracts/live010-rejected-plan.json`。来源模型为 `model_9b10053f4b9a433ea64c38688be0561a`，原 response 文件 SHA-256 为 `9a2cc7e467433a64f9a4222dec2c0a458cbefff62c720f1d3e59f6f901922734`。测试确认整份候选可见；另用真实离线 `_decision` 调用链证明第三轮只收到第二份拒绝候选，以及重启后纠正预算不会重置。
- live011 的省略条件提案保存于 `tests/fixtures/code_contracts/live011-omitted-defaults.json`。来源模型为 `model_0c9a3c5e7d0b40af851bb42bf8ebcd04`，原 response 文件 SHA-256 为 `ef37badab59d25624739dfa29f25cb9d3ee72a2fd3ad92b0f63769ba9bfabde2`。原提案在新规则下拒绝；随后显式采用默认值的成功候选为测试构造，不代表原 Run 被修复或重新执行。
- live004 的原始 fixture 保持原 SHA-256 `6950ada2708b20820d11c7eb1e728831ed428cb249e911095514275dfa9625e0`。其索引 1 曾省略配置条件，v42 测试先验证拒绝，再明确构造默认采用；输入科学冒充负例通过 `prepared_geometry` 端口定位目标，避免依赖不同回复的列表顺序。

## 局部测试结果与中间失败

XML 均保留在本地 `data/code-tools-step3/`，不把该目录整体纳入提交。表中时间来自 XML testsuite.time；终端总耗时可能略有差异。

| XML 文件 | 通过 / 失败 | 秒 | 说明 |
| --- | ---: | ---: | --- |
| `intake-live008-red.xml` | 0 / 5 | 1.903 | 修改前的预期红例，证明聚合反馈缺失。 |
| `intake-and-code-context-v39.xml` | 62 / 3 | 36.719 | 旧错误路径断言、旧 SDK 提示断言及一个真实 12k 容量回归。 |
| `intake-v39-capacity-recheck.xml` | 7 / 0 | 5.277 | 修复专用提示按真实失败条件展示后复验。 |
| `intake-and-code-context-v39-final.xml` | 138 / 0 | 60.677 | v39 六模块最终定向。 |
| `generated-failure-discovery-v39-final.xml` | 10 / 0 | 4.946 | 同候选重新验证诊断文件发现。 |
| `context-ready-v40.xml` | 6 / 2 | 16.732 | 新 fixture 预算/调用形状配置不足。 |
| `context-ready-v40-regression.xml` | 61 / 5 | 28.948 | 新 fixture 的原生投影/额度假设及真实立即工具额度过滤回归。 |
| `context-ready-v40-final.xml` | 8 / 1 | 16.982 | 零读额度 fixture 未真正设置为零。 |
| `context-ready-v40-regression-final.xml` | 65 / 1 | 25.331 | 已规划读步骤被全局过滤，违反原调用契约。 |
| `context-ready-v40-regression-final002.xml` | 66 / 0 | 24.348 | v40 四模块最终定向，恢复原 Store 成本门禁。 |
| `rejected-proposal-v41.xml` | 25 / 2 | 19.438 | 新测试错误假设 revision 接受记录含 action；直接调用 `_decision` 的重启 fixture 绕过正常目标状态初始化。 |
| `rejected-proposal-v41-final.xml` | 65 / 0 | 36.585 | v41 四模块最终定向；两个问题仅修测试设置/断言。 |
| `omitted-defaults-v42.xml` | 58 / 17 | 35.130 | 新 scope fixture 缺必需 goals 字段；另四项旧 live004 预期与新的显式默认采用要求不符。 |
| `omitted-defaults-v42-newtests.xml` | 10 / 7 | 1.434 | scope fixture 未清空 Request 的历史默认 explicit provenance，现有保护正确拒绝默认。 |
| `omitted-defaults-v42-final.xml` | 75 / 2 | 32.768 | 改用条件完整的历史回复后，负例仍使用旧列表索引，误改了科学 Goal。 |
| `omitted-defaults-v42-final002.xml` | 77 / 0 | 29.502 | v42 五模块最终定向；负例改为按准备端口定位，保留真正科学冒充拒绝。 |

最终定向集合：

- v39：`test_code_context`、`test_general_input_goals_live004`、`test_general_intake_correction`、`test_general_intake_live003`、`test_general_intake_live008`、`test_prompt_contract_versions`。
- v40：`test_code_context`、`test_context_action_budget`、`test_context_execution_facts`、`test_context_ready_execution`。
- v41：`test_agent_diagnostics`、`test_code_context`、`test_generated_failure_discovery`、`test_rejected_proposal_context`。
- v42：`test_code_context`、`test_general_input_goals_live004`、`test_general_intake_correction`、`test_general_intake_live008`、`test_omitted_authorized_defaults`。

上述 XML 均为零 errors、零 skipped；失败数只指 assertions/测试失败。修改文件的 Ruff 与 `git diff --check` 已通过。没有用扩大旧 Run 预算来消除 12k 回归；各次冻结后的源代码检查另由独立运行审计记录。此处不替代最终统一回归或 live012 结果。

## 最终定向 XML 的 SHA-256

| 文件 | SHA-256 |
| --- | --- |
| `intake-and-code-context-v39-final.xml` | `e3127dc878ff2cec677f34b1aa56fa0ae032f811b974a87af376840c27fe3f0e` |
| `generated-failure-discovery-v39-final.xml` | `cef84a370cc4ae0bbaf05a877e898df05d39169353d1087f283c89bf807117f0` |
| `context-ready-v40-regression-final002.xml` | `60d0f2fe383652e43e4b6783a33d4e1b4c5bbae4ebe504db2ce425469a98799c` |
| `rejected-proposal-v41-final.xml` | `7631a6d67f4cc710b205259841663235eb79c8954c8758fba436606620717653` |
| `omitted-defaults-v42-final002.xml` | `a2f0f499b25914bf4faf0cabb058a3c0849cb598a1d727ba5c1d392bcda62d51` |

## v42 冻结时本子任务相关文件的 SHA-256

这些是开始 live012 前读取的文件字节 hash；未来候选若修改，不应把本表称为最终交付 hash。

| 文件 | SHA-256 |
| --- | --- |
| `src/orca_agent/semantic.py` | `28f8242651c73b1763836096e02e63c8ab6b03ae2102a735503d4709a811f019` |
| `src/orca_agent/context.py` | `de9801570f3bdd82efcbecf2b04f5c9b939859834247b7e3b7f394ea062ad89d` |
| `src/orca_agent/readable_context.py` | `9cebb0b43be31a48f4147c0f53448c8555a993fe5fe13b2cf5a8c0c91ce92d67` |
| `src/orca_agent/decision_purpose.py` | `9c897e0c7243cce380113a25c3ae05087aaf3885860c3885e178010fadd51583` |
| `src/orca_agent/agent.py` | `225ddd7a32b2dc1b39e1d81ead46a898767b2ca080d6e6f92a599be2efea538f` |
| `tests/unit/test_general_intake_live008.py` | `018d940f503bfb0bc0c9fce912c08b1bcf84845f5a55ae738051defd7019aa54` |
| `tests/unit/test_context_ready_execution.py` | `068861abe58f9289541985db9d1ab6b54d0e911a41a902c87aacdebe55be07ed` |
| `tests/unit/test_rejected_proposal_context.py` | `a515b4908df12614924850a441b2c7dff0b454aef912ae0b1d64e95d5aed010f` |
| `tests/unit/test_omitted_authorized_defaults.py` | `614bf6cdedcc567e67c87921262268e7e4a170ef736d046cbd6deff862b739cb` |
| `tests/unit/test_general_input_goals_live004.py` | `342a7c40d365ff49148758c2ec5ccb5b01a37ec6e9e688170d2ab3a964d6f00a` |
| `tests/unit/test_code_context.py` | `f9240b621e2621a6169268efcd00c4c881d960719b5c26ae9757aeab4c38e3a3` |
| `tests/unit/test_context.py` | `fa72527126449023b5cbc45a44988d985f1dc998d29a911e61991c2a14748aa9` |
| `tests/fixtures/code_contracts/live010-rejected-plan.json` | `1edf9e8e9b8729fec4b7d5dc907d5908fc194729b73391c9f1ffbd897f4eab09` |
| `tests/fixtures/code_contracts/live011-omitted-defaults.json` | `e3f4abdbfa8100ee28559f42a9c88d928aaf6b83070f098dbc6147c97d99a0c0` |

该记录仅确认本子任务的局部修复、失败处理和测试证据。步骤 3 退出条件及用户验收尚不能由这些离线通过记录得出。

## v43：结果引用契约与纠正反馈

live012 的后两份修订将 `artifact_id/sha256` 当成完整结果引用。`EvidenceRef` 原运行时校验要求未来 Step 引用与实际 Run/Result 引用二选一，但其生成 schema 未表达这个条件；已有 Plan 的原生修订上下文还把该类型简化成普通 object。v43 从同一个模型类型导出互斥形状，并在修订上下文恢复该定义。错误反馈说明文件引用和结果绑定的区别，允许保留原完成 Step 及 Goal 绑定，不替模型补源 ID 或改写原结果。

`test_evidence_reference_schema`、`test_plan_goal_binding_contract`、`test_proposals` 合计 **121 passed，8.92 秒**。XML 为 `data/code-tools-step3/evidence-reference-schema-v43.xml`，SHA-256 `be999984a15898b25198ae7a5705801da2da22111ab755f909c1c4bcf5d2e3b9`。覆盖实际 live012 文件引用反例、未来/具体引用组合及历史空摘要兼容。独立复核另运行重叠的 96 项测试，并在内存中加载修改前类型，确认实际 live012 Run 及历史引用的新旧序列化一致；两组数量不相加。测试使用仓库的 schema 匹配辅助器，不宣称运行了第三方 JSON Schema 验证器。

当前账本的只读复核确认前 12 次累计 85 个模型 HTTP 请求、703114 token（输入 651010、输出 52104），未知 token 占用为零；按冻结未缓存峰值费率估算 USD 0.2578278，不等于服务账单。前 12 次 ORCA、Python Attempt 和内部调用均为零，独立原生 HF 探针另计。12/50 预约已占用，失败额度未回收。原始核对清单位于本地 `data/code-tools-step3/budget-audit-live001-012-001.json`（93356 字节，SHA-256 `1601652999edf775f0691df5d5ae1bc2176c032bd572335f5c3cb731220ce52a`）。

### v43 上下文、容量与最终冻结补充

已存在 Plan 的原生 `revise_plan` schema 现在直接包含 `EvidenceRef.model_json_schema()` 的定义，保持单一类型来源。定向测试在已完成输入准备、ready 集为空的上下文中，用保留的 live012 文件型引用作拒绝反例，并验证合法未来 Step 引用及 Run/Result 引用可匹配，原 Plan.goal_map 不变。通用代码说明明确 `CodeRef.language` 必须符合实际文件内容；XYZ 是 manifest 依赖，不能作为 ORCA 程序输入。缺少程序文件时仍需先规划 writer、得到真实 Artifact/hash，再规划消费者；说明不生成代码、不填计划、不增加许可。

第一轮 89 项中有 3 项真实容量失败：一个 12k 代码观察反馈回环，以及两个 12k 历史采样反馈场景。后两项在独立测试进程恢复旧提示并移除新增 oneOf 后仍超限，确认是此前执行状态事实增加后未被局部回归覆盖的容量缺口。修复只精简同义指导，并在无 delivery snapshot 的最后紧缩投影中省去与完整 `PROPOSAL_SCHEMA` 重复的响应 envelope。schema 的字段、basis 常量、action 分支、权限与逐 Step 执行事实全部保留；没有增加预算，也没有省略 `record_id`。历史采样场景最紧投影降至 11866 输入 token 上界。

随后 90 项通过。冻结前检查又发现紧缩提示的分号使原“授权代码”字符串替换失配：即使存在 TASK_CODE_RULES，系统提示仍写成禁止 code。修正标点后，在 12k 反馈用例中增加明确授权提示断言，并重新运行 4 个受影响用例。90 项对应标点修复前候选，4 项对应最终候选；两组不能相加，也不能把 90 项称为最终字节候选的完整复验。

| 本地 XML | 通过 / 失败 | XML 秒数 | 候选与范围 |
| --- | ---: | ---: | --- |
| `data/code-tools-step3/validation/code-context-v43.xml` | 86 / 3 | 28.940 | 三个上下文模块，修复前容量失败。 |
| `data/code-tools-step3/validation/code-context-v43-final.xml` | 90 / 0 | 33.968 | `test_code_context`、`test_context_ready_execution`、`test_context`；标点修复前。 |
| `data/code-tools-step3/validation/code-context-v43-authorization.xml` | 4 / 0 | 5.724 | 最终候选：12k 观察回环、两个无原生授权分支、完成输入后引用契约。 |

90 项 XML SHA-256 为 `f3bdec1fa0a16c0b310f3ae144477539d9ac67ac0683247b2483d77f5ed0ddd1`；最终 4 项 XML 为 `c40e5a380bb5e324aa18b975369e42fac0e11c8cea439ce0a2aa54ca57224766`。相关六文件 Ruff 和 `git diff --check` 通过。测试与上述 121 项/96 项存在交叉，不合计为独立覆盖数。

还只读重建了 live012 的真实准备完成后上下文：输入上界 41969 / 48000，完整引用定义等于运行时类型的原生投影，原 file-only 引用拒绝、合法 Run/Result 引用匹配、原 Goal 绑定保留、ready=[]。前后 78 个 Run JSON 文件 SHA-256 不变。证据为 `data/code-tools-step3/validation/live012-v43-context-replay.json`，SHA-256 `070de0c142877f146fb9d219878e8f57c29d0288fd5017fc57bf849b80ac984c`。这是离线上下文重建，没有调用模型或 ORCA，不证明后续模型已经遵循契约。重建最初一次仅因测试辅助模块导入路径失败，修正验证脚本的 sys.path 后通过；产品代码及原 Run 未因此修改。

开始 live013 前，本子任务最终冻结六文件如下；之后未修改源码、测试、脚本或依赖。这里的 hash 不替代总任务的全源冻结清单。

| 文件 | SHA-256 |
| --- | --- |
| `src/orca_agent/context.py` | `edbf24ffd59409b48355c2b157124bb58533892e41f8647478867c419f8a86dc` |
| `src/orca_agent/readable_context.py` | `6be44eeae120861fbe3b87d80853bf1a0493d60a40064938a389614d537eef09` |
| `src/orca_agent/decision_purpose.py` | `0babe7ca39b8f9805820e78c3981e484bea736488b7cb5f2da6ed6075e16df04` |
| `tests/unit/test_code_context.py` | `37d26002169f99d314418bc4a5f27dde03f6f1446243974f147b51cf077b3c65` |
| `tests/unit/test_context.py` | `d503e2bbc70046a97ecc3e52d723ffbb70bd6ee534cb73e20f213879c7c2f1f2` |
| `tests/unit/test_context_ready_execution.py` | `a876718c1c69943961070daafd80283b06e05588d173544fd5de1509db85c582` |

## v44：修订计划的完整 Step 提案形状

live013 的首份修订复制了运行时 `id/logical_id/immutable_frozen`，第二份又将 Goal 的运行时 `step_id` 用作提案字段。已存在 Plan 的原生 schema 虽在 v43 恢复了 EvidenceRef，却仍使用将 `steps.items` 简化成 object 的结构投影。v44 的原生初始计划和修订计划均从 `ProposedPlan.model_json_schema()` 生成，完整保留 ProposedStep 的字段、必需 `key` 和额外字段禁用规则；复用已完成 Step 的说明明确为 `{key:existing Step.id}`，Goal 对应 `step_key`。原先“确无可用 Result 时不提供现有证据目标”的分支继续保留，未借此修改工具目录或生产准入。

同时，通用纠正反馈明确指出 Step 提案字段和运行时显示字段的区别，Goal 的 `step_id` 错误给出 `step_key` 指引。只有被拒绝候选确实包含 evidence 对象时，才补充 EvidenceRef 指导，避免 union 的缺失 evidence 分支把无关错误引向另一种绑定。没有替模型插入计划、代码或真实来源 ID。

最后三份原始 parsed proposal 与对应 response 文件 hash 保存在 `tests/fixtures/code_contracts/live013-runtime-plan-fields.json`，文件 SHA-256 `4b1ec56bcedcdbd41ce1afa663308ca9a73a2da476b4ae02f47db35db6285347`。其中第三份同时混用 future producer 和具体 Run/Result/Artifact 引用；它不是完整合法的已有结果绑定。测试在真实准备完成形态的上下文中验证三份原提案仍被拒绝，并验证测试构造的 `{key:existing id}` 保留提案符合展示 schema；测试构造物没有被应用或宣称来自真实模型。

完整 Step 定义新增后，一个原有 12k 纯文件反馈回环的最紧投影为 12314，出现真实容量失败。仅在最后紧缩投影、`scientific_execution=false` 且全部 Goal 为 `artifacts/observations` 时，移除该场景不适用的 SDK 来源键和原生收据说明，保留任务相对文件来源、完整 observations 格式、未取得科学资格的说明及脚本边界。重试计数仍完整显示于 AUTHORITY；其重复指导文字被省去。未删除任何 schema、权限、事实或计数，未增加旧 Run 输入预算。另将角色映射写清为 `Step.inputs[role]={producer_key,port}`。

| 本地 XML | 通过 / 失败 | XML 秒数 | 范围 |
| --- | ---: | ---: | --- |
| `data/code-tools-step3/validation/native-plan-v44-first.xml` | 1 / 1 | 6.038 | 新实际提案反例通过；旧 12k 回环超限。 |
| `data/code-tools-step3/validation/native-plan-v44-capacity.xml` | 1 / 1 | 5.561 | 初次精简后仍略超限，保留该失败。 |
| `data/code-tools-step3/validation/native-plan-v44-final.xml` | 91 / 0 | 32.885 | `test_code_context`、`test_context_ready_execution`、`test_context`，最终候选。 |
| `data/code-tools-step3/plan-proposal-feedback-v44-final.xml` | 126 / 0 | 7.148 | `test_evidence_reference_schema`、`test_plan_goal_binding_contract`、`test_proposals`。 |

最终上下文 XML SHA-256 为 `41d56f2d820a099451b2d9c54e5b176d32b7e642f7500bf95d65bcff02fa1a03`；纠正反馈 XML 为 `da36c74e99104bb0c80b7503720ecc671fe915df985cefefbdd45a1b55d0053ff`。终端分别报告 32.96 秒及 7.74 秒；表中统一使用 XML 时间。相关文件 Ruff 和 diff 检查通过。以上是定向结果，不能与此前集合加总为独立覆盖数，也不是最终全量回归。

只读重建 live013 准备完成后的实际上下文得到 41382 / 48000 输入上界，原生修订 schema 等于完整 `ProposedPlan` 原生投影，三份原错误提案均不匹配，原 Goal 绑定保留、ready=[]。前后 71 个 Run JSON 文件 hash 不变。记录为 `data/code-tools-step3/validation/live013-v44-context-replay.json`，SHA-256 `9f6e49e3f0cbe2f2d833cfe55da74f8716678079ddce7ea29c73720f56c45a59`。没有新增真实模型或 ORCA 调用；后续是否正确分阶段写代码、计算及交付仍需真实运行验证。

开始 live014 前，本子任务最终冻结文件如下。其后只追加本文档，未编辑源码、测试、脚本或依赖。

| 文件 | SHA-256 |
| --- | --- |
| `src/orca_agent/context.py` | `f927417a6ad83abdad60efdecb1a9065e77a5426c8cb5effe48d182f51cc2b0e` |
| `src/orca_agent/readable_context.py` | `4422f77a62bbd5d9cdb8c8904ba1f2191f444954dd7499d9a9920923b28ac3ac` |
| `src/orca_agent/decision_purpose.py` | `59429ab4446b85357b6d73be4e7762d313f11a0c77c944c578eee98aa651da7f` |
| `tests/unit/test_code_context.py` | `90f6b87640a4c8e88c441f8c62b8883d766a2e7189284ab8e218afd3b59849c6` |
| `tests/unit/test_context.py` | `d5ab4b88c7fd4f9d9419a9a1f303b85fef1ec846b8ce447ad8398243d9e04c86` |
| `tests/unit/test_context_ready_execution.py` | `0ee378e7a451815c969b83069119ec8deb220e72be8cc0ed1355b46bdbf5f052` |


## v45：保留显式读取窗口并利用已有输入容量

live014 的 10 次 evidence 调用全部完成，其中 9 次 text、1 次 list；第 10 次完成后没有下一次模型请求。原 Python 全文读取已返回 67 行、2802 字节，但其后上下文仅展示 1–22 行；另一次请求 23–67 行的读取只展示 23–39 行。每次决策只保留最新读取，旧窗口被新的读取替换，因此模型在已保存的 Python、ORCA 输入和 XYZ 间重复消耗读取额度。list 原结果含 11 件 Artifact metadata，旧投影却将整块替换为 hash/size。以上均为上下文选择和展示问题，不是文件读取或 Artifact hash 失败。

v45 在已授予 code_execution 且许可工具定义包含 execute_python 的 Run 中，按 Tool effects 保留已完成的本 Run 纯只读 Call 所对应的 Result。选择器不读取原始 Artifact，不增加查询或预算，不使用具体科学工具名。既有 32 Results 上限和总输入额度仍生效。失败读取可以作为当前反馈出现，但不参与正文去重。不同 Artifact、SHA、view/path 或实际返回窗口保持独立。相同窗口只有在实际 Call/Result 对应、原 Artifact ID 和有效 SHA 与通过的读取检查来源一致、原始返回行完全一致时，才通过 lines_ref 复用同一可见集合中首次展示的正文；各次 Result 身份、requested/returned coverage 和费用事实保留。引用指向可见预览，不能代表预览以外内容。

正文投影先尝试 16 KiB，并在缩短正文前尝试既有 schema、键和重复字符串的无损紧缩；随后仍保留 1 KiB/256 字节回退。context_page 明确区分展示预览和 Tool 实际覆盖；list 保留 inventory_sha256、实际 metadata 及分页信息。没有自动读取脚本，没有修改生成代码，没有将读取成功升级为科学通过。

原生决策界面只广告 action=Tool.name、parameters=工具参数这一种立即形式。call_tool 只用于已计划且 ready 的 step_id；已知 pending=[] 时不再广告该动作，直接的合法立即工具仍可用。底层既有 nested call_tool 解析保持兼容。提示版本加入 v45，历史版本仍受原决策契约保护。

真实前 9 次读取以及第 23 次请求的来源、原 Request/Plan 和原文保存在 `tests/fixtures/code_contracts/live014-explicit-read-windows.json`，SHA-256 `6a38daf247522d0466a2a4bd38a378dbb7999767e25f431feb2bad28a529a025`。第 10 次未进入后续模型请求，明确排除在回放之外。独立只读审计核对各文本原始文件与注册 hash，生成 fixture 前后 123 个原 Run JSON hash 不变。

| 本地 XML | 通过 / 失败 | XML 秒数 | 范围与失败性质 |
| --- | ---: | ---: | --- |
| `validation/retained-read-v45-first.xml` | 17 / 4 | 32.372 | 新 fixture 的 Run 48k 与冻结 model 24k 不一致；新两页 12k 场景确实超限；两处旧 matcher 不支持空集合 schema。 |
| `validation/retained-read-v45-final.xml` | 115 / 13 | 41.877 | 中间候选，10 项因测试在 writer 前修改冻结配置触发 stale Run；另 1 个立即工具 fixture 预期不符及 2 个旧 v40 enum 预期过时。不是最终冻结结果。 |
| `validation/retained-read-v45-second.xml` | 20 / 1 | 29.606 | 新窗口正反例通过；剩余旧 fixture 未展示 evidence.text，改用其实际合法的 knowledge.search 正例，同时验证无 ready Step 动作。 |
| `validation/retained-read-v45-final002.xml` | 128 / 0 | 39.300 | 最终冻结候选的 test_retained_read_windows、test_context_ready_execution、test_code_context、test_context、test_call_tool_contract。 |

上表路径均相对 `data/code-tools-step3/`。最终终端时间 40.24 秒，XML SHA-256 `0aa1fb5b3709626ea3415a26dbbdc84ffeee40103b9b35f9cb60272b742fe76a`。Ruff 八文件及 diff 检查通过。各轮测试集合重叠，不能相加为独立覆盖，也不是最终全量验证。新增的两页组合在 12k 下仍超出容量，测试明确验证 ContextLimitError；既有可承载的 12k 场景保持通过，未提高其限额或静默丢弃旧页。

实际 live014 的最终只读重建恢复了第 23 次原请求的累计费用显示，沿用原 Request/Plan/permission/config 和已读取结果；仅在内存排除其后第 10 次读取。为允许构造历史请求，使用明确记录的历史投影时间。新原生投影输入上界 46078 / 48000，同时显示 Python 67 行、单独尾页 45 行、ORCA 输入 19 行和 XYZ 12 行；每个展示行和复用引用均与原 Result 严格逐行相等。pending=[] 不广告 call_tool，evidence.text 直接动作存在。原 123 个 JSON hash 不变，没有模型、ORCA 或任务 Python 调用。记录 `data/code-tools-step3/validation/live014-v45-context-replay.json`，SHA-256 `1af1bc7971f3defaf350dbf268cada677ec06289fd91a9c54a28d5709d722a44`。较早回放在未尝试 16 KiB 紧缩时确实退到 1 KiB，保留其失败记录，不作为最终成功证据。

总任务同轮另将原生 ORCA 公共契约说明写清：wavefunction 关键字必须显式出现在 ! 行；xyzfile 是单行，不追加独立 closing *。准入实现没有放宽，真实 live014 也没有执行这些输入，故不得把只读发现描述为实际 ORCA 失败。相关 native_analytic/native_orca/native_prepared_xyz 三模块 144 项通过，终端 6.83 秒；`data/code-tools-step3/native-contract-guidance-v45.xml` SHA-256 `6f80190ee0f7fc9f558c769634cf538cbe0aa1b214227a63396f5385e490419d`，由总任务记录。

v45 源冻结如下；冻结后本子任务只做上述只读回放和本文档追加。后续真实模型能否正确计划、计算及交付仍须独立实测。

| 文件 | SHA-256 |
| --- | --- |
| `src/orca_agent/context.py` | `9f1077bf775290e1d3d8e7e52d265663e2f5f648f7a932b2dea7c5352a2981e4` |
| `src/orca_agent/readable_context.py` | `c51ad2585035ab7485ef6287bfb4e7f5eb163b723ecf5d67edb788e12bec08b9` |
| `src/orca_agent/decision_purpose.py` | `86a0faf58c402520d9c4cc9cdcf6af36c1342b2ad346c84b578c43213a257b10` |
| `tests/unit/test_context.py` | `6155929d461e115ccc2492a9c14a90e24a8b316f56d454942a9747027f48e9c9` |
| `tests/unit/test_code_context.py` | `e597f0da0290430ec674aa7fef3d2da109badcdf7983d437037cb042f4e1ca2a` |
| `tests/unit/test_context_ready_execution.py` | `f07c2893bfb99c29beb223d31d2b0a3bd424c98d2bf79a016b838421dacbb107` |
| `tests/unit/test_call_tool_contract.py` | `f8a6124fe2eac5691cc93406c0f280eaaf235ab776a5f17088ab215a816f8d06` |
| `tests/unit/test_retained_read_windows.py` | `879d96c65b762720b4a701e92916199b1a5fdf707745677849a6ad2c66f81491` |


## v46：立即工具参数的原生 schema 与数值纠正反馈

live015 结束于 failed/partial，13 次真实模型调用、156685 tokens、0 个 Attempt/ORCA 启动。最后三份 evidence.text 提案依次包含 `{b:ArtifactID,start_line:0,lines:250}`、修正字段名后的 `{artifact_id:...,start_line:0,lines:250}`、以及 `{artifact_id:...,start_line:0,lines:200}`，均在准入前被拒绝。不能把这些描述为实际文件读取或 ORCA 执行失败。

逐份检查实际发出的请求确认：三份 schema 解码后均保留 start_line 的 minimum=1/maximum=10000 和 lines 的 minimum=1/maximum=200，数值约束没有丢失。但紧缩界面将 artifact_id 参数名编码为 b/c，将数值 schema 放入 @N 字符串池。模型首份提案恰好复制了展示别名 b。default=1/40 在当时的注释投影中被省略；另外 native_schema 原先省略字符串 maxLength，因此同 Run 较早 knowledge.search 的过长查询未在原生界面直接看到长度上限。真实 registry 上限是 120，不是早期沟通中的 128。v41 的候选回传没有回错：第二、第三请求均完整包含紧邻上次候选、正确的 logical request、basis 与 response hash。

v46 对可立即调用工具的 ACTION_SCHEMAS 直接采用唯一 registry 的完整参数 schema，并只对这些小型子树保护真实字段名、默认值、数字边界、字符串长度和 pattern，不进行 schema 列编码、字符串池或字段别名编码。大 Plan 和 DATA 仍使用已有压缩。没有新增别名容错、自动修正参数或读取动作；底层原有校验仍拒绝三份实际错误提案。提示版本加入 v46，历史版本集合保留。

同轮总任务新增通用 validation_error_details。agent 和计划参数反馈共用它，只保留白名单中的有限数值 ge/gt/le/lt/multiple_of/min_length/max_length，不回传错误 input、msg、url、任意 ctx 或凭据。这样 start_line 的 greater_than_equal 同时带 ge=1，查询 string_too_long 带 max_length=120。程序提供规则事实，由模型重新提交候选，不自动填写或执行修正提案。

三份原始拒绝候选统一复用 `tests/fixtures/code_contracts/live015-invalid-read-bounds.json`，SHA-256 `7156a2869c2adb3345d65028036b1ec7f469ac86ebb78543a1a32596dc552a58`；本子任务试建的重复 fixture 已删除，没有保留第二份同义证据。

| 本地 XML | 通过 / 失败 | XML 秒数 | 范围与失败性质 |
| --- | ---: | ---: | --- |
| `validation/native-immediate-v46-first.xml` | 123 / 2 | 28.789 | 新测试假设一定生成 KEYS，但 DATA 被已有表格编码处理；旧比较 helper 的 pending 示例未遵守 v40 真实 ready enum。属于断言设置，容量项通过。 |
| `validation/native-immediate-v46-final.xml` | 130 / 1 | 107.253 | 131 项生产源候选；唯一失败为新测试误写 query maxLength=128，registry 实际为 120。包含旧 12k 与 repair cycle 容量验证。 |
| `validation/native-immediate-v46-final002.xml` | 2 / 0 | 0.708 | 只修测试常量为 120 后，复跑全部受影响新模块；生产源未再改。 |
| `numeric-feedback-v46-001.xml` | 116 / 2 | 7.650 | 总任务初轮：测试把 artifact_id 字段名误认成值泄漏；直接 _decision 的新 Run fixture 未先更新 Goal 状态而触发 stale。 |
| `numeric-feedback-v46-fixture002.xml`、`fixture003.xml` | 0 / 1、0 / 1 | 2.369、2.159 | 同一离线新 Run fixture 调整的中间失败。 |
| `numeric-feedback-v46-fixture004.xml` | 1 / 0 | 1.807 | 修正 fixture 后通过。 |
| `numeric-feedback-v46-final.xml` | 118 / 0 | 7.476 | 总任务通用安全反馈的最终四模块定向。 |

路径均相对 `data/code-tools-step3/`；表中 fixture003.xml 与 fixture004.xml 的完整前缀同为 numeric-feedback-v46-。131 项终端报告 107.32 秒；最后 2 项终端 0.74 秒；总任务 118 项终端 7.57 秒。不能将 130P 与最后 2P 写成单次 131 项全绿，也不能把重叠集合相加；所有受本轮修改影响的失败已定向复验。相关八文件 Ruff 通过；本轮不是最终全量回归。

上下文 131 项 XML SHA-256 `3724ec7c55fb41d07eefde7c9a355b5f73c9ad945b503587e4e1ebe9e8ad37f7`，最后 2 项 XML `336c90b4e39c26b433d140df9b9e64822c84e060b2949d7b5fb5e57edbf8a6ca`；总任务 118 项 XML `37adb8ad9f2226fad6a7e21062029b09d0d5df9ae20be48d080b92e47f278526`。

真实上下文的只读重建验证：

- live015 首个非法读取前的原 Request/Plan/permission/config/已完成 Results 与原费用显示重建后，输入上界 38158 / 48000；原始 wire 中 evidence.text 与 knowledge.search 的 schema 均逐对象等于 registry，无需任何别名或池解码。80 个原 Run JSON hash 不变。记录 `validation/live015-v46-context-replay.json`，SHA-256 `56443566bac5ebc7183204f756fcbe4cada6ceef2dc8c1a0095e35cf1d876ba5`。
- live014 第 23 请求同样复跑，输入上界从 v45 的 46078 增至 47056 / 48000；Python 67 行、另一次尾页 45 行、ORCA 输入 19 行、XYZ 12 行仍同屏完整，逐行和引用复用均与原 Result 一致，没有 context_page 截断。123 个原 Run JSON hash 不变。记录 `validation/live014-v46-context-replay.json`，SHA-256 `e55d05bf6646ca0da6ad5177522ecd8e1db853f7005f362e0bae30638ec92fa6`。

以上回放没有调用真实模型、任务 Python 或 ORCA，也没有重写旧 Run。更大的未来上下文仍可能需要明确预览或达到容量门禁，不据这两个案例保证所有场景都完整。真实模型能否正确推进计划和执行仍待后续独立运行。

v46 冻结文件如下；之后本子任务只追加文档。

| 文件 | SHA-256 |
| --- | --- |
| `src/orca_agent/context.py` | `aff3b075959f3a5fb56b3d8d3e5b0c5e1330a295beb4847b450a037a5d059b8e` |
| `src/orca_agent/readable_context.py` | `e4fd1baa99720697b457a267aa40ea9631537d3f24d00d43ceda35d9c50c2b2b` |
| `src/orca_agent/decision_purpose.py` | `9a4cbb4891c41a460a07c1490e1ead7195d2651b8c1d0b5f265fd8af359e3132` |
| `tests/unit/test_context.py` | `fd7cc936f2684b178a42be383bf9a61d62dd9cfaa5a9a844e3923056b1e225ba` |
| `tests/unit/test_code_context.py` | `275945b38bf4658f57e703e8de4ffcb7ed23149e38c3ddd2dcbebab8de2ae430` |
| `tests/unit/test_decision_purpose_capacity.py` | `410979a4f956e27b56be47ac742840063f48115197eecdaa3347e201dddfef5f` |
| `tests/unit/test_repair_cycle_e2e_capacity.py` | `5875727c0899df04bb9ff930c6cf6651df1d7a00d06cfa59cd0b11d306836744` |
| `tests/unit/test_native_immediate_schema.py` | `3f40f14da8e1f24cb41a22dbf49e0ebb8c7da786479d425f679390dcc2b92e57` |

总任务反馈源的冻结 SHA-256 为 agent.py `6663abb1684e74ec61f2fda5dc88d582c8ea7c2e69e88166262f09d1d8fd734b`、proposals.py `a1c586c6d17a9c673763d65eb14b8118a1dc708594c50b76e621eaa4f7fb9857`；完整发布仍以总任务全源清单为准。

## v47：代码运行的完整字面提交契约与容量边界

live016 的实际模型请求证明，v46 只保护立即工具的小型 schema 仍不够：第 13、14 份修订提案复制了其他显示别名，包含根字段 y、Step.u 和 Python 参数 t；第 18、19 请求因容量进入旧完整 Proposal 包络，其 Plan Step/EvidenceRef 显示又退为普通 object，第 19 回复使用 j/V/T/C/g/x 等别名。第 17 提案的基本形状合法，但漏掉已满足的结构 Goal，不能把这些问题都归为 JSON/schema 语法失败。原第 15 提案曾进入一次真实 Python Attempt；本节只验证上下文修复，不把该 Attempt 或后续候选当作 ORCA 成功。

v47 对 `Run.permission.code_execution=true` 的运行保留一致原生提交形状：ACTION_SCHEMAS、RESPONSE_ENVELOPE、CONTROL、工具名称/参数 schema、Goal 的 id/port、计划 Goal 绑定和上次拒绝候选均使用真实可复制字段及值，不经过自定义短键或共享值池。代码运行的 schema 仅删除 title/description 注释，并使用已有等价简化；pattern、字符串长度、数值边界、默认值、互斥结构均保留。实际名为 title/description 的参数与 schema-looking default 数据也不会被误删。立即工具仍直接来自唯一 registry 定义。非指令 DATA 可继续按既有可逆方式紧缩，未新增参数容错、计划补全、权限或自动文件读取。

保护同时覆盖嵌套列表和祖先键：TOOLS 不会先变成 @rows 而绕过参数保护，不可信 DATA 中重复出现 request/goals/plan 也不能把 AUTHORITY.request.goals 的路径改成短键。只有精确保护路径及其祖先键受限，其他 DATA 仍可完整解码还原。最后容量层可将非立即 schema 转成已有、可逆的 SCHEMA_COLUMNS 表示，字段名、const/enum/default 和边界仍是字面值；独立展开对照证明其与完整原生 schema 相等。未保留试验中的通用重复子树 $defs 优化器或 16 KiB 事实池扩展。

生产代码运行在带有 delivery snapshot 的首次/纠正、语义登记、规划和最终交付中均保持原生包络，压缩后的 terminal 仍为 action/parameters/reason。装不下时明确 ContextLimitError，不再为适应容量退回泛 object 的另一提交协议。无 snapshot 的历史纯投影入口及非 code_execution 运行保留原展示兼容；这些非代码历史 schema 仍有 v46 既有的 pattern/字符串长度/空默认省略，不能称为本轮完整字面契约。运行时参数校验始终来自原有定义。TASK_CODE_RULES 的适用性继续由工具 effect 判断，与完整提交契约的 code_execution flag 范围不同。

原第 13、14、17、18、19 份 parsed proposal 与原请求/回复 hash 保存于 `tests/fixtures/code_contracts/live016-contract-alias-replies.json`，SHA-256 `b396b256abd1209669deea66d0a8e7b373ad82376d9235a93a8123efaa40301b`。测试保留错误提案拒绝与第 17 份合法基本形状的区别，没有改写旧提案或替模型补 Goal。

### 定向验证及中间失败

以下 XML 均位于 `data/code-tools-step3/validation/`，名称共同前缀为 `literal-contract-v47-`。集合大量重叠，不能相加，也不是最终全项目回归。

| XML 后缀 | 通过 / 失败 | XML 秒数 | 性质 |
| --- | ---: | ---: | --- |
| `first.xml` | 36 / 4 | 12.756 | 三个旧 12k 场景容量不足；一处旧纯文件 SDK 指导断言过时。 |
| `focused.xml` | 4 / 1 | 1.065 | 新反例发现 schema 注释删除误伤实际 description 参数名，随后修复。 |
| `defs.xml` | 29 / 4 | 8.716 | 中间容量优化候选，未达到全部容量目标；该泛化优化最终移除。 |
| `readonly.xml` | 99 / 1 | 20.520 | 中间候选的旧输入链容量失败。 |
| `capacity.xml`、`input-chain.xml` | 0 / 4、0 / 1 | 5.187、1.505 | 容量失败持续，未以提高旧 Run 预算掩盖。 |
| `protocol.xml` | 78 / 0 | 7.831 | 字面协议、证据引用及读取保留定向通过，非最终全组。 |
| `columns.xml`、`columns-pool.xml` | 1 / 3、0 / 3 | 5.593、4.601 | schema 列表示及事实池试验仍有容量失败；事实池扩展最终移除。 |
| `final-first.xml` | 73 / 6 | 20.887 | 新参数化 fixture 试图赋值冻结 ModelLimits；改为创建新 Run 时传入冻结配置。 |
| `capacity-final.xml` | 27 / 5 | 12.055 | 四处 fixture 修改已存 Run 引发 stale；一处把有效原生来源投影误限定为紧缩表示。 |
| `final002.xml` | 142 / 1 | 33.664 | 新 64k 来源断言未展开 text_basis_ref；生产来源事实存在，修正断言。 |
| `final003.xml` | 146 / 3 | 72.312 | 三个旧非代码水分子 12k 场景出现真实容量失败；恢复历史非代码展示范围。 |
| `legacy-scope.xml` | 9 / 0 | 9.168 | 恢复非代码历史范围后定向通过。 |
| `final004.xml` | 149 / 0 | 124.743 | 最终冻结候选六模块全部通过。 |

最终模块为 test_native_immediate_schema、test_code_context、test_context、test_decision_purpose_capacity、test_retained_read_windows、test_repair_cycle_e2e_capacity。终端报告 125.87 秒，XML SHA-256 `4ec9a37d1e5a387b354e56c7b23b24be3f6eb42046f51472eb218c10fa675caa`。本轮修改的七个源码/测试文件 Ruff 通过；独立只读审查复验了祖先键保护、schema 列表示等价、terminal 压缩和无新能力投影。

容量结论有明确范围变化：两个旧纯文件、代码授权的失败/观察反馈场景在完整契约下不再适配 12k，测试保留原预算并断言 ContextLimitError、无模型 HTTP 请求、Run/Request/Plan/Result 与费用不变；另建冻结为 64k 的新 Run 作为正控制，原有来源、schema、权限和观察断言全部保留。旧非代码输入链的 12k 正例及六个非代码 repair-cycle 容量场景保持通过；没有逐个提高旧测试预算。新 64k 是前瞻配置要求，不代表旧 48k/12k Run 获得新限额。

### 最终源码上的 live016 只读重建

复用原第 18、19 请求的 Request/Plan/permission、历史时间、累计费用、11 个 Result、10 个 Call 和准确的前次候选；先核对实际 Artifact hash 与 Python 94 行、stderr 14 行原始读取内容，再仅在内存分别设置 48000/64000 输入限额。没有执行模型、任务 Python 或 ORCA。以下上界不是外部模型实际计费 token。

| 原请求 | 内存限额 | 输入上界 | 原生合同字面 | Python / stderr 展示行 | 前次候选 |
| --- | ---: | ---: | --- | --- | --- |
| 18 | 48000 | 47939 | 是 | 26 / 11 | 完整，等于原始候选。 |
| 18 | 64000 | 53211 | 是 | 94 / 14 | 完整，等于原始候选。 |
| 19 | 48000 | 43223 | 是 | 12 / 6 | 整项明确省略，保留 bytes/hash/原因。 |
| 19 | 64000 | 54601 | 是 | 94 / 14 | 完整脚本保留；唯一 Goal gap 被明确脱敏。 |

64k 的 Python 正文与原始读取逐行相等；stderr 全部 14 行与既有 `_safe` 投影逐行相等，原始第 2/7/10/14 行中的路径被脱敏，不能称为原始 byte-exact。第 19 候选的中文 gap 中“参数/路径问题”触发现有保守路径匹配，该字段被替换为明确的 omission/hash 元数据；projection 标明 redacted=true、complete=false，其他候选内容和完整脚本均保留。这个中文误匹配限制没有在本轮改动。48k 展示缩短均附 context_page，读取 coverage/hash 仍来自原 Result，不把预览当全部内容。

最终独立文件为 `validation/live016-v47-context-replay-final003.json`，SHA-256 `486a82d1752958ca80eba5cdde4beaebbfe7052e4fdb10624d040c706be04382`。回放前后 141 个原 Run 文件、60 个生产 Python 文件及所有较早回放报告 hash 均不变。最初报告暴露 TOOLS 列表先压缩而漏过保护的问题，保留 `live016-v47-context-replay.json`（SHA-256 `034b27eef802b5c6c4fc7f701f90dfb4293cc40f67208561dcf4a80778b458a4`）；较早候选的 final001 也保留，没有覆盖为最终结果。

### v47 子任务冻结文件

在 live017 启动前源码、测试和 fixture 已冻结；随后只做上述只读回放与本文档追加。完整发布仍以总任务冻结清单和后续真实结果为准。

| 文件 | SHA-256 |
| --- | --- |
| `src/orca_agent/context.py` | `175057c6f59d6423fca4d70f50efdda5103a291dfb0c371ec77c7ddd6d905743` |
| `src/orca_agent/readable_context.py` | `78334afde290323513369912fb921fa4747411416f7ae533541de0f58ca8f042` |
| `src/orca_agent/decision_purpose.py` | `aff03db89023070bc372e1331c93ea3ca36aa298f676fb6cc08f158b4503ae74` |
| `tests/unit/test_context.py` | `0a904b8ae9e80635484a4716f9d29a8c77e50cc8acfb78fcdafba94a8676cda2` |
| `tests/unit/test_code_context.py` | `92d2478c2945befc608ac0563c591163270f7b619aef36d280a86eb520bbb75a` |
| `tests/unit/test_decision_purpose_capacity.py` | `e1a89ec927eea6d5646844b0f9f55dd97555f6d321189ffc31d6de26b077d77b` |
| `tests/unit/test_native_immediate_schema.py` | `9cfa525285a30d41dc38ee06c08a62901f5717ea257327496cd385fa2ef7b3ee` |

## v48：复用已完成 Step 的依赖事实与纠正反馈

live018 的受控故障用例尚未到达注入门禁即 failed/partial：11 次模型调用、155735 tokens、0 个 Python Attempt/ORCA。第 9 回复执行了已计划的 writer；第 10 回复提出 Python Step，复用已完成 prepare 和 writer，却省略 prepare 依赖的 resolve。只读 materialize_plan 重放准确触发 `Plan.valid_graph: dependency references an unknown step`。未到达的 Python、SDK、原生输入准入或故障注入不能被描述为这次实际失败。

第 11 请求的原生 schema 和候选回传正确，无短键或共享字符串编码；1828 字节的前次候选完整保留。但 AUTHORITY.plan 把 prepare 压成 `{id,immutable_frozen:true}`，抹去其 depends_on；整个 wire 中遗漏的 resolve ID 仅出现于自身 Step.id，纠正反馈也只有 `value_error/loc=[]`。第 11 回复最终正文为 0 字符，finish_reason=length、completion_tokens=16384、HTTP 响应 71007 字节。reasoning 内容及其分项长度按既有策略不保存，不能声称准确的 reasoning 字节数，或断言所有输出 token 都属于 reasoning。本轮没有据此提高输出额度。

v48 仅在 code_execution=true 的 Plan 展示中，为冻结的已完成 Step 保留真实 depends_on，包括空列表；说明复用 Step 也复用其依赖，修订计划必须保留传递依赖闭包。紧缩和末级回退仍保留对象，Plan steps[].id/depends_on 的字段名与 ID 值不被别名、共享池或表格编码隐藏。非代码旧展示不变。没有修改运行时 Plan、自动添加依赖或 Step，也没有扩大许可。

总任务在 materialize_plan 的保留分支中增加具体纠正反馈：若原冻结 Step 的 depends_on 不在当前提案中，返回 `missing_retained_dependency`、准确 `parameters.steps[index].key`、retained_step_id 和 missing_dependency_step_ids，并给出静态规则说明。反馈先于最终 Plan 构造，候选仍被拒绝；程序不自动补闭包。测试中的明确修正提案只用于验证已有 Plan 图规则，不代表激活成功或真实模型自主修复。

共用 fixture `tests/fixtures/code_contracts/live018-missing-retained-dependency.json` 保存 exact 第 10 候选、原 Request/Plan、原第 11 纠正投影以及来源文件 hash，SHA-256 `3f264844b1814afbd76a2733e4faecaf76fe485b1dbf49e40f00e724bdf085fd`。其中 run_context 明确是选定身份/冻结配置/许可/预算字段，未收录的大量历史、Calls、Results、input_bindings 不被断言为空。

| 本地 XML | 通过 / 失败 | XML 秒数 | 范围与失败性质 |
| --- | ---: | ---: | --- |
| `retained-dependency-v48-first.xml` | 40 / 3 | 11.045 | 新、旧 writer 12k 场景真实容量不足，最紧 12082；另一个旧 prompt 版本断言待更新。 |
| `retained-dependency-v48-second.xml` | 41 / 2 | 11.020 | 仅精简新增提示后两个 writer 场景仍超限，最紧 12003。 |
| `retained-dependency-v48-final.xml` | 145 / 0 | 110.114 | 再去除短提示重复词后六模块通过；保留全部依赖事实与合同，旧 writer 12k 通过。 |
| `retained-dependency-feedback-red001.xml` | 1 / 1 | 1.162 | 总任务先用原候选验证旧反馈缺失具体依赖诊断。 |
| `retained-dependency-feedback-green001.xml` | 92 / 1 | 5.737 | 通用反馈已生效，单项测试误用 detail.instruction，真实字段为 requirement。 |
| `retained-dependency-feedback-green002.xml` | 93 / 0 | 5.981 | 修正测试字段后，总任务四模块通过。 |

路径均相对 `data/code-tools-step3/validation/`。145 项为 test_retained_plan_dependencies、test_native_immediate_schema、test_code_context、test_context、test_decision_purpose_capacity、test_repair_cycle_e2e_capacity，终端 110.36 秒，XML SHA-256 `328961371cc61c33c0d27a805239cc6d632ce9e13ddf9dbd53528cb9f554cf0b`。93 项终端 6.08 秒，XML SHA-256 `f1d47ba7a1d102dddeb079914187818108a79458050bf2b14b3d19c4e74bef61`。两组及中间轮次重叠，不相加为独立覆盖，不代替最终全项目回归。相关源码/测试 Ruff 通过。此次没有再新增容量兼容变化：writer 的 12k 正例、非代码旧 12k 回归继续通过，v47 已记录的两个代码 12k 失败关闭边界仍按原测试验证。

在最终源码上，只读重建原 live018 第 11 请求得到输入上界 50389 / 64000，保持原生字面合同，prepare→resolve、writer→prepare 两条冻结依赖均直接可见；前次候选仍等于原候选。为比较实际显示缺口，回放沿用原来的笼统错误反馈，不伪造已经产生新的结构化纠正。78 个旧 Run 文件及 60 个生产 Python 文件在回放前后 hash 均不变，无模型、任务 Python、ORCA 调用。记录 `data/code-tools-step3/validation/live018-v48-context-replay.json`，SHA-256 `dabc5e41c0c39325cfc6a293c3911384cf03ebfc7a9311a3ae91692a4e6b975a`。

live019 启动前，本子任务源码/测试/fixture 已冻结；之后只追加本节。后续真实模型是否保留完整闭包并通过故障修复验收，仍以新 Run 为准。

| 文件 | SHA-256 |
| --- | --- |
| `src/orca_agent/context.py` | `ff71f56226798e3a6257fecc8ccb9ffeaa8f52f5bbefb4a8ebc7c7299eadc471` |
| `src/orca_agent/readable_context.py` | `012f7fca67c35c50dc4a5deedccc27d6352b8bba55fe7f31db44c55e05a4edcc` |
| `src/orca_agent/decision_purpose.py` | `61ceab2aedf269a2ae51beb8d832d3a007ccdc6608fc78859cfd95af196847fa` |
| `tests/unit/test_context.py` | `e1a42615fab9ebe961916acda07b3c3adc76c67696cea98c5b458241a42f85ff` |
| `tests/unit/test_code_context.py` | `61c73c30a51b5d6c7155850cea021538d691a0cbb09899a05bf8ca466aa193db` |
| `tests/unit/test_retained_plan_dependencies.py` | `5c5952c0ae04f9d4af8222834dce3ea1d97574157ebc63c17fbe9210ec399f2e` |

总任务另冻结 proposals.py `d4584721b526b25bb5055213d1f000444179a4549ef241b630f04acc7dacd7c2` 和 test_retained_dependency_feedback.py `47a05640812c27bdd0b194e8a7c6690f677e5dd6f7d672dffd46e339b2d50ba1`；完整源与前瞻配置范围以总任务冻结清单为准。

## v49：SDK 返回文件的输入与输出角色

live019（`run_a7212378d09e4165aeecae161136c7bf`）以 failed/partial 结束，21 次模型调用、340963 tokens、2 次真实 Python 与 2 次内部 ORCA。prepared_geometry 和 vibrational_frequencies Goal satisfied，optimized_geometry Goal insufficient_evidence；观察资格仍为 unverified。两次计算与两次交付失败的性质须分开记录，不能把后一次校验失败重写为前一次的科学来源错误。

第一次模型脚本用 `next(k for k in files if k.lower().endswith('.xyz'))` 取回执中第一个 XYZ。实际文件映射同时包含输入 `ethanol.xyz`、最终输出 `job.xyz` 和轨迹 `job_trj.xyz`，且输入排在前面。脚本引用的是输入 XYZ，故目标反馈为 `optimized_requires_original_native_result_source`；所引 SHA 与 prepared geometry 的 SHA 相同。第 15 请求已显示这一来源不足及真实 hash，模型 reason 也正确识别了输入来源问题。第 15—18 请求的提交合同和任务指导均为字面原生形式，没有字段别名或共享池误解。

已有指导确实说明回执 maps original filenames to readable read-only copy paths、并非 Artifact 元数据、输入统一 job.inp，以及成功 OptFreq 可能有 job.xyz/job.hess 且须检查存在。此前没有明确说明 files 也包括复制的输入和依赖，亦未说明后缀或字典顺序无法判定输出角色。结果事实未自动展开完整 SDK files 清单；可用的受管证据查询仍可获取原始材料，本文没有把自动读取新文件加入上下文。

模型在第 15 回复中曾提出从 ORCA 正文最终坐标块取值的脚本，但没有执行该版本；第 16 回复写入的下一版本又使用第一个 XYZ。第 18 reason 声称使用 files['job.xyz']，实际引用的脚本仍按后缀取首项，并把输入重命名为 geom.xyz。第二次实际回执仍同时包含 geom.xyz 与 job.xyz；然而第二次 Result 的直接拒绝原因是 `observation_manifest_rejected: observation name is duplicate or outside the frozen Step`：Step 仅声明 optimized_geometry，脚本却输出两个观察，故该 Result 没有有效观察记录。不能宣称第二次又通过科学来源校验得到了同一个 gap。

第 20、21 回复的 `finish_reason` 均为 stop，正文分别为 1565 和 629 个 ASCII 字符，completion tokens 分别为 1020 和 1139；并非截断或代码围栏。两份 JSON 都在结尾缺少一层闭括号，且 reason 位于 parameters 内而非所展示包络要求的根层。纯内存补一个括号只能用于定位形状问题，不能作为合法提案或自动修复；原回复未改。本轮没有发现需要改变 JSON 解析或生产科学校验的证据。

v49 只替换通用 SDK 文件角色说明，并注册新 prompt 版本：files 包含 inputs/dependencies；后缀和映射顺序不能识别输出；统一 native 输入为 job.inp，OptFreq 最终几何为 job.xyz、Hessian 为 job.hess，日志为 stdout.out/stderr.txt。继续要求检查实际 keys 和文件存在性；保持只读副本、非 Artifact 元数据、相对依赖、同 Attempt 回执、来源 filename=原 files key/internal_request_key=receipt.request_key、冻结资源和累计预算约束。没有提供任务算法、预写模型脚本、自动选择源文件、修改运行时或放宽科学资格。该段 UTF-8 长度由 1393 增至 1442 字节，净增 49 字节；纯文件或非代码场景不会收到这段科学 SDK 指导。

| 本地 XML | 通过 / 失败 | XML 秒数 | 范围 |
| --- | ---: | ---: | --- |
| `sdk-file-roles-v49-final.xml` | 139 / 0 | 33.201 | test_code_context、test_context、test_native_immediate_schema、test_retained_plan_dependencies、test_decision_purpose_capacity。 |
| `sdk-file-roles-v49-legacy-capacity.xml` | 6 / 0 | 86.768 | test_repair_cycle_e2e_capacity 的水/甲烷 × 临时/开发/正式路径六个旧非代码 12k 场景。 |

两份 XML 位于 `data/code-tools-step3/validation/`；终端时间分别为 33.52 秒和 87.07 秒，SHA-256 分别为 `6dea6f9e91073457f62e27a64c5810860ec0570aa745668f1f64f73ef1a6380a`、`e6c0da7c5b62797fa2b0a13b2b6df00076c662e173e0a419081ab9eb660e1b52`。这是分两次执行的定向验证，并非一次全量测试；与历史轮次重叠，不累计为独立覆盖。139 项已包括 writer 12k 正控制、冻结依赖的强制紧缩投影，以及 v47 两个 code 12k 失败关闭边界与对应 64k 正控制。本轮没有中间测试失败；四个改动源码/测试文件 Ruff 通过，补测前后文件 hash 与冻结值一致。

| v49 改动文件 | SHA-256 |
| --- | --- |
| `src/orca_agent/context.py` | `be6e752c1c4d32fff734cbc3709e7c8d0560761656d27c8114a4af0df6c25c13` |
| `src/orca_agent/decision_purpose.py` | `1ef84d9c8e0eabd3355cfef4d07df8f1ed84f38f50964d8da5fd95de828cd466` |
| `tests/unit/test_code_context.py` | `8ff0725e50426619b6ed00d1c306344a4c6d18aecc44e2c260b801b291d5ef69` |
| `tests/unit/test_context.py` | `e26b649e0b52c901a7559d22ab6e79df9922ac180f2bdf2d22f89e4f1ed337fc` |

上述源码和测试在后续真实运行前冻结。本节更新没有修改 live019 原 Run、脚本或回复。提示更明确不等于模型一定会采用，也不证明 manifest、来源选择及最终 JSON 已由真实模型自主修复；这些仍须以新 Run 和独立核验分别报告。

## v49 产品不变：live021 开发故障门禁的调度来源修复

live021（`run_6621c9486fe04c2b92ba23858ccb24e5`）实际为 failed/partial，11 次模型调用、137330 tokens、0 Attempt、0 ORCA。模型已生成首行独立未定义名称 marker 的 Python，完成 workspace.write，并用 revise_plan 将其绑定到 Python Step。生产循环接受该计划后按既有逻辑取真实 ready[0]；它不要求模型再发一个 call_tool。原开发驱动误把“最后决策必须是 call_tool”当作首次计算的必要条件，因而报告 seeded not_established 并阻止执行。此处是测试驱动缺陷，不能记为产品无法执行合法计划，也不能把尚未发生的 NameError 或模型修复写成真实证据。

本次只修改 `scripts/code_tools_development.py` 和 `tests/unit/test_seeded_code_development.py`。生产源码、v49 prompt、配置、用户请求和任务内容均未修改。只读 inspect_seeded_execution 复用生产 `_step_results`/`_ready`，支持两种来源：第一，尚未 applied、没有既有 Call、basis 等于当前版本且指向真实 ready Step 的显式 call_tool；第二，无待处理调用时，当前已激活 Plan 的 immutable decision/hash 与版本匹配，且对应模型记录通过 read_model_reply 的请求/响应 hash 校验，原回复是 initial_plan/revise_plan，然后严格取 ready[0]。第二条不得筛选并跳过前面的非 Python Step。显式模型选择仍可选择任一真正 ready 的 Step，不被驱动改写成首项。

两条路径都拒绝未处理用户消息、控制信号和未处理 Result feedback；原有零 Attempt/内部调用/科学预约/工作量、同 Run 已验证 workspace.write Python Artifact、精确代码 hash 与 AST 首个可执行语句 marker 检查保留。新增 receipt 记录 selection_origin、来源 decision/model hash 和 Plan hash。观察器不执行代码、不补 Plan、不代填模型修复；后续任务修复仍由真实模型及原生产门禁处理。

| 本地 XML | 通过 / 失败 | XML 秒数 | 性质 |
| --- | ---: | ---: | --- |
| `seeded-selection-driver-first.xml` | 31 / 13 | 10.990 | 真实 live021 只读重放已通过；13 个新增合成模型 fixture 的提示缺少 JSON 字样，被 prepare_request 前置拒绝。 |
| `seeded-selection-driver-second.xml` | 44 / 0 | 15.060 | 只修 fixture 的提示后全部通过，没有放宽生产或驱动校验。 |
| `seeded-selection-driver-final.xml` | 122 / 0 | 24.495 | gate、development_budget、development_outcome 三模块；包括补充的显式非首 ready 选择与已结算 Step 拒绝。 |

XML 位于 `data/code-tools-step3/validation/`，最终文件 SHA-256 `418021842ca1139061f5023b1d5ff7d6b5a699261915e86d9cf0b87a5cdf672a`，终端耗时 24.83 秒。覆盖旧显式 call_tool fixture、已接受模型 Plan、未处理反馈/用户控制、stale basis、首 ready 非 Python、缺模型来源、请求/响应或 activated Plan 被篡改、已 applied 调用等正反例。轮次重叠，不累加；不是项目全量回归。Ruff 首次只有测试 import 顺序错误，调整后两文件通过，diff 检查通过。

另存的 `data/code-tools-step3/validation/live021-seeded-gate-replay.json`（SHA-256 `e1bda9300fd46f19fe4b72e70f1e32fc3a23cd6d91e1c71297439ca25264d3ed`）在原数据上只读核对当前来源为 `model_0517bdc185294ec9b6f09c4d305d190b` 的已接受修订计划，真实首 ready 为 `step_a22da47c749446b6a93af4061d3863d2`，marker 位于第 1 行。代码字节同时等于 workspace Call 的 content 与原初始 Plan 模型 writer 的 content。101 个原 Run 和关联 Artifact 文件在回放前后 hash 不变；新增模型、Attempt、ORCA 均为零。该结果只证明驱动现在识别既有合法调度来源，原 live021 仍保留 failed/not_established。

最终冻结 driver SHA-256 为 `8114dec5cb2677610593939d2cb915e0ab09166be17a587b2aa9cda4bd982f01`，测试文件为 `b2b550e7230bcd35c16436d489ad19017b1236c1fc1aaf528a024f3974d52c97`。后续受控错误真实运行使用相同 v49 产品和原模型输出上限；只有开发门禁来源识别发生上述变化。

## v50：跨计划修订保留已结算 Python 失败与完整修复合同

live022 的第一次 Python 确已执行：Attempt.started=true、state=failed，execution.not_started=false，原 stderr 是第 3 行未定义 marker 的 NameError。第 15 模型请求的 pending_step_ids 为空且包含该失败 Result 和冻结 Step；模型正确承认失败，随后生成 task_v2.py 的 writer Plan。该 Plan 移除了失败 Python Step。第 16 请求仍为空 ready，却不再显示失败 Result 正文或冻结 Step，只在 other_result_ids 留下 Result ID；step_execution 也只显示当前 Plan 的 Call。模型于是错误声称 v1 只写入、尚未执行，新增另一个 Step/logical ID，仍绑定旧代码和空 manifest。Store 的相同失败输入门禁正确拒绝，新 Attempt 和 ORCA 均未预约。这不是 _ready、not_started 或 Linux 执行状态错误。

v50 只修改通用上下文选择与展示，并注册新提示版本。relevant_result_ids 保留 Run 中具有匹配冻结 Step、已结束且绑定 Result 的 failed/timed_out Python Attempt 的 Result；AUTHORITY.failed_python_attempts 保留每条历史的 Attempt/Result ID、状态、started、输入 fingerprint 和完整 frozen_step，独立于当前 Plan。logical_id、代码引用/hash、输入 manifest 及 purpose、conditions、depends_on、observation_names、system_id、geometry 等保留原值。它们是历史事实，不成为 ready Step、许可或科学资格；同逻辑修复仍由模型提交新 Step，现有校验仍只允许换代码。多条失败不会因后续成功或预算耗尽而被覆盖；重复失败输入、累计预算、调度与执行门禁未改。

压缩编码必须保留整段历史的字面字段和值。调用方遗漏失败 Result、Result 与 Attempt 的 Run/Step/状态绑定不一致、超过 32 个所选 Result 或 token 上限时拒绝发送，不通过裁掉失败来满足容量。原有隐私投影若会删改合法 frozen_step 的字段或路径，同样失败关闭；没有绕过凭据和路径规则。独立审查实测发现 conditions.filename 会被删除、manifest purpose 中 file:// 路径会被隐去，因此增加投影前后严格相等检查和两个经实际 Step/Attempt reservation 建立的负例。**当前限制：这类需隐去内容的合法合同不能自动进入修复上下文，不能将删改后的合同当作原合同发送。**

| 本地 XML | 通过 / 失败 | XML 秒数 | 性质 |
| --- | ---: | ---: | --- |
| `failed-python-history-v50-first.xml` | 4 / 12 | 25.382 | 新负例误修改 commit_revision 前的旧 Attempt 对象；另有 15 条 fixture 将 dict 放入强类型 manifest 的序列化警告。 |
| `failed-python-history-v50-second.xml` | 21 / 1 | 29.170 | 修正对象和类型后，仅 token 负例直接赋值冻结 ModelLimits，尚未到被测上下文；改为 model_copy。 |
| `failed-python-history-v50-final.xml` | 188 / 0 | 92.978 | 七模块定向测试通过；随后独立审查发现上述安全投影会改变合同的问题。 |
| `failed-python-history-v50-final002.xml` | 190 / 0 | 92.172 | 增加投影一致性拒绝及两个负例后的最终七模块验证；终端耗时 92.68 秒，无 skip/warning。 |

七模块为 test_failed_python_history、test_code_context、test_context、test_decision_purpose_capacity、test_retained_plan_dependencies、test_python_repair_budget、test_context_ready_execution。覆盖实际 Store 失败结算→移除旧 Step→写新代码→读取完整修复合同，合法同逻辑代码替换及改 purpose 拒绝、多次历史、未知/未结束/缺 Result/身份不符排除、原生紧缩合同、缺正文和数量/token 上限失败关闭；已有 code 12k 失败关闭、64k 正控制与 writer 12k 容量控制均包含在此次测试内。轮次重叠，不累加为独立覆盖；这些是离线定向验证，不能替代真实模型修复和最终全量回归。五个改动 Python 文件 Ruff 与 git diff --check 通过。

上述 XML 均在 `data/code-tools-step3/validation/`。最终 XML SHA-256 为 `583a1d4a266b98c7df9a9932301d5f71afb955b7df0011c7df57d4b5169c5f6f`；前三轮分别为 `143da6f4f2d7f318833811830bc3109beaf41f07b5fa01b05a0c33034263888d`、`f09ad7384cf1cea92a617cebf0508365e93483c1332c011356add39842c805d0`、`996167fea90ab9d70a8e82e279d3265f16eefa8868c0e373b206c5f99057480a`。真实失败的小型 fixture 为 `tests/fixtures/code_contracts/live022-failed-python-history.json`，7505 字节，包含原记录来源 hash、精确失败 Attempt 冻结合同、第 16 请求遗漏情况和重复旧代码的提案，未执行其中代码。

最终源码下另做原 live022 的只读重放：从持久 Plan3、原第 16 请求和交付快照，在内存恢复相应视图；v50 的输入上界为 56486/64000 tokens，失败 Result 重新可见，frozen_step 逐字段相等，ready 仍为空且不开放 call_tool。127 个原 Run 文件及六个候选源码/测试/fixture 前后 hash 不变，新增模型、Attempt、ORCA 均为零；原 live022 仍是失败。最终回放 `data/code-tools-step3/validation/live022-v50-context-replay-002.json` SHA-256 为 `020e9c6414bc2b12a8543c326353351b9b6d5002ba3225a4aa0e3e6aab02ea99`。较早未含安全投影一致性拒绝的回放保留为 `live022-v50-context-replay.json`（`5ea660d481f5c33d648e8c9c212f90aa34e784442b334697858c4e03c5f60a9f`），没有覆盖它。

| v50 最终冻结文件 | SHA-256 |
| --- | --- |
| `src/orca_agent/context.py` | `c4a73afd175a94268dc1b729042d9d158609505ca8acc5ee730949b28bd9f561` |
| `src/orca_agent/decision_purpose.py` | `0de5b8e31af2660095119a23da5cedaee94af0ccb46ccab6ec222a452654d36d` |
| `tests/unit/test_failed_python_history.py` | `c7bf1a04a87d55ee8ce28e01e433dad0e39795bd5c478d5caf60086e7f6e892f` |
| `tests/unit/test_context.py` | `575932cb7247348221bb6ee615bdbe3929164c001f1ffbc21d1f3e11fa6c45a6` |
| `tests/unit/test_code_context.py` | `d3976ac0673f701c8b087d58cdb89c90878b9a3728016be77898653e9f667087` |
| `tests/fixtures/code_contracts/live022-failed-python-history.json` | `673c61facbcd476cd6baa11ff81ee824454fd2c218a944d7b9665536251bbd75` |

该修复不会使 live022 的 359 字节故障占位脚本获得完整科学任务合同。原首版没有输入 manifest、条件或观察名，改为完整计算需改变冻结目的，不能算原合同下的代码修复。后续真实对照须由模型从第一版就生成完整任务程序和合同；本次没有手工改写历史代码、输入或 Plan，也没有放宽修复校验来适配该占位脚本。

## v51：计划引用拒绝的准确位置与保留格式

live024 的第 13 个模型候选只有新 Python Step，其 depends_on 指向未列入该候选的三个旧 Step。实际首先失败于依赖键转换，旧反馈只有 KeyError；尚未走到 goal_map 检查。第 14、15 个候选补入旧 Step，却都把已执行 prepare 写成 `{key,depends_on}`，最先触发保留 Step 的通用拒绝。原完整上一提案已进入后续上下文，问题不是提案正文丢失；通用反馈没有指出第 1 索引的 prepare、额外 depends_on 和准确保留格式，模型仅调整 writer，随后耗尽有界纠正次数。三次拒绝都正确，原 Run 仍为 failed/partial、15 HTTP、227166 tokens、0 Attempt/ORCA。

本次只增强 proposals.py 的结构化错误：同一 depends_on 列表一次报告全部缺失键、逐项路径及其中可从历史复用的键，并给出仅 `{key}` 的保留形状；未知 retained key 与已知 key 夹带字段分别报告，后者包含具体索引、key 和 unexpected_fields。geometry/inputs 的 producer_key 使用同一小型解析函数，原 inputs.geometry 移入内部 geometry 时仍报告原字段路径。错误说明原依赖会随保留 Step 保持，依赖 Step 本身仍须显式列入候选，程序不会补 Step 或删除提交字段。已有未知 goal_map Step 检查未改；已执行冻结合同、许可、预算、累计用量和科学检查均未放宽，未执行旧 Step 的合法显式修改仍通过。提示只升版为 agent-json-v51 并注册，不增加指导段。

| 本地 XML | 通过 / 失败 | XML 秒数 | 性质 |
| --- | ---: | ---: | --- |
| `plan-reference-v51-first.xml` | 15 / 0 | 1.887 | 新模块首次运行；实际三次拒绝、准确 producer 路径、无输入修改及离线正反控制。 |
| `plan-reference-v51-final.xml` | 187 / 0 | 38.704 | 八模块相关回归；终端耗时 43.09 秒。 |
| `plan-reference-v51-legacy-capacity.xml` | 6 / 0 | 77.176 | 水/甲烷 × 临时/开发/正式路径六个旧非代码 12k 场景；终端耗时 77.21 秒。 |

八模块为 test_plan_reference_feedback、test_proposals、test_retained_dependency_feedback、test_retained_plan_dependencies、test_rejected_proposal_context、test_context、test_code_context、test_decision_purpose_capacity；其中包含 code 12k 失败关闭及 64k 正控制、writer 12k 容量控制。三个测试命令均无失败、skip 或 warning，轮次重叠不累计为独立覆盖，不是全项目回归。六个改动 Python 文件 Ruff 通过，git diff --check 通过。探索性只读命令曾误查不存在的模型 .json 路径、误取 model_calls 字段，各报一次错误，随后分别改为现有 .request/.response 文件和 model_records；没有改写原记录或形成测试成功证据。

三份 XML 均位于 `data/code-tools-step3/validation/`，按表顺序 SHA-256 分别为 `f1f560b48034ab8e15228b7bf7544a60be41a3816957d337239d38a89dad6798`、`39c350f4349df76e39cece883416d94c82e4961a7f7a29ce922d82fb8894dfae`、`c6bd68b342dc18923034922f984e23850bc99b2081280c128f91949033a7a16e`。

新 fixture `tests/fixtures/code_contracts/live024-plan-reference-rejections.json` 为 53437 字节，包含原 Request、Plan、三份实际候选、来源 hash 和明确裁剪的 Run 视图。preceding_validation_error/preceding_rejected_projection 明确表示该请求携带的上一候选反馈，不误记为本候选的拒绝原因。三旧 Step 仅 `{key}` 的正控制由测试显式构造，materialize_plan 与 validate_revision 均通过且保留原 Step 的全部字段；这是离线合同测试，不是模型修复或科学执行。只删 prepare/writer 的额外字段仍保留 resolve 的不完整显式重建，继续被 reserved Step 不可变检查拒绝，证明本次没有绕过其他门禁。

最终源码只读重放原 live024：read_model_reply 校验原 request/response hash 后重放三份精确候选，分别返回 missing_step_reference、retained_step_has_fields、retained_step_has_fields；提案参数与 Run 均未修改。93 个原 Run 文件前后 hash 相同；没有模型调用、Plan 激活、Attempt 或 ORCA。回放文件 `data/code-tools-step3/validation/live024-v51-plan-replay.json` SHA-256 为 `d52f7da1a9215e671f3367084161a3ef98bbdfe0312db96b937e609de6625230`，内含逐文件 hash 和候选源码 hash。更准确的反馈不证明模型必能修复，后续原文与受控错误真实运行仍须在同一冻结版本分别验证。

| v51 最终冻结文件 | SHA-256 |
| --- | --- |
| `src/orca_agent/proposals.py` | `5681b8828eecfdda1d1adfbd95b5acf013a413736919816f4e641a3653c6f72e` |
| `src/orca_agent/context.py` | `8910d39ed5816967406d12c221dbadbae6f55c2321bc0b7e3544e79becf48f2d` |
| `src/orca_agent/decision_purpose.py` | `a09529273e7ec0ee4351c39e472b4e5910f59fb2544cce5add17dd99dcb00b29` |
| `tests/unit/test_plan_reference_feedback.py` | `489aaaf8be68de0faf538acf0c21987450e2ac6db022b6aabfd6d98e25da560c` |
| `tests/unit/test_context.py` | `27684f2de05f1096da82a48747f7d1ff4754d6596560d718fabb3cafd4a3b794` |
| `tests/unit/test_code_context.py` | `83b55bcee2fc116742a5338d6277b9b608964992e0415b72748cb244424cb3d1` |
| `tests/fixtures/code_contracts/live024-plan-reference-rejections.json` | `70d71b908aa2fd1d14a09c6910d04d997352ef6662d49f82bbaabc18faa57db5` |

## v52：披露已有原生块标量语法与可选输出字段

live027 在同一 Run 内真实建立了有界代码修复：首版 3987 字节包含 SDK 调用、XYZ/频率解析和观察交付，首个执行语句为第 2 行 marker；首 Attempt 在该行 NameError。模型自行写出 3955 字节修订 Artifact，逐字节恰好只删除 32 字节 marker 行，并提交新 Step ID、同 logical_id 的修复。除 Step.id 和 CodeRef 外，两次完整 frozen_step 逐值相等。第二 Attempt 真正进入 run_orca，却在启动 ORCA 前被原生输入校验拒绝：模型输入的 `%output` 块只有 `jsonpropfile`，没有所需标量值。原 Run failed/partial，18 HTTP、317810 tokens、两次失败 Python、0 ORCA；两科学目标仍 insufficient_evidence，不能计为受控错误科学闭环成功。

只读代码/合同审计为 `data/code-tools-step3/validation/live027-code-repair-audit.json`，SHA-256 `e06a9e7aa3694803353b7a0dd31f5baa397217d43fc0b171b964511ee9579851`。它校验真实 writer、Artifact 与原模型回复来源，172 个原 Run 文件前后 hash 不变。两次 Step.system_id 均为 null，现有生产 native_input_binding 根据唯一乙醇 Request 体系绑定，审计没有补字段。审计期间一次待返回 response 文件读取、一次错误 Result 属性查询、一次未执行的审计命令语法错误均在 JSON 记明；修正后只读审计通过，没有更改历史记录。程序包含任务工作不代表输入已准入或科学已验证。

v52 只在 profile_contract 中公开既有通用块形状 `%<block> <key> <scalar> ... end`、每字段必须有值、不得裸 flag/嵌套以及独立 %maxcore 规则。可选 output 字段名从 BLOCK_FIELDS.output 导出，公开既有 true/false/0/1 值和 `%output JsonPropFile true end` 示例；不强制添加该块。native.py 标量校验及 admit_profile 没有修改，未扩展方法、执行或科学保证。PROMPT_VERSION 与注册升到 agent-json-v52，两个版本预期同步，未另加任务指导段。

| 本地 XML | 通过 / 失败 | XML 秒数 | 性质 |
| --- | ---: | ---: | --- |
| `native-block-v52-first.xml` | 10 / 0 | 0.412 | 新模块首次运行。 |
| `native-block-v52-final.xml` | 193 / 0 | 34.394 | 五模块相关回归；终端耗时 34.51 秒。 |

五模块为 test_native_block_guidance、test_native_analytic、test_code_context、test_context、test_decision_purpose_capacity。覆盖原 live027 精确输入仍拒绝、只在内存补 true 后通过原准入、四种布尔值及完全省略 output 的正控制、非法布尔值/重复字段/嵌套负控制、模型上下文里的字面合同与容量。没有测试失败、skip 或 warning；轮次重叠不累计为独立覆盖，不是全量或真实 ORCA 验证。六个改动 Python 文件 Ruff 与 git diff --check 通过。XML 位于 `data/code-tools-step3/validation/`，SHA-256 依次为 `58e1601a5541881024bcf2bc09dad602fb32017f1e3e6724cd47dde0569acb19`、`757f5c46d0ecbd33f287c505810febbea244ca7eb1ef6bce932b5fe9efd50dc0`。

真实输入/几何 fixture `tests/fixtures/code_contracts/live027-native-output-flag.json` 为 3859 字节，保留原 Artifact/hash、冻结资源/策略、生产绑定及失败来源。最终源码在原数据上再次只读准入回放：原输入仍报同一错误，仅在内存补 true 的合成字符串通过，声明工作量 157、scientific_validation=unverified。没有改写 Artifact、执行程序或启动 ORCA，172 原 Run 文件与输入 Artifact hash 前后不变。回放 `data/code-tools-step3/validation/live027-v52-native-admission-replay.json` SHA-256 为 `8f4d8b3ce805603fbdfc1ca1361a6e278c9dfe9a80887a90a58981088c604611`。更清楚的语法披露不保证后续模型写对或科学完成，仍待同版本真实运行。

| v52 最终冻结文件 | SHA-256 |
| --- | --- |
| `src/orca_agent/orca/native_profile.py` | `b3eb3d60798088e67fe8b4abbf5a50a67ecb90069f04b8b1f35b5deca04b646f` |
| `src/orca_agent/context.py` | `8df6426ec5a4aad7d73a5c49f24ffde19b92318032c2c6d93a5e366a5068f487` |
| `src/orca_agent/decision_purpose.py` | `3b078f44a5459ecdd2af41deea538280e466cff02c7b8d3dd86b383e3adba0c0` |
| `tests/unit/test_native_block_guidance.py` | `3ee86e9ed6c727c4a686322a85745fdb2ffde25821350f55f6037a0d169eb6c2` |
| `tests/unit/test_context.py` | `3e0f1df6aad3301da3047ce89e9f02a97d4dd022de3b37ab1d8e0a80a5b7d8e3` |
| `tests/unit/test_code_context.py` | `f7c1b629127d3b141a6986733a983e22bde7aa8e0b556b1be6d2045141fe1dff` |
| `tests/fixtures/code_contracts/live027-native-output-flag.json` | `9511081a0cbe6c752ca60eb55d3eb92e7c184aa7a0fc76309f23abdd5dc7bd38` |

未修改的原 native.py SHA-256 为 `2bb8bcc4b77b7421845cb2591cbadfbe279427f082e83b76e2b7a38f67e30cea`。本轮没有替模型修订真实输入，也没有改变修复合同来允许修复后换入不同 manifest。

## v53：完整序列、筛选披露与物理单位指导

live028 已真实完成 marker-only 修复和第二次 Python/ORCA 执行；独立科学核验确认原输出含 27 个模式，其中六个零值、21 个正值，原生优化/频率检查通过。但模型代码只交付 value>1e-6 的 21 项且未披露筛选，优化坐标字符串 unit=null，报告显示未知单位，因而不满足预定完整性与单位交付验收。`validation/live028-independent-scientific-audit.json` 明确 raw_orca_scientific_checks_passed=true、independent_development_acceptance_passed=false，SHA-256 `83a1b1bbc431f1d22dbf9788dea0c1cfe2207e7354c9e831761d120a85e77a42`；这不能被 operation completed 或 Goal satisfied 取代。

v53 仅在已有通用 _CODE_GUIDANCE 增加 387 字节：交付完整解析序列，保留负值及近零值；额外筛选子集须明确筛选与遗漏，不替代完整数据，也不能据筛选后序列排除异常；已知有来源的物理单位即使在字符串/坐标文件中也应显式声明。原 null=unknown 定义保留，未知单位仍为 null 并披露 limitation，不能猜测或将 null 改释成无量纲；确有依据的无量纲量应明确标注。未加入科学参考数值、任务解析算法或新 checker；未改观察校验、权限、预算或历史产物。版本升为 agent-json-v53 并保留旧版本注册。

| 本地 XML | 通过 / 失败 | XML 秒数 | 性质 |
| --- | ---: | ---: | --- |
| `observation-guidance-v53-first.xml` | 31 / 1 | 10.320 | 原代码 writer 12k 正控制超限失败关闭；终端耗时 11.31 秒。 |
| `observation-guidance-v53-final.xml` | 129 / 0 | 33.099 | code_context、context、decision_purpose_capacity 三模块；终端耗时 34.28 秒。 |
| `observation-guidance-v53-legacy-capacity.xml` | 6 / 0 | 78.078 | 原非代码水/甲烷 × 临时/开发/正式路径六个 12k 场景；终端耗时 78.11 秒。 |

新增指导使代码 writer 的最紧保守输入界约从 11972 增至 12359，原 12k 余量不足。按明确范围决定保留新增指导和原契约，不压缩或删除旧要求，也不增大运行预算。测试改为保留同一 writer 场景的 12k 发送前 ContextLimitError 负例，并增加实际当前 64k 配置下的正例；原 Artifact/hash/大小、schema、权限和不内联任务代码断言仍全部保留，负例确认无模型预约或状态修改。**兼容性限制：该代码 writer 反馈场景自 v53 不再兼容 12k；未改变任何历史或实际 Run 的限额。** 原非代码六场景继续按原 12k 正例通过。该限制已追加至 independent-review-plan，不以改测试掩盖容量变化。

最终两轮没有失败、skip 或 warning；首轮真实失败保留，不与后续覆盖累计。四个改动 Python 文件 Ruff 与 git diff --check 通过；这些是离线相关验证，不是全量回归或真实模型表现。三份 XML 均位于 `data/code-tools-step3/validation/`，按表顺序 SHA-256 为 `1cbeafb60253c3366ff53579cdfabed0e5db58ffddbc02d7c3a72b0ce9bf6813`、`792750e67b48e6811f1273c10173fd22f4f6a59a31fc0970e83fae5f788c8f59`、`d6078e94d0d67aa6b5046396d319b050660c1721c1a37d7692325c842f1f4225`。

新 fixture `tests/fixtures/code_contracts/live028-observation-disclosure-gap.json` 为 11684 字节，保留真实 Result 的两项观察、原 stdout 频率块、原代码筛选行与来源 hash，明确它是部分 Result 视图和交付缺项记录。测试证明新投影仍保留原 21 项、unit=null、未披露筛选与 unverified 状态，不自动补入零值、猜测单位或升级资格。通用指导进入实际 64k 代码上下文和纯文件代码上下文，原非代码上下文不增加 TASK_CODE_RULES；没有把 fixture 的科学数值或筛选算法写入生产提示。

最终源码对原 live028 Result 的只读回放完整保留三项观察，221 个原 Run 文件 hash 前后不变；没有模型/ORCA 调用或历史改写。记录为 `data/code-tools-step3/validation/live028-v53-observation-feedback-replay.json`，SHA-256 `8d19e5a8b155934e30fde497ca2203f89a0c524b75ed36ab5b83c940abe741b1`。该提示修订不能追溯修复 live028 的交付缺项，后续仍须同一冻结版本的新受控错误与原文 Run，以及独立科学核验。

| v53 最终冻结文件 | SHA-256 |
| --- | --- |
| `src/orca_agent/context.py` | `439735c8fa320fffa8cac39b2c54b3d93d4bc3c9a94fdd7ae531b09bc8e5338c` |
| `src/orca_agent/decision_purpose.py` | `1eb06063ce6863687b6063ed50e27b2aebd76f092c796116edeeef4265b48917` |
| `tests/unit/test_context.py` | `3e83763db799319193cd29710fbb9792da3b9af104192dcd42b573531c01e84c` |
| `tests/unit/test_code_context.py` | `6cba02458529d745a7d8a5a364807ea7b2a64cfb270cbd1a088e7b44a9cd9bae` |
| `tests/fixtures/code_contracts/live028-observation-disclosure-gap.json` | `34d36cc70d3a9f67d5eff4471ed01d1163e8e33fee5bed34d2e0e2a79b8cd378` |


## v54：首次身份归一化中的查询别名指导

live026 与 live030 的真实 normalize_request 均省略 identities[].query_name；原 name=乙醇 因而由既有规则成为冻结查询名。两次 structure.resolve 对同一中文名称 URL 返回完整 HTTP 404，原响应 SHA-256 都为 fbfc1748cfbb386bfb21964a4b487153d17c908ce3bd58db1ff68f55de4dd691。没有 identity 资格，后续几何/Python/ORCA 均未建立。Tool 参数只有 system_id，不能在调用中另传 alias；同 Request 的同参数输入获取已经有 durable Call，换 Step ID 也不能重试。这是现有防重复边界，不是 ORCA 或容器故障。原 normalize_request 的身份绑定只允许尚未绑定的首次 intake，不能将普通计划修订或再次 normalize 冒充身份修订；本次没有新增已冻结身份的编辑/恢复机制。

v54 只在 code_authorized 的 generated_task_intent 增加两句通用指导：非英文名有确信的英文查询别名时可写 query_name，name 保留用户原文；别名仍为推断，必须经过既有身份来源独立确认，不知道时不得猜。程序没有添加翻译表、自动填值、请求覆盖或新重试。现有 PubChem 同义词或 Wikidata 原文 label/alias 与同 CID 的来源检查不变。PROMPT_VERSION/注册及两个版本预期升为 agent-json-v54，历史版本继续注册；未增加任务专用名称、SDK 指导、解析算法或科学资格。

| 本地 XML | 通过 / 失败 | XML 秒数 | 性质 |
| --- | ---: | ---: | --- |
| `identity-query-v54-first.xml` | 78 / 0 | 18.871 | test_identity_query_guidance、test_general_identity_selection、test_code_context；终端 19.16 秒。 |
| `identity-query-v54-context.xml` | 96 / 0 | 23.436 | test_context、test_decision_purpose_capacity；终端 24.17 秒。 |

两轮均无测试失败、error 或 skip，属于离线相关回归，不是全量或真实外部查询。已有 alias 来源正例及不同 CID、近似名称、多 CID、错误 property 等拒绝例均通过；新指导仅在授权代码 intake 出现，原非代码分支保持不变，当前代码 64k 上限通过，已有 12k 失败关闭边界未放宽。六个改动 Python 文件 Ruff 与 git diff --check 通过；后者提示既有文件换行转换警告，没有差异空白错误。XML 位于 data/code-tools-step3/validation，SHA-256 依次为 e84aff936fa15f19c37d8385fc4abedc0344b2b6aa8da9c3593f827f0d3c276d、933d6502ec027584dce4879681567e49264b1f497fc0e13dc24505c91dc54709。

fixture tests/fixtures/code_contracts/live026-030-identity-query.json 为 13191 字节，保留两次精确 normalization 参数、原用户文本、冻结 identity、404 失败摘要和原模型 request/response/Result hash；SHA-256 为 8c7b9d3b2b65f617e2ad0208fa5a4599164aa316612decbe41051ad31e0ae388。离线重放仅重绑定临时测试消息 ID：原候选仍选择中文 query；另两项由测试作者显式加英文 alias 的正控制只证明合法首次绑定，仍 canonical_names=[]、没有 alias_verified、没有 Tool/Attempt/Result，不冒充真实模型修复或来源确认。参数和 fixture 前后不变。

提取前后，live026 的 42 个原 Run 文件及 live030 的 47 个原 Run 文件逐文件 hash 相同。记录 data/code-tools-step3/validation/live026-030-v54-fixture-extraction.json 的 SHA-256 为 b1a25efc8edef0a7e009c36a7452d626e2fd3df704e2749cf435ee855429a032。探索性只读脚本曾误用不存在的 model_transport 模块、误解 read_model_reply 返回的 tuple/dict 形状及未指定 UTF-8，各报错后修正；一次 rg 指向不存在的测试文件。均未执行外部查询或改写历史，也未计为验证通过。提示改善不能保证后续模型选对别名或外部服务成功，仍需新冻结版本真实验证。

| v54 最终冻结文件 | SHA-256 |
| --- | --- |
| `src/orca_agent/semantic.py` | `03fbde9b5fe1930c1501282c5d1ef32e4bc98f10c9a4f97b1d748210b19fd594` |
| `src/orca_agent/context.py` | `30319c3d1d7a7676a69d3f6a947838c5db21407df5f3916da10efc10618eecb5` |
| `src/orca_agent/decision_purpose.py` | `0fe9a20206fed99245f9e3c0879f5efe7b7f4ae318b24e91b639e2fcd120329a` |
| `tests/unit/test_context.py` | `45860205dc54f1d901a9fea2073fbfb23fd461f51ac31335c9b15be67091c02c` |
| `tests/unit/test_code_context.py` | `ca557942d8350e187016dbea5705158dc70715a13c4b24039ed04caf0a774007` |
| `tests/unit/test_identity_query_guidance.py` | `95ae8617367bbe6f3d7e3131f1cac101eebea3a129a553f3307db123c795b73b` |


## v55：目标证据规则拒绝的完整有界诊断

live033 原文 Run 中 Python 与内部 ORCA 均 completed，Result 包含带单位的优化几何和 27 项频率观察；模型却在交付路由修订中第一次省略 evidence.rule_version、第二次省略外层 port，随后误认为未注册科学 checker 就不能交付 reported_with_limitations 观察，终止为 failed/partial。原观察/目标状态不因此改写。只读审计 data/code-tools-step3/validation/live033-code-binding-audit.json（SHA-256 6fe20239014d0cd5e7f1451e5a6caa74ee36c5624ecc396529f13abeea4b2d05）保留完整代码来源与实际三份候选；201 个原 Run 文件不变。独立科学检查由另一个审计记录负责，程序 completed 不替代科学和目标验收。

v55 保持 PlanningError 类型、原规则兼容判断与全部准入条件，仅为这一缺少/不兼容 rule_version 的拒绝增加可选结构化 detail：Goal.id、准确提案路径、expected/actual rule，以及当 producer 已在 Plan 中时可复制的完整外层 step_key+port 路由。只有 Goal 本身为 generic-observation-1 且 reported_with_limitations 时，诊断才说明该规则是带限制的观察交付契约，不要求注册 scientific checker；观察仍 unverified，源与限制必须保留。显式 qualified 目标没有这一提示，绑定 generic Python 仍被原 validate_goal_output_contract 拒绝。程序没有自动补字段、修改 Plan、重新计算或提升资格。

agent 只对明确的 PlanningError.detail 使用结构化反馈，与原 ProposalError 同样避免普通错误字符串 300 字符截断；任意其他异常的 detail 属性不会被采纳。独立代码审查发现 EvidenceRef.rule_version 原类型没有长度上限，直接回显会撑大纠正上下文。最终增加仅显示用的 _rule_feedback：None 和最多 128 字符的安全版本 token 保留；其他标签显示明确 omitted、原字符数和 SHA-256，不改原字符串，不把显示占位当有效规则。20K 标签、换行和文件 URI 标签继续被同一原准入拒绝，完整诊断低于 2000 字符，原候选/Run 值不变。版本标识升至 agent-json-v55 并保留历史注册；没有新增任务算法或科学参考值。

| 本地 XML | 通过 / 失败 | XML 秒数 | 性质 |
| --- | ---: | ---: | --- |
| `goal-rule-v55-first.xml` | 6 / 2 | 2.622 | 新模块首次运行；两项合成 qualified 测试构造错误。 |
| `goal-rule-v55-final.xml` | 213 / 0 | 38.992 | 七模块相关回归；终端 39.53 秒。 |
| `goal-rule-v55-qualification.xml` | 13 / 0 | 8.284 | 明确资格拒绝与既有观察不得升级资格/目的/值/hash 的回归；终端 8.61 秒。 |
| `goal-rule-v55-bounded.xml` | 62 / 0 | 4.337 | 最终有界标签反馈、新模块、planning、rejected_proposal_context；终端 4.39 秒。 |

首轮两个失败来自测试作者误将 evidence_contract_version 设成非法 optimization-1，该字段只允许 legacy-1 或 generic-observation-1，于是在 Request schema 提前被拒，没有到预期 PlanningError。只将合成测试的该字段修成合法 legacy-1，保留 minimum_check_version=optimization-1；生产校验未放宽。后续明确断言 qualified Step 路由被 generic observations cannot replace required scientific qualification 拒绝、错物理端口被原 quantity 检查拒绝，错误 rule 仍拒绝。七模块为 test_goal_evidence_rule_feedback、test_planning、test_proposals、test_rejected_proposal_context、test_context、test_code_context、test_decision_purpose_capacity。后续各轮无失败/error/skip，重叠测试不累计为独立覆盖；均为离线测试，不是真实模型修复。七个改动 Python 文件 Ruff 与 git diff --check 通过；原非代码 context 和当前代码容量回归通过，未变更 Run 限额。

四份 XML 均位于 data/code-tools-step3/validation，按表顺序 SHA-256 为 5f175a8bde367534d06f2b40f61092ab539224f40a2b76571dacaed9e4797bef、0f80da8b83883eef2c5cb544cd42b9828cb4d2b69a7af4a3ad93d9b142c72a4d、b0c7f04bfa9bf02a2bfc101fabe46fa28f2294257dd6bd0e5454642c301a9df1、0d34a363b3174a61bb3c69f9b320f01086e1222f1c8f52b1252c623fa63e4b9a。

实际 033 fixture tests/fixtures/code_contracts/live033-goal-evidence-rule.json 为 51427 字节，SHA-256 62735596ef13a4c0c946e3edb337d595ab3dcee514785005061c81b1e973de98，保留原 Request、Plan、拒绝提案、来源 hash 和明确裁剪的 Run 上下文。真实候选仍被拒且完整 detail 能穿过实际离线决策循环进入后续模型 context；超过 300 字符部分未截断。纯内存测试作者仅将两个证据绑定改成已有 Step 的 step_key+port，materialize_plan/validate_revision 通过且所有 frozen Step 不变；没有激活候选或修改原 failed/partial 与资格事实。

最终有界反馈源码对原 033 只读复验见 data/code-tools-step3/validation/live033-v55-rule-feedback-replay-002.json，SHA-256 3a6b86f45f2f1f87d402aebba9d01f8ceaf4e3119672ac698652ec1d27e77c66；201 个原 Run 文件逐文件 hash 不变。较早未加入标签显示边界的回放保留为 live033-v55-rule-feedback-replay.json（SHA-256 f6ea949db27109b2803e6a5d5a4c9de6a88fc5e5c3d014083c8ba2ecb76efd90），不覆盖或冒充最终源码记录。探索性 rg 曾指定不存在的候选测试文件，随后使用实际文件；没有因此形成通过证据。

| v55 最终冻结文件 | SHA-256 |
| --- | --- |
| `src/orca_agent/planning.py` | `406856798cebcc94e8d720a66851938e6fd017cec7a45b46b27112ab52eee389` |
| `src/orca_agent/agent.py` | `7c684c98b90376da774bfc5b1589577beb8ed2142fd22cc6366cf7fae1baa5bd` |
| `src/orca_agent/context.py` | `c9caefe53b6f5d149fac67369cfd23c3872901f7b98639a2bf4c1fe56927f59c` |
| `src/orca_agent/decision_purpose.py` | `559241c2a493b2313c764f191295e8fe9dada43d121ba39762120703596741bc` |
| `tests/unit/test_goal_evidence_rule_feedback.py` | `a16a58accd9445ff554ccf35188965534bfe47836488ac94833555669857d712` |
| `tests/unit/test_context.py` | `510d79eb90db8b735f52a29d191968ff2307292e8ad8c2d001404a0b2543f96f` |
| `tests/unit/test_code_context.py` | `b4f0cda006b7e5c759832559399a02468313f71375b64f9ba1372cf2f7222cd5` |

## Pro v5 配置验证（生产 v55 不变）

live034 结束后仅将下一候选的既有 model_name 配置改为 deepseek-v4-pro，保留 thinking_low、原协议、64k/16k、180 秒期限、资源及全部累计预算，旧 v4 配置不变。新配置与价源见 [配置证据](validation/model-pro-v5-configuration.json)及独立核验计划的前瞻决定。load_config 与 prepare_request 本地检查证明只改变 model.model_name；没有通过该探针发起模型或 ORCA。

既有 `tests/unit/test_model_selection.py` 与 `tests/unit/test_model_profile.py` 共 **38 passed，8.34 s**；JUnit：`data/code-tools-step3/validation/model-pro-v5-config.xml`。两文件测试覆盖两种既有模型名称及冻结 JSON/profile 参数，也确认 Pro 在没有产品价格表时费用仍为 None/unknown，而非套用 Flash 价格。配置成功只证明本地兼容，实际服务行为待新的真实 Run；历史性能、失败和开销不回填。

## 最终只读审查：极端非法引用的纠正容量限制

在不修改 v55 候选的前提下，纯内存构造一个 100895 字节的提案，包含 100K 字符的未知 Step 依赖名。它低于当前 131072 字节提案上限，但被拒时原样引用的结构化诊断达到 200627 字节。当前 64k 配置的 `build_context` 明确抛出 `ContextLimitError`，发生在 `send_model` 前；没有 HTTP、Tool 调用或 Attempt，Request、Run、候选及拒绝事实均未改变。真实决策循环先保存拒绝，再在上下文超限时记录失败并停止，不会无限重试或错误完成。

这说明极端非法大字段不保证可继续模型纠正；它不是执行权限、累计预算或科学资格的放宽。本次保留安全有界停止并如实记录这一限制，没有为该反例修改生产候选。只读证据：`data/code-tools-step3/validation/oversized-reference-feedback-failclosed-audit.json`，2901 字节，SHA-256 `eb4bf3e055f7dcd66bb1c2424fffd896a681169f73f9ea8fcccf16b01aada790`。该检查是离线构造，不计真实失败修复通过。


## 第一轮冻结全量回归（修复前，非最终通过）

2026-10-19 11:36:17—12:14:05 UTC，默认离线 `pytest -q` 完成 4433 项：**4231 通过、23 失败、179 跳过**，耗时 2266.06 秒。驱动在前后核对 9570 个源文件及显式历史输入，freeze_unchanged=true，目录、文件、运行时版本与驱动均无变化；全程停止了受冻结文件的开发写入。1 条 Starlette/httpx 弃用警告保留，没有为此升级锁定依赖。此次新增真实模型及 ORCA 均为零。

[公共摘要](validation/continuation-regression-001.json)列出全部失败名、跳过原因及原始日志/XML/冻结文件 hash。原始输出位于 `data/code-tools-step3/continuation-regression-001/`，日志 SHA-256 `06c7fa0c219cca1de553ebdee1bd5264c3f45208f85e0412a9e4548c3b23b264`，XML SHA-256 `a9cd8c0ff917e145c37c020e78d06f1b6980ec0edf6418cde65eebf476599005`。完整 XML 为 1116337 字节，保留本地，未作为大体积原始运行产物提交。

独立诊断把 23 个失败分为：15 项历史时间或精确展示断言过期，1 项属于已声明的代码 writer 12k 容量限制但测试尚未同步，7 项为真实非代码容量回归（3 个水输入链、4 个读取反馈场景）。这一分类不是修复后通过；实际修复后的定向结果另记；用户随后取消后续全量回归。特别是读取纠正还受最终说明预约限制，单次输入低于 12k 不能代替累计余额与最终说明余量检查。

冻结期间的独立实验只写 ignored 临时目录。完整代码候选及回执展示被错误扩展到非代码运行，与 0034 的既定范围不符；范围恢复的离线原型已保留代码授权请求 body 逐字一致，并使三个水流程在原预算下通过，但读取反馈仍有待处理的容量缺口。编码排列、仅缩短提示、仅限定 candidate 等未解决问题的实验均保留为失败或不足，不报告成最终实现。正式修复开始于本轮前后 hash 核对完成之后，决策先记入 0034。


## v56 范围修复及最终定向验证

用户明确要求停止后续全量回归，按已发现问题修复、验证后交付。本轮恢复非代码的既有展示范围：新增完整拒绝候选、额外执行回执视图及指导仅在代码授权上下文展示；非代码末级恢复原 schema 投影和可逆列式/共享。协调者的完整持久化记录、校验错误、原任务事实、许可、真实执行门禁和预算不变。旧 `_schema` 投影省略默认值等注释，不意味着 registry 默认被删除或改写；代码模式的字面字段、默认、完整失败合同与拒绝候选仍保留。

- [v56 上下文定向记录](validation/v56-targeted-context-compatibility.json)：相关批次 133 通过、3 个新增测试构造失败；修正测试入口/独立 schema 比较后针对性复验 4 通过，当前版本断言 1 通过。三个旧水流程均通过原单次/累计门禁，未抬预算；代码授权的历史拒绝候选和失败 Python 上下文原型 body 逐字一致。批次有重叠，不相加成独立总数。
- [历史测试修复记录](validation/stale-test-repairs-five-modules-001.json)：首轮 132 通过、3 个旧 schema 表达断言失败；修正后五模块 **135 通过**。历史时钟、当前工具/ready 枚举、旧 allOf 与 compact oneOf 等价、12k 明确拒绝/独立 64k 正控分别验证，原证据和冻结预算不改写。
- [读取反馈定向记录](validation/query-feedback-repairs-v56.json)：模块最终 **10 通过**，包含首轮的四个真实失败；历史纠正保守输入 11128、输出 2000、最终说明预约 12000，合计 25128，不超过剩余 25303，尚余 175。V07 四次保守输入 9612/11359/11703/8094，保留顺序、原值、来源、单位与未验证状态；无效调用仍不执行或覆盖 Plan。该轨迹模型使用量为脚本模拟，不是真实计费或真实模型可靠性证明。详细原始记录位于 `data/code-tools-step3/query-regression-diagnostic/`。

相关 Ruff 与差异格式检查通过。正式改动后没有再次运行全量回归，不能把以上结果称为全量零失败；原 4231/23/179 基线保留。所有本节测试及原型均为离线验证，新增真实模型与 ORCA 为零；后续 live037 单独记录。live035 继续保留 v55 原文成功身份，不改称 v56 原文重复成功。


## v57：显式冻结步骤修复引用与定向收尾

live037 的实际 NameError 后，模型改写冻结 manifest，修复未被执行。[0035](../../decisions/0035-declarative-code-repair-reference.md)先记录问题和替代方案，再增加局部闭合提案 `{key, repair_of, code}`。程序按模型明确指定的本 Run 最新已结算失败 Python Attempt 复用其冻结 Step，仅替换 ID/CodeRef，保留 logical_id 和所有其他字段；Result 的 Run/Attempt/Step/状态须精确匹配，依赖仍显式保留，仍经过原 validate_revision、Artifact、许可和预算检查。没有新 Tool、求解算法、科学检查器、恢复或多计算机制。代码授权上下文提供该形式；旧完整 Step 仍合法，非代码 schema 退回同一类型定义的既有分支。

独立检查还发现普通旧 Step 的错误反馈混入新 union 分支的缺字段和指导。最终在八条错误上限之前按该项明确提交的形态筛选相关分支，不改变验证接受/拒绝结果；旧形态保留 v56 已有通用字段和指导，新引用错误给出自身闭合字段。最初独立审查错误地拿 Git HEAD 的早期 validator 作为 v56 完整反馈基线，已保留并纠正该比较；HEAD 的四个未变化 Step/Goal 类型仅用于经 AST 核对相同的旧 schema 比较，不冒充整个 v56 validator。

[新引用定向证据](validation/v57-declarative-code-repair-targeted.json)保留首轮 39P/1F、后续 193P/1F 和单项 1P。两处失败分别是测试将带默认字段的 CodeRef 与 writer 的原始两字段字典等同，以及旧结构投影断言未包含新增的不可达定义；只修正测试的类型等同/精确可达集，没有放宽准入。[最终反馈及引用定向](validation/v57-declarative-code-repair-final.json)为 **102P、0F、0E**，覆盖明确引用、冻结合同、错误或过期 Result 绑定、权限、原 Artifact/预算边界、旧形态和错误反馈定位。各组有重叠，不相加。

[上下文与容量记录](validation/v57-context-compatibility-final.json)的六模块批次为 **193P/1F**，唯一失败是同一个代码上下文测试的旧提示原句；原水流程、读取反馈、真实冻结合同展示、旧容量均已通过。提示断言第一次更新后，小范围复验为 **32P/1F**，到达该测试第二条旧原句；两条均同步为新合同的等义说明后，仅该测试的 12k/64k 两个参数复验 **2P**。低容量仍明确失败关闭，高容量仍保留完整失败身份；未改预算或执行条件。两批各有 528 个 src/tests/scripts 文件的前后 hash，changed_files=[]。首批 pytest 已保存 XML/outcome 后，包装脚本按 UTF-8 显示 Windows 日志遇 UnicodeDecodeError；这是日志显示失败，原日志和测试结果完整保留，不额外算成测试失败或通过。

独立有限探针验证声明式展开只改变 ID/CodeRef，十八个负例拒绝；旧非代码 schema 与既有类型源相等，序列化新增 0 字节。完整源绑定、十八个反例及真实旧反馈重放见[独立复核](validation/v57-independent-repair-reference-review.json)。50 个改动 Python 文件 Ruff 及 git diff --check 通过。以上均为离线验证，没有模型、ORCA 或开发预约。用户取消全量回归的要求持续有效；本节不表示最终候选全量通过。

前瞻 v5 受控请求只把 v4 的协议标识和强制旧 logical_key 字段的措辞改为保留 logical_id、使用当前明确引用或合法完整修订；任务、唯一人为故障、首 SDK 前 NameError、完整程序及独立科学交付标准均未改。v57 / Pro-v5 / v5 的 live038 已另行冻结启动，其结果单独记载，不能以本节离线通过代替真实修复。

## v58：必需原生输入块的直接说明

live038 证明显式修复引用实际进入第二次 Python，但 SDK 在 ORCA 启动前拒绝缺少 `%method Z_MaxIter` 的输入，见[执行审计](validation/live038-pro-seeded-v5-execution.json)。旧 `required_blocks` 已列出该要求；真实 writer 请求中，method 字段被可逆 KEYS 编码为 P，原值只有 `Opt+Freq only: Z_MaxIter <bound> end`，块名和具体上限须结合其他字段解读。不能把此事描述为此前没有校验或契约。

按先记录的 0034 决定，v58 只把三条既有 required_blocks 说明改为完整 `%scf`、`%geom`、`%method` 语法、从冻结 NativeInputPolicy 读取的 1..N 数字范围及明确的适用条件，并加一句 SDK 执行前对照必需块的生成指引。原 `admit_profile` 函数字节 hash 不变，没有新字段、预检 Tool、自动补输入、任务算法、科学检查或预算改变。

[输入边界定向](validation/v58-native-guidance-targeted.json) **51P/0F**，包括真实 live038 输入继续拒绝、仅离线合成添加缺失块后通过原准入、动态非默认上限，以及原非法/歧义/无界输入反例。合成正控不修改历史 Run，不算模型修复或 ORCA 证据。采集 fixture 的两次 helper 错误在写入前失败，分别是 LF/CRLF 差异假设和 dict 访问错误；原字节与 hash 均保留，摘要如实记录，不算成生产测试失败。

[上下文定向](validation/v58-context-targeted.json)首批 **33P/1F**，唯一失败为遗漏更新的一处 v57 提示版本原句；只修正该期望为 v58 后原失败单项 **1P**。原生块完整字符串已在实际上下文 wire 中检验，代码 12k 的明确失败关闭、64k 完整展示和旧非代码上下文边界保留。首批所选 466 个源/测试/脚本文件起止 hash 无变化，相关 Ruff 通过。批次重叠不相加；没有全量回归、真实模型、ORCA 或开发预约。真实新候选结果须另记，不能据离线通过宣称步骤 3 完成。

[独立指导复核](validation/v58-independent-native-guidance-review.json)使用真实 live038 冻结源比较：只有 profile_contract 的三条指导 AST 改变，admit_profile 函数文本和 AST 均相同；非默认上限 97/43/19 对照成立。仅复用一次历史 writer 上下文，在既有紧缩下输入上界 57016 ≤ 64000，三个完整原生块字符串直接可见；权限、预算、usage、资源和 CONTROL 不变。旧上界为 58175，新表示采用已有 schema_columns 最后紧缩层，不能把差额归功于指导文本。首次审计包装缺 tests import path 的失败保留，修正 ignored 审计入口后重做同一个离线样本；没有新增 API、ORCA 或广泛回归。

## 用户要求收尾后的 Flash 配置与静态检查

按用户明确选择，`config.code.example.toml` 改为 `deepseek-flash`；本机 `config.local.toml` 显式固定同名模型，新的本地 `data/code-tools-step3/config.thinking-flash-v6.toml` 仅在模型名称上区别于历史 Pro-v5。历史 Pro-v5 文件 SHA-256 不变，旧 Run 的冻结配置不变。三个配置均通过 `load_config` 和 `prepare_request` 本地检查，实际构造的请求 body 指向 Flash；检查期间 socket 调用被禁止，没有发送网络请求。结果见[配置记录](validation/model-flash-v6-configuration.json)，编辑 helper 的换行错误和一次本机写入错误也如实保留，修正后仅重做本地配置读取检查。

最终 50 个改动 Python 文件 Ruff 通过，`git diff --check` 通过。没有再跑全量回归，也没有新增真实模型或 ORCA 测试。当前 Flash 配置的在线表现、最终候选的完整错误修复交付、归档瓶颈修复均未验证或未完成；不能用历史 live035 的 Pro 成功替代这些事实。用户要求本轮就此收尾，不进入步骤四，等待用户验收。

提交时为新增 `tests/fixtures/code_contracts/live*.json` 保留原始字节，避免 Git 自动换行转换改变已审计 fixture 的 SHA-256；既有验收证据目录沿用原 `-text` 规则。该变更只影响证据文件的 Git 存储，不改变运行源码、测试数据或历史 Run。
