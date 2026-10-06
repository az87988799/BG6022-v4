# R-03：已结算历史尝试再次崩溃后的引用恢复

日期：2026-10-06。审查基线：`856cada`。状态：等待用户验收；阶段 B 尚未开始。

用户复核发现，上次 R-03 的“历史补收”和“保存补收 Result 后崩溃”分别通过，但两者组合仍失败：尝试已有 `finished_at`、租约已释放，新的补收 Result 已保存而 Run 仍引用旧 unknown Result，后续 resume 被提前跳过，随后报 `unbound replacement result requires explicit resume`。上次报告的 R-03 关闭结论因此不足，本记录补充该组合边界。

生产修复只调整 `runner._recover()` 一处提前跳过条件：已结算、无需收集、无租约的尝试，还须已绑定合法结果链末端才能跳过。未绑定末端继续走现有引用更新分支；Result 链和 Artifact hash 仍先校验，历史尝试不调用结算函数。Store、模型、科学检查、输入生成和原生后端均未改动。

现有崩溃回归参数化为“未结算 / 已结算”两种状态。新增已结算分支先释放租约，再在保存补收 Result 后注入崩溃，连续两次 resume 验证同一末端接回、完整历史引用保留、无需再收集或结算、原完成时间和完整用量保持、执行回执与两份 Result 原字节不变、无合格科学输出和新启动。未修复代码为 **1 failed / 1 passed**，失败与用户报告一致；修复后恢复测试文件 **20 passed**。

完整离线验证、日志 hash 与源码 hash 见 [验证记录](r03-followup-validation.json)。本轮仅采用离线故障注入，没有新 ORCA 执行、模型调用或远程计算，也未重新运行历史科学回放；既有真实证据仍按原范围引用。本修复落实 [决定 0003](../../decisions/0003-check-version-and-recovery-results.md) 已规定的恢复顺序，没有新增对象关系或科学保证。

最终本地完整回归为 **304 passed、14 skipped、0 failed**（203.30 秒），比基线新增 1 个组合用例。13 个真实 ORCA 测试未开启，1 个符号链接测试因本机 Windows 权限跳过；Ruff 通过。1500 份历史文件核对与上次交付一致。远端 CI 在推送后独立核验，本地通过不代替远端结论。

复现命令：

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_recovery_edges.py -q
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check .
```
