# 自然语言规划接口修复

2026-10-09。用户要求参考本机 BG6022-v3 与 GitHub 成熟 Agent，解决自然语言规划，不能以预设代替。

参考：v3 src/bg6022/agent.py 调用 plan_builder.build_plan；planner.py 从用户原文提取语义，llm.py 传递原生 JSON Schema 并有界反馈校验错误。v3 的 Requirement 等领域对象和运行协议不迁入 v4。

GitHub 对照：
- https://github.com/huggingface/smolagents/blob/main/src/smolagents/prompts/toolcalling_agent.yaml：明确工具及参数，执行后观察反馈。
- https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py：模型选择工具，工具结果回到同一循环；该旧 factory 已标记迁移到 langchain.agents。
- https://github.com/pydantic/pydantic-ai/blob/main/docs/tools-advanced.md：参数 schema、验证反馈与有限重试。

采用原生、按当前动作裁剪的模型接口；取消简单意图/规划场景里的字符串池和 schema 列编码。仍使用既有 Proposal/SemanticCandidate/ProposedPlan 类型作为唯一校验来源，不新增运行时领域对象或第二执行循环。模型选择工具、目标映射和依赖；程序按 Tool 参数声明从当前确认条件补全缺省参数、生成 ID 和依赖。冲突、未知条件仍交给现有校验，不自动改变科学目标或许可。大型历史分析上下文仍保持原有有界投影能力。

本轮真实验证最多 16 次模型调用、96000 tokens、2 次身份查询、2 次 OPI 准备、2 次 ORCA；每个 Run 沿现行配置最多 8 次/48000 tokens/900 秒/1 次 ORCA。仅水或甲烷气相 RHF/STO-3G 中性单重态，4 核/1024 MB/MaxCore 192 MB。所有失败和前轮成本保留，不恢复或重置旧 Run。退出条件是自由文本入口经真实模型产生计划，真实 OPI/ORCA 完成并交付合格结果；局部样例不等于任意自然语言覆盖。

最终接口为 `decision-intent-1`：模型仅填写 action/parameters/reason，程序依据该次实际发送的冻结 AUTHORITY 补齐 Proposal.basis 和 related_results，再走原有过期、许可和提交检查。DecisionIntent 是短期传输校验类型，不是新增领域生命周期。Tool 参数只继承声明字段，显式值优先并接受原有冲突校验；inputs.geometry 归一到既有 geometry 绑定，重复指定拒绝。相关 schema 从既有定义投影，不新增另一套工具协议。

替代方案是继续强化压缩编码提示，或接入新的 Agent 框架。前者已有真实误解，后者增加迁移范围且不能解决本地接口错配，均不采用。详细文本证据在登记后以不可变 Request 引用保留，确认值与来源仍进入模型上下文；原始证据不覆盖。

实际完成：普通文本水单点运行 5 次模型调用、0 次纠正、1 次 OPI、1 次 ORCA，已完成科学检查和最终解释。本轮含失败合计 14 次调用、46167 tokens、2 次身份查询、1 次准备、1 次 ORCA。成功 Run 模型额度收窄为 7 次以遵守累计上限。结果、费用及有限验收范围见[真实记录](../acceptance/natural-planning-repair/README.md)，等待用户验收。
