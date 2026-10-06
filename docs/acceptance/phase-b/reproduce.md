# 正式评测的执行与复核

这些命令属于开发验收入口，不是模型可调用 Tool。运行前必须具备原验收 Store、同一批次账本及收据。新工作副本不能因缺少数据重新获得额度；缺归档时只记录未验证。

只有最终代码、测试、coverage 和运行 profile 提交后，才创建正式冻结。已存在冻结不得覆盖；需要修复时保留旧冻结、每条失败轨迹和成本，再以明确的新标签提交源码及冻结。`formal-v1` 是本例的初始标签，不能用新标签重置整批限制。

```powershell
.venv/Scripts/python.exe -m tests.helpers.phase_b_formal_ops freeze --label formal-v1 --execute
$env:ORCA_AGENT_EVAL_FREEZE = 'formal-v1'
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
$env:PYTHONIOENCODING = 'utf-8'
# 75 个真实模型槽；无科学许可，首次运行会发生真实模型费用。
.venv/Scripts/python.exe -m pytest tests/evals/test_model.py --live-model -q
# 18 条真实模型 + ORCA 轨迹，顺序执行，整个环境最多一个计算任务。
.venv/Scripts/python.exe -m pytest tests/evals/test_agent_e2e.py --live-model --live-orca -q
```

每个槽的身份持久化。再次执行同一正式槽不会新建 Run 或重新计算；中断须先查看原始状态，再显式恢复相同身份。真实模型节点若缺独立行为/六轴 review 会显示 skip/未验证；联合节点的通过只表示机械检查通过。人工或开发者独立复核需引用真实持久化模型原文，不能使用确定性报告补齐模型遗漏，也不能只检查 JSON 格式。

每次离线故障评测由 coverage 选择明确节点，生成独立 JUnit 与不可变 sidecar。每个 repetition 只执行一次；既有目录会拒绝覆盖。

```powershell
.venv/Scripts/python.exe -m tests.helpers.phase_b_formal_ops offline --label formal-v1 --repetition 1 --execute
.venv/Scripts/python.exe -m tests.helpers.phase_b_formal_ops offline --label formal-v1 --repetition 2 --execute
.venv/Scripts/python.exe -m tests.helpers.phase_b_formal_ops offline --label formal-v1 --repetition 3 --execute
.venv/Scripts/python.exe -m tests.helpers.phase_b_acceptance_report --output data/phase-b/evaluations/formal-v1-acceptance-01.json --offline-receipt data/phase-b/offline-evaluations/formal-v1/1/receipt.json --offline-receipt data/phase-b/offline-evaluations/formal-v1/2/receipt.json --offline-receipt data/phase-b/offline-evaluations/formal-v1/3/receipt.json
```

聚合命令只读既有证据并写新报告，不发模型请求、不启动 ORCA。它分别报告首次成功、纠正后成功、失败、未验证及未运行；开发轨迹不能填正式槽。账本成本只算一次，初始 SP 与修复成功场景复用同一正式轨迹时不会重复计费。

完整归档须包含 `data/phase-b/` 下的 reference、agent、agent-budget、receipts、batch-ledger.json、model-evaluations（含历史冻结）、evaluations、offline-evaluations，另保留对应源码提交和正式冻结。portable 导出只是有限文本子集，二进制省略项仅有 hash，不等于完整归档。当前数据包含绝对来源路径；复验需恢复原路径，本阶段未实现通用跨机器重定位。最终归档文件与 hash 以实际交付索引为准。
