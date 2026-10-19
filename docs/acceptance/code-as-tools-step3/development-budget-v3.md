# 步骤 3 开发预算的第二次显式修订

2026-10-19（Asia/Hong_Kong），用户明确要求“继续完成步骤三，预算额度提升到 50 次”。本次解释为同一开发批次**累计最多 50 个 Run**，不是额外增加 50 个。原 8 个预约及其失败、实际消耗全部保留，因此迁移时最多还可预约 42 个新 Run。范围仍为步骤 3；不构成步骤 4—7 或正式跨任务验收授权。

**本轮最终状态：** 用户随后要求停止 NameError 专项及付费试跑，并明确“就先这样完成步骤三收尾”，不进入步骤四。预约停在 40/50，剩余 10 次不再自动使用；实际用量及未知项见[001—040 累计审计](validation/cumulative-budget-live001-040.json)。下文保留原预算迁移时点的记录，不构成继续消耗的许可。

历史 [独立核验方案](independent-review-plan.md) 的“不追加第二次扩容”描述保留为当时的约束；本次新增的用户决定授权此次前瞻修订，不回写旧 Run，也不将旧失败改记为成功。

| 额度 | 每个新 Run 上限 | v3 整批累计预约上限 |
| --- | ---: | ---: |
| Run 数量 | 1 | 50 |
| 模型调用 | 24 | 1200 |
| 模型 token | 1,200,000 | 60,000,000 |
| ORCA 启动 | 2 | 100 |
| 声明内部工作量 | 400 | 20,000 |

这些是启动前的最坏情况预约，不能当作实际用量。失败、提前停止、未知结果和未使用额度均不退回预约；另有历史独立原生探针 1 次 ORCA，继续单列。此次修订不改变每个 Run 的资源、期限、重试、许可及生产配置。

## 显式迁移

驱动提供独立管理命令；它只接受已有、有效且恰好保留 8 个预约的 v2 账本，不加载运行配置，不创建 Run，不调用模型或 ORCA。普通运行从不自动升级账本；重复迁移、缺少历史、损坏历史或超额配置均拒绝且不改账本。

```powershell
.venv/Scripts/python.exe scripts/code_tools_development.py extend-development-budget-to-fifty --ledger data/code-tools-step3/development-ledger.json --reason '2026-10-19 user explicitly authorized continuation of step 3 with 50 total development Runs; retain all eight prior reservations and original per-Run limits.'
```

v3 追加第二条修订记录：旧、新上限、时间、原因、迁移前 v2 文件原始 SHA-256、前 8 条预约的规范 JSON hash、原修订列表的规范 JSON hash。第一条 v1→v2 修订及其迁移前原始文件 hash 原样保留。校验递归检查 v2 的前 8 条和 v1 的前 5 条，使两次迁移历史仍能各按原契约核对。hash 用于审计，不是对有本机写权限者的防篡改签名。

实现前读取的本地 v2 文件 SHA-256 为 `ddabd7ebd3d4ff69fffe41aa452fac1978b7200751fa7f492a91fdb941ca1e4c`；其中第一条迁移保存的 v1 原始 SHA-256 为 `9bff25e9b3b3ab6f80e4cc762d8fc97f7a91ec1f6a619f4dfadd32c6d301a62d`。原始 v2 文件已另存为本地 `data/code-tools-step3/pre-v3-development-ledger.json`，其完整快照及原始字节重建规则保存在[迁移证据](validation/development-budget-v3.json)。这些值描述历史证据，不能证明新运行已启动或已经通过。

## 离线验证范围

预算测试覆盖历史 v1/v2 兼容、两条修订和原预约保留、50 次累计上限、逐 Run 上限、超额与损坏账本拒绝、重复迁移拒绝、CLI 不进入执行路径；故障种子驱动测试同时回归。此处测试只使用临时账本，不修改真实开发账本，不启动真实模型、ORCA 或容器。

实际定向回归：`tests/unit/test_development_budget.py` 与 `tests/unit/test_seeded_code_development.py` 共 **95 passed，17.66 s**，JUnit 保存在本地 `data/code-tools-step3/budget-v3-targeted-002.xml`；驱动及预算测试 Ruff 通过。第一次命令误用了不存在的故障测试文件名，pytest 以退出码 1 报告 `no tests ran`，修正为现有文件名后执行上述测试；这次收集错误不计为通过。

随后真实账本于 **2026-10-19 11:47:41.285467（Asia/Hong_Kong）**完成显式 v2→v3 迁移，原始 UTC 回执时间为 `2026-10-19T03:47:41.285467+00:00`。只读核对确认原 8 条预约和第一条修订未变，迁移时还可预约 42 个新 Run；第二条修订的原始 v2 hash、前 8 条预约 hash 和旧修订 hash 均匹配保存证据。迁移本身没有创建 Run 或启动模型、ORCA，不据此宣称真实任务通过。后续预约和实际消耗另行记录，不回写此迁移时点快照。
