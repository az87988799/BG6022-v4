# Code as Tools 步骤 4 交付

日期：2026-10-19（Asia/Hong_Kong）。基线：`620e6aa`。状态：**实现与定向验证已交付，等待用户验收**。仅实施[方案步骤 4](../../CODE-AS-TOOLS-IMPLEMENTATION-PLAN.md)，不进入步骤 5—7；未运行全量回归、未调用真实模型。

## 本次结果

动态 Python 可经当前 Run 的唯一协调者顺序发起多次 ORCA，在同一容器/cgroup 和环境租约内运行。每次调用保留独立输入、目录、启动门、进程身份、目标/条件、资源、工作量及原始产物。SDK v2 的 1 MiB 请求/响应及每 Attempt 1024 次交换上界由代码和工具定义公开；检查点另限制单状态 64 KiB、256 条及累计 1 MiB。

稳定键及内容指纹相同的完成回执可在本次或后续 Python Attempt 复用：重新核对原始 Artifact hash 并提供只读副本，不重新启动或扣计算额度。同键内容变化拒绝；未结算/未知先对账。新候选、计划追加、失败重试分别记录；失败重试使用新键并关联原失败，所有 Attempt 共用累计预算。原生扫描/数值频率等内部工作量继续由原准入链核算。

`checkpoint(state, request_key='batch_1', requires_decision=True, reason='...')` 保存状态、代码 hash、来源 Attempt 和完成调用键，正常结束 Python，将结果返回原有目标检查/计划影响判断。即使代码捕获 SDK 退出，也不能在该 Attempt 继续提交 ORCA。检查点不是调用栈快照或科学资格。跨 Attempt 引用保留原始 Artifact/Attempt 与几何和条件绑定，不重绑历史证据。

恢复逐调用核对确切容器/进程身份，收集其原目录，不重启 Python/ORCA。缺启动身份继续未知并占用额度；没有可信执行完成回执时，即使原始输出存在，也不猜测成功。已写检查点文件但尚未发布 Run 引用的故障窗口可恢复。服务重启不自动继续运行。

## 实际验证

| 证据 | 实际结果与边界 |
| --- | --- |
| SDK、managed 执行、容器字节协议定向 | 51 passed；离线，不代表真实隔离或科学通过 |
| Store/共享启动预算及双 Run 竞争 | 56 passed；补充检查点/回执 4 passed，存在重叠，不相加冒充唯一用例数 |
| 多调用 Broker/未知父 Attempt/逐目录收集 | 5 passed；覆盖同键变更拒绝、复用不追加、检查点阻断及逐目录收集守卫 |
| 反馈、恢复与来源 | 首批 12 passed，新增孤立检查点 2 passed；包含两种观察触发“停止/新增步骤”的离线配对、重复 resume 不重放、跨 Attempt 来源及条件反例 |
| 本次全部变更 Python 静态检查 | Ruff 通过 |
| 真实正常路径 | 1 passed：动态脚本完成两次真实四核 ORCA，同键复用第一项不新增启动，持久化决策检查点后退出 |
| 真实取消 | 1 passed：第二项开始后取消，同一受管边界全部终止，第一项原始证据不变，两次恢复不重启 |
| 真实协调者崩溃 | 首次恢复失败：恢复结算前的未知成本更新未保存，触发 stale Run 守卫。修复后直接恢复原 Run 两次通过，新增 ORCA/模型/容器均为 0；首次失败 JUnit 保留，未重新跑科学任务 |

真实初次 pytest 合计明确为 **2 passed、1 failed**；原崩溃 Run 的修后恢复审计通过。不能写成三个原始用例首次全通过。这些测试直调生产 `_science`/`_recover`，证明 Attempt/内部计算边界；夹具 Run 顶层仍为 `running`，不代表自然语言产品端 Run 已完成。

正常样例两项原始能量独立从 ORCA stdout 提取为 `-74.962991615317` 和 `-74.963399828991 Eh`，并与检查点一致；两项均有正常终止与四进程事实。它们是开发者编写的 SDK 执行证据，不是模型自主选择、科学资格或跨任务正式验收。Result 不发布 `QualifiedOutput`。

## 成本、配置和未验证项

本批 3 个 Run 共 **6 次实际 ORCA 启动、6 个保留启动与工作量、0 次模型调用**。复用与修后恢复未新增计算。已知聚合 CPU 下界 `23.987714 s`、Attempt 耗时下界之和 `63.219 s`；崩溃期间样本缺失，记录的 0 是未知成本下界，不是物理成本为零。独立收集成本仍不完整。

固定新镜像 `sha256:6641236074aa40af56505cfb92c6a8e0c2cc6b45f1407eccf39df640e5ec6e08`，manifest `40ec1ba664f9abb7200ade88e161c2156e031cb7c6e0c5c3b4ac44ae646f4c10`，完全使用既有离线缓存构建。真实验证采用 4 核、总内存 1024 MiB、MaxCore 192 MiB/进程；Python 与内部 ORCA 共用该聚合边界，不另申请第二份资源或并发槽。新镜像已写入容器示例和忽略提交的本地配置，并以只读 inspect 核对；模型配置未变，历史 Run/镜像未迁移。

环境并发仍限定 1，大于 1 明确拒绝；双 Run 竞争测试不能据此声称实现并行资源分配。Windows 本轮只覆盖离线协议和目录归属，未新增真实多计算验证。新镜像没有重跑完整隔离攻击矩阵；原 seccomp/UID/cgroup 边界未放宽，历史边界证据保留，不能当作本镜像完整重验。尚无每 Attempt 磁盘硬配额。

观察驱动的后续分支本次只有离线主循环配对，**真实模型的自适应规划可靠性未验证**。步骤 3 的 NameError 受控修复和 live040 收集超时既有失败不在本次扩展修复范围，不改判通过；本批小任务成功也不证明该超时已解决。未实施己烷交付、界面扩展或正式跨任务验收。

## 证据索引

- [真实正常路径](evidence/batches/live-normal-001/index.json)、[取消/崩溃及原始失败 JUnit](evidence/batches/live-interruption-001/index.json)、[原 Run 修后恢复审计](evidence/batches/recovery-audit-001/index.json)：每个来源文件有 hash，导出的文本按内容寻址；二进制仅索引，不提交。
- [实际运行与成本摘要](validation/real-evidence-summary.json)、[真实命令](validation/validation-commands.json)、[Store/Broker 定向记录](validation/store-broker-tests.txt)、[离线验证记录](validation/offline-tests.json)。
- [镜像构建回执](validation/runtime-build.json)、[新 Run 配置核对](validation/new-run-configuration.json)、[决定 0036](../../decisions/0036-multiple-computations-and-checkpoints.md)。原始大产物保留在本地 `data/code-as-tools-step4/`。

证据整理复用了既有只读导出工具；导出时逐源 hash 验证通过，未启动工作负载。提交排除原工作区已存在的无关未跟踪 `docs/acceptance/repair-cycle-development-4/`。等待用户验收。
