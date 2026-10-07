# 正式评测的执行与复核

这些命令属于开发验收入口，不是模型可调用 Tool。运行前必须具备原验收 Store、同一批次账本及收据。新工作副本不能因缺少数据重新获得额度；缺归档时只记录未验证。

Windows 官方离线入口如下；`py -3.11 -m uv` 使用已安装的 uv 0.12.23，与工作流中的 `uv` 为同一工具。pytest 的仓库根导入约定仅由 `pyproject.toml` 的 `pythonpath = ["."]` 提供，无需手工设置 PYTHONPATH。

```powershell
py -3.11 -m uv sync --locked --group dev --python 3.11.4
py -3.11 -m uv run --offline --locked ruff check .
py -3.11 -m uv run --offline --locked pytest -q
```

独立参考 grader 使用 `tests/fixtures/phase_b/independent/reference-copies.json` 中明确的历史来源身份与 hash 映射，每次读取仍校验原字节。原 reference-review 不改写；小型独立水 SP、甲烷 Opt stdout/最终 XYZ 已有仓库副本。此映射仅服务静态独立评分，不迁移活跃 Store 或释放预算。

修复版新增 12 个原始文本变体，覆盖 N-01/N-02/N-03/N-04/N-05/N-06/N-07/N-09；调用 driver 不带参数可列出全部 37 个模型变体。这些输入通过真实 `initialize_bundle` 原始文本入口，不预填 goals、物理条件或 changes。`conditions.explain_results` 仅是显式交付配置；多轮继续只入队用户原文。以下命令会消费原共享账本的模型额度，科学许可和 ORCA 预算保持为零：

```powershell
.venv/Scripts/python.exe -m tests.helpers.phase_b_model_evaluation --variant N-01/raw-water-sp --category development --freeze-label repair-raw-v1 --repetition 1 --execute --live-model
```

每个新增 Run 的声明上限为 4 或 6 次 HTTP、32000 或 48000 tokens；同标签再次执行重用身份，中断显式 `--resume`。12 个新增变体三次重复至多增加 180 HTTP、1440000 tokens、保守 USD 1.08、0 ORCA，仍受原批次上限约束。原 25 变体与其原 case hash 保持，新增变体另绑定 `raw-text-cases.json`。当前完整可执行映射为 210 槽：111 模型、78 离线故障、21 联合变体槽（18 条实际联合轨迹）。既有甲烷 Opt 轨迹已改为 raw text 入口，同时检查优化后电子能；没有新增 ORCA 分配。映射不等于通过：真实开发闭环、累计执行预算核算及稳定版本全量门禁未完成前，正式冻结仍拒绝。

只有最终代码、测试、coverage 和运行 profile 提交后，才创建正式冻结。已存在冻结不得覆盖；需要修复时保留旧冻结、每条失败轨迹和成本，再以明确的新标签提交源码及冻结。`formal-v1` 是本例的初始标签，不能用新标签重置整批限制。

```powershell
# 先配置本机实际 ORCA/MPI 路径；配置不包含凭据。
$env:ORCA_AGENT_CONFIG = 'C:/operator/orca-config.toml'
.venv/Scripts/python.exe -m tests.helpers.phase_b_formal_ops freeze --label formal-v1 --with-science --execute
$env:ORCA_AGENT_EVAL_FREEZE = 'formal-v1'
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
$env:PYTHONIOENCODING = 'utf-8'
# 按最终冻结 coverage 的真实模型槽；无科学许可，首次运行会发生真实模型费用。
.venv/Scripts/python.exe -m pytest tests/evals/test_model.py --live-model -q
# 18 条真实模型 + ORCA 轨迹，顺序执行，整个环境最多一个计算任务。
.venv/Scripts/python.exe -m pytest tests/evals/test_agent_e2e.py --live-model --live-orca -q
```

每个槽的身份持久化。再次执行同一正式槽不会新建 Run 或重新计算；中断须先查看原始状态，再显式恢复相同身份。真实模型节点若缺独立行为/六轴 review 会显示 skip/未验证；联合节点的通过只表示机械检查通过。人工或开发者独立复核需引用真实持久化模型原文，不能使用确定性报告补齐模型遗漏，也不能只检查 JSON 格式。

已知断言、六轴、全提案事实/语义或必需最终协议失败统一记 failed；即使同时缺 review/fixture，也不能变成 skip。仅缺必要依据时才 unverified。允许的 timeout→成功分别保留失败收据、成功提案绑定与未知费用占用，不要求所有请求 usage 都为 known。

新执行冻结核对完整相关源码集合、实际导入来源、Python/关键依赖文件、prompt/model/token版本及有效配置。联合执行另核 doctor 的实际 ORCA/OPI/MS-MPI 版本和可执行文件 hash。冻结后只增无关文档无需强求 HEAD 相等。历史 `_freeze` 聚合或 `validate_freeze(label, execution=False)` 仍只读核对原清单，无需安装 ORCA；历史 v1 freeze 不能授权新正式执行。冻结文件不能覆盖。

每次离线故障评测由 coverage 选择明确节点，生成独立 JUnit 与不可变 sidecar。每个 repetition 只执行一次；既有目录会拒绝覆盖。

```powershell
.venv/Scripts/python.exe -m tests.helpers.phase_b_formal_ops offline --label formal-v1 --repetition 1 --execute
.venv/Scripts/python.exe -m tests.helpers.phase_b_formal_ops offline --label formal-v1 --repetition 2 --execute
.venv/Scripts/python.exe -m tests.helpers.phase_b_formal_ops offline --label formal-v1 --repetition 3 --execute
.venv/Scripts/python.exe -m tests.helpers.phase_b_acceptance_report --output data/phase-b/evaluations/formal-v1-acceptance-01.json --offline-receipt data/phase-b/offline-evaluations/formal-v1/1/receipt.json --offline-receipt data/phase-b/offline-evaluations/formal-v1/2/receipt.json --offline-receipt data/phase-b/offline-evaluations/formal-v1/3/receipt.json
```

聚合命令只读既有证据并写新报告，不发模型请求、不启动 ORCA。它分别报告首次成功、纠正后成功、失败、未验证及未运行；开发轨迹不能填正式槽。账本成本只算一次，初始 SP 与修复成功场景复用同一正式轨迹时不会重复计费。

完整归档须包含 `data/phase-b/` 下的 reference、agent、agent-budget、receipts、batch-ledger.json、model-evaluations（含历史冻结）、evaluations、offline-evaluations，另保留对应源码提交和正式冻结。portable 导出只是有限文本子集，二进制省略项仅有 hash，不等于完整归档。当前数据包含绝对来源路径；复验需恢复原路径，本阶段未实现通用跨机器重定位。最终归档文件与 hash 以实际交付索引为准。
