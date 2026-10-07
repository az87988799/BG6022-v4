# 批次三：协议、逐目标交付和可复验材料

日期：2026-10-08（Asia/Hong_Kong）。本记录仅为离线实现验证，等待用户验收。

## 实施范围

- 空正文/纯空白截断在冻结配置下作为不可纠正失败停止，原始错误类别、正文、使用量及请求/响应 hash 保留；恢复不重复发送。部分截断正文不拼接，普通格式错误继续使用原有有限纠正。
- stop/clarify 参数来自本地类型，生成严格 schema、执行校验及安全纠正诊断；多余字段不忽略。新上下文版本为 `agent-json-v18`，历史收据不改。
- 仅 `explain_results=True` 的轨迹在发送边界为最终回答保留一次调用和决策机会，披露可用纠正和 token 容量。未来上下文大小不作最大值预约；实际输入和输出仍按原有字节上界校验，不能冒充 tokenizer 实测。最后一个合法 final-only 调用可发送，额度不足仍有确定性报告。
- 共用纯函数生成逐目标事实：当前条件、条件来源、历史实际条件、几何关系、合格值/原始观察/缺项、单位、Result/Attempt/Artifact 与检查版本。原始读观察不提升为科学资格，当前 charge 未知时不拿历史中性结果作答案，登记完成不等于科学目标完成。
- 先保留观察并共享重复表示，再缩短大型值；大值保留 hash、字节数和来源引用。紧上下文按当前未完成目标/Plan 选择完整 Tool schema，已有校验 Step 通过 ID 调用；工具能力、完整动作枚举与严格校验均保留。
- HTTP 证据、接受/拒绝提案、最终接受回答和协议交付状态分列。空正文事实/语义轴未验证；已知协议失败不能因没有接受提案变成无失败的缺证据。
- 测试协作身份 JSON 使用已有 `atomic_write`。半写窗口已先复现，坏的最终 JSON 继续报错。退出断言要求非空 PID/create_time 或确实创建的进程句柄；不把空记录列表当成退出证据。

## 可移植回归材料

`tests/fixtures/phase_b/bounded-protocol-failures.json` 明确标注为合成材料，包含 N06 仅登记、V07 最终 Markdown 而非单个 JSON 对象、V09 当前 charge 未知而历史来源为 0、v16 空/空白截断及部分截断形状。测试通过真实本地 Agent/Store/报告生产路径回放，但没有 HTTP 或 ORCA。没有真实派生字节，因此不杜撰原始/派生收据 hash。

## 验证记录

使用仓库 `.venv/Scripts/python.exe`，默认网络/科学执行禁用。各组存在重叠，不合并为独立测试总数。

| 验证组 | 结果与范围 |
| --- | --- |
| `test_bounded_protocol_delivery.py`、`test_worker_identity_publication.py` | 26 passed；包含严格动作参数形状、空截断两次恢复不重发、2/8 次原预算最终交付、预算发送前停止、Context/报告同一观察及 unknown 单位、N06 双目标只登记、V09 旧值不作当前答案、两张合成 HTTP 形状收据与 0 接受提案区分、半写/原子发布/坏最终 JSON |
| `test_agent_diagnostics.py`、`test_llm.py`、V07 多读完整路径 | 108 passed；包含完整/巨型/压缩/非法正文和纠正细节安全边界。纠正约束从相同 schema 派生 |
| 新上下文版本、joint 上下文六形态、action_budget、budget_fallback、call_tool_contract、structure 输入整链、report_extended | 最终代码 84 passed；原 12k 输入上限不变，原文、未知条件、成员和来源事实不丢；输入整链 4 次离线脚本回复、0 ORCA |
| 更广协议/上下文/报告/旧材料静态回放 | 一轮 400 passed / 4 failed；随后定向修复并重验全部 4 项：保留旧 Artifact count、保留线格式解码说明。旧历史文件未写入；完整干净检出仍待集成验证 |
| Windows 真实内核、受控测试 Python 子进程 | 一轮 54 passed / 1 failed；不是 ORCA。1 秒测试截止可能早于新原子发布 import 完成；用实际创建 PID/create_time 核对单进程退出，仍不释放未知生产配额。相关失败及非 ASCII 路径回归 2 passed |

原子发布测试先在旧发布方式上得到期望的失败：读取半个 JSON 会报 `JSONDecodeError`，文件直接可见而不是暂存后替换；改后原子观察和完整 JSON 校验通过。不重建后端，不改变生产取消判断。

新增字段投影规则：省略 False 的 `external_identity_queries`/`geometry_preparation`、0 的 `identity_queries`/`structure_preparations`、空 System.identity 和默认 registered 几何来源。测试明确确认被省略值确为默认，并完整比较旧字段；非默认值不得省略。

## 未验证与边界

本批没有真实模型、PubChem、OPI 几何生成或 ORCA 调用，没有真实模型成本、科学精度或模型解释质量证据。HTTP 评分测试验证已持久化形状和来源绑定，不提供远端真实性证明。大型历史归档依旧是显式静态回放，缺失时按既有分类跳过，新增关键失败 fixture 不依赖开发者盘符。完整干净检出全量回归与真实执行应由集成交付另行记录。
