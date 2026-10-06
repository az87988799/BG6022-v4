# B-10：组合可靠性与权限边界

本记录描述已执行的离线验证，不代表真实模型重复评测或阶段 B 已获验收。最近一次 unit/integration 全量运行为 1083 passed、1 skipped，耗时 466.76 秒；跳过项仅为 Windows 符号链接创建权限。后续修改的定向检查另记，不用该检查点替代新验证。

| 边界 | 验证事实与实现行为 | 可复查测试 |
| --- | --- | --- |
| 模型等待期间的新消息、暂停和取消 | 旧响应保留原依据；控制代次改变后不得激活旧提案 | `test_agent.py`、`test_model_usage.py` |
| HTTP 发送、响应保存与结算窗口 | 请求前持久预约；恢复两次不重发、不重复计费；缺响应保留未知占用 | `test_model_usage.py`、`test_phase_b_cross_recovery.py` |
| 分析反馈后的追加与修订窗口 | 反馈、Plan 激活和具体尝试分别绑定；两次恢复只保留一次预约和执行回调 | `test_phase_b_cross_recovery.py` |
| 历史条件与补收 | 旧 Attempt 使用启动快照；当前 Request 不能重标旧结果；R-03 已结算补收崩溃链继续回归 | `test_recovery_edges.py`、`test_phase_b_cross_recovery.py` |
| 科学预约与全局并发 | 环境被占用或前置校验失败时不创建批次票据；跨文件崩溃只有固定的未启动证明才能结算 actual=0，reserved 不退还 | `test_phase_b_prelaunch_budget.py` |
| 混合失败诊断 | SCF 字样不能覆盖资源、来源完整性、执行未知或后处理失败；仅已验证的 SCF 失败可建议限定参数修复 | `test_phase_b_cross_recovery.py` |
| 提案越权与注入 | 模型改许可、降规则、任意代码/路径、隐藏后处理均在 Tool 预约前拒绝；文件指令只作为数据 | `test_phase_b_reliability.py` |
| 模型预算耗尽 | 仍可重接已有证据和生成确定性报告；不得补写或伪造模型解释 | `test_agent.py`、`test_phase_b_reliability.py` |
| 尝试身份与追加额度 | 新 Step/logical ID 不能使耗尽的两次科学尝试变成第三次；初始采样 Plan 不能预置第四点 | `test_phase_b_cross_recovery.py` |

以上测试位于 `tests/unit/`，`test_recovery_edges.py` 位于 `tests/integration/`。模拟响应与受管测试子进程仅验证协议，不计 DeepSeek 或 ORCA 科学证据。默认离线测试禁止外部网络；`--live-model` 仅允许受限 DeepSeek HTTPS，真实联合计算另外要求 `--live-orca`。

模型纠正、传输重试、科学修复和批次额度各自有界。预算记录与不可变收据同时校验；缺少或冲突的证据不会恢复为零用量。执行身份未知时保守占用全局资源和预算，不按文件缺失推断可以重启。

正式三次离线故障重复的精确节点由 [coverage.json](coverage.json) 固定，必须以各次独立 JUnit 记录及对应源码冻结证明完成。该映射本身不是通过记录。真实联合轨迹、六轴解释复核和正式汇总另外交付；当前不宣称未运行槽已通过。
