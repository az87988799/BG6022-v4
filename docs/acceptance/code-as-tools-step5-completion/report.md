# 步骤 5 授权续作与交付

日期：2026-10-20（Asia/Hong_Kong）。**最终 Run `completed`、交付 `complete`、8/8 Goal 满足；本次交付包含明确披露的开发者元数据协助，等待用户验收。**

用户在[原六次开发记录](../code-as-tools-step5/report.md)之后明确要求继续完成步骤 5。此次继续原 live006：`run_4a5877907dbd4c7fbcfae9bdbd8f608e`，保留既有失败、初始结构、生成脚本及累计消耗。只实施步骤 5，不进入步骤 6/7 的正式重复验收。旧失败与新授权后的续作分别报告。

## 已取得的真实结果

已完成的模型 Python 修复脚本保留了 **12 个 C6H14 候选 XYZ**。Agent 按力场预排序在本 Run 的 4 次 ORCA 限额内评估了 4 个候选；另 **8 个候选因预算未做 ORCA**，结构仍保留，原因记录在 `failed_members.csv/.json`。这 8 项是预算排除，不能写成 ORCA 计算失败；尚未评估的候选没有 ORCA 能量。

4 项计算的实际条件一致：**B3LYP/6-31G、RKS、电荷 0、多重度 1、气相，ORCA 单点**。独立只读审计逐项读取原输入与 stdout，核对对应几何 hash、SCF 收敛、正常终止及能量。下表取原始结果，不由报告补算或猜测绝对能量；完整核对见[独立审计](validation/finalaudit.json)。

| 已评估候选 | 能量 / Eh | 相对该 4 项最低值 / kJ·mol⁻¹ |
| --- | ---: | ---: |
| candidate_08 | -236.845084754358 | 0.000000 |
| candidate_04 | -236.844111944460 | 2.554112 |
| candidate_09 | -236.844108490420 | 2.563181 |
| candidate_11 | -236.843077320772 | 5.270516 |

归档公式为 `ΔEᵢ(kJ/mol) = (Eᵢ − E_min) × 2625.4996394799`，其中 `E_min` 仅指这 **4 个已评估候选**的最低单点能量。完整精度及 eV 列保留在原 CSV/JSON；报告表格仅作显示舍入。candidate_08 是已评估子集中的最低项，不能据此比较另外 8 项或声称全局最低。

任务脚本记录的方法为 RDKit ETKDGv3 生成、MMFF94s 处理后预排序，再对选出的 4 项作 ORCA 单点。实际几何关系是**任务生成并经力场处理的候选几何上的单点**；初始结构独立保留，未被候选或修复覆盖。搜索、筛选和 4 次 ORCA 的任务代码来自产品 Agent，开发者未代写该科学算法或 ORCA 输入；后续确定性元数据修复有明确开发者协助，范围见下，不宣称全过程自主。

## 验证覆盖与搜索限制

保留 12 项结构和相应表格、4 项原始 ORCA 输入/stdout、实际运行 Python、初始结构及 Artifact/hash 来源；各观察仍按自身验证覆盖交付。通用观察与脚本执行成功不自动成为 `QualifiedOutput`，来源存在、数值可读、科学检查和目标满足分别判断。

- 脚本请求生成 24 个构象，实际返回并保留 12 个；没有记录每个未返回请求的内部筛除原因，不能把差额自动解释为失败、重复或 12 次独立尝试。
- MMFF94s 的优化收敛状态未逐项记录，未独立重算力场；原文 `All candidates distinct` 属于历史 `unverified` 观察，全部候选在 MMFF 后互异仍未得到充分证明，“12 份文件”不能作为“12 个已验证互异局部极小”的证据。
- 4 项进行了 ORCA 单点，没有进行 ORCA 几何优化或频率计算；不证明驻点、无虚频、零点能/热修正或自由能排名。搜索有限，未穷举，不证明全局最低。
- 原始结构、失败/预算排除与完整表格必须随结果交付；独立审计只读核对已有文件，不执行模型代码、ORCA 或力场来补造缺失证据。资源采样和每 Attempt 磁盘硬配额的既有局限仍保留。

## 修复、续作与冻结边界

[0040](../../decisions/0040-authorized-continuation-and-task-input-repair.md)允许显式更正任务输入副本 manifest。省略时继承原值，显式提交仍通过 Artifact 归属、hash、权限和可消费检查；科学条件、目的、依赖和最低证据保持。新的独立 Attempt 运行已生成的修复脚本，原失败、原代码和初始结构均不覆盖。预算修订通过可信用户控制入口持久化，模型和任务脚本没有扩额入口。

| 同一 Run 续作段 | 已发生事实 | 冻结与状态 |
| --- | --- | --- |
| continuation-001 | Python 修复及 4 项 ORCA 完成，8 项 Goal 中 2 项满足；随后大观察多次展开导致 `ContextLimitError`。 | `failed / partial`；运行期源码变化 `[]`。 |
| continuation-002 | 修正大值重复投影后，真实 execute 的控制反馈仍使上下文构造超限；没有新增 API/ORCA，模型调用仍累计 29。 | `failed / partial`；运行期源码变化 `[]`。离线大小检查不代替该真实失败。 |
| continuation-003 | 真实上下文构造及两次模型 API 完成，Run 累计 31 HTTP、4 ORCA；6 项报告观察缺显式原生条件来源，旧交付建议将 ORCA 零额度与这些缺口合并为部分停止，模型据此停止。 | `failed / partial`，2/8 Goal 满足；运行期源码变化 `[]`。 |
| continuation-004 | 0042 提供已有来源修复路径；模型归档的分析脚本却误写体系及 8 份 XYZ 保存事实，并未执行，不能取代原 12 候选和 4 项能量。 | `budget_exhausted / partial`；既有科学数据保持。 |
| continuation-005 | 按 0043 引入开发者协助，但 workspace.write 缺显式 Step 被 Tool 拒绝，尚未执行元数据脚本；失败 outcome 保留。 | 该段未完成交付，不记作修复通过。 |
| continuation-006 | 显式登记后元数据脚本执行完成，但 `original/` 输入与复制产物重复，归档收集超过 60 秒；仅注册 4 项观察、仍 2/8 Goal，失败 outcome 保留。 | 执行完成不等于归档或目标完成；运行期源码变化 `[]`。 |
| continuation-007 | 去重版本在代码登记阶段重复 decision_id，幂等机制复用旧 Plan，随后“Step 不在激活 Plan”检查拒绝执行；未新增 usage。 | 开发驱动错误与拒绝保留，不记作元数据执行通过。 |
| continuation-008 | 使用唯一 decision_id 并执行 v2；归档再次达到收集期限，3 项来源文件尚未归档，仅注册 4 项观察。 | 仍 2/8 Goal；失败及原始文件保留。 |
| continuation-009 | v3 只修 6 项缺口；脚本执行完成，归档时耗尽原 `artifact_count=200`，不是 30 秒收集超时。 | 未注册新观察；失败及占用不清零。 |
| continuation-010 | 0044 计数 400 已生效，v3 正常执行；宿主仍在 30 秒归档期限只登记 23 项（sealed 2、task 21），末尾 summary 未收集。 | 属归档失败，非科学计算失败；原 4 ORCA 不变。 |
| continuation-011 | v3 元数据完整收集，8/8 Goal 满足、`delivery_status=complete`；最终解释前，历史读取/代码登记累积的相关 Result 超过 32 项，上下文构造失败，未发送新 API。 | Run 为 `failed`；元数据交付完成与最终模型解释失败分别记录。累计仍 37 HTTP，运行期源码变化 `[]`。 |
| continuation-012 | 0046 的终态 Result 选择通过，但发送 HTTP 前交付快照超过旧 256 KiB 保存上限而被拒绝；未新增 API、Python 或 ORCA。 | Run `failed`、交付 `complete`、8/8 Goal；累计仍 37 HTTP，运行期源码变化 `[]`。 |
| continuation-013 | 仅恢复最终解释，完成 1 次真实模型 API；未新增 Python、元数据执行或 ORCA。原 Run 最终累计 38 HTTP、854,725 已知 token、4 ORCA。 | `completed / complete`、8/8 Goal 满足、error=null、运行期源码变化 `[]`。 |

有界反馈决定见[0041](../../decisions/0041-bounded-task-observation-feedback.md)，已有证据来源修复见[0042](../../decisions/0042-recover-evidence-without-new-calculation.md)。ORCA 零余额仍禁止新计算；有权限及分析余额时，可通过新的显式分析 Step 补充已完成回执的来源声明，原观察和数值不覆盖。原来源、hash、条件、最低证据及科学检查门槛保持；恢复建议本身不赋予权限或判定通过。

各段保存各自 freeze、before Run、outcome 和源码 hash，原失败不改判；不同冻结版本的计算与交付证据通过同 Run 的历史引用衔接，不能拼接成某一冻结版本首次通过。[最终成本审计](validation/costs.json)同时核对 13 段源码冻结：最新第 013 段运行期间和归档时的当前源码均无变化；早期段与后续源码的差异分别记录。

[0046](../../decisions/0046-bounded-terminal-result-selection.md)只约束最终解释的相关 Result 选择，使最终上下文保留当前交付所需证据；历史读取、代码登记、失败和全部原始文件继续在 Run 中保留，不因上下文选择而删除或改判。第 013 段另行完成了真实模型解释和原终态检查，未用 8/8 Goal 或 `delivery_status=complete` 代替它。

[0047](../../decisions/0047-bounded-delivery-snapshot-storage.md)只统一本地交付快照保存与读取的有限 2 MiB 上限；模型上下文仍为原 64K、模型输出/tokens/API 与执行资源限额不变。先前 256 KiB 拒绝不改判，较大的本地快照不等于扩大模型请求。

[0043](../../decisions/0043-developer-assisted-metadata-delivery.md)是本次明确披露的开发者协助：原样复制初始/候选结构、表格和 summary，保留 6 项观察的 value、unit、formula、结构及本地来源，追加原 manifest 已有的 4 个原生回执来源，并补充真实验证限制。v3 的新派生产物列表省去 4 个冗余 stdout 别名；这些原始 stdout 仍由原科学 Attempt 和 native 回执提供下载，不宣称新旧 `artifacts` 数组完全相同。原能量排名继续取原科学 Result。

它不重算或重排能量，不重建候选，不新做 ORCA，不修改旧 Artifact/Result。新代码、完整输入 manifest 和显式 Plan 仍通过 workspace.write、python.execute、隔离、hash、来源、条件和最低证据检查；元数据完整收集后，由一次真实模型调用完成最终解释。此开发者协助不作为无人干预或步骤 6/7 重复验收证据。

## 预算、成本与必要验证

原 live006 的 **19 HTTP、395,000 已知 token、0 ORCA** 保留。不可变修订 `continuation_0040_budget` 将累计上限设为 **44 HTTP、2,000,000 token、80 决策轮、24 次证据读取、8 次分析执行、4 次代码重试、每逻辑 Step 4 次尝试**；ORCA 总上限仍 4、额外上限仍 3，原许可、4 核/1024 MB 边界、模型输出上限 32,768 均保持。新截止时间为 **2026-10-19 16:56:30.944052 UTC**；原 `budget.json`、旧 Run before 原字节及授权/hash 独立保留，usage 不清零。

[0044](../../decisions/0044-explicit-archive-budget-amendment.md)进一步明确将归档计数 **200 → 400**、代码重试 **4 → 6**，原绝对截止时间增加 10 分钟至 **2026-10-19 17:06:30.944052 UTC**。0040 原修订链和全部已用/仅预约 Artifact 均保留，不删除旧预约来腾额度；其余上限不变，尤其模型调用仍 44、token 仍 2,000,000、ORCA 仍 4。新增尝试继续计量，许可、资源与科学检查不变。

[0045](../../decisions/0045-authorized-host-archive-deadline.md)再通过可信授权指定**宿主归档登记期限 300 秒**，绝对截止时间增加 5 分钟至 **2026-10-19 17:11:30.944052 UTC**。它是授权元数据，不是修改 `BudgetLimits` 中的计算/模型限额或冻结配置；原容器文件收集 30 秒、CPU/内存、并发及 ORCA 额度均保持。未授权 Run 沿用原时限，旧不完整收集不自动补判，全部失败和占用继续保留。

0047 的独立可信修订仅将绝对截止时间再延 10 分钟至 **2026-10-19 17:21:30.944052 UTC**，`BudgetLimits` 中只累计 `run_seconds` 随之变化，宿主归档 300 秒沿原修订链继承。0040/0044/0045/0047 的不可变授权和 before 快照全部保留，原用量不清零。

旧六个唯一 Run 已交付基线为 **71 HTTP、1,312,336 已知 token、0 未知占用 token、0 ORCA**。[最终唯一总账](validation/costs.json)为 **90 HTTP、1,772,061 已知 token、0 未知占用 token、4 项实际/预留 ORCA**；相对旧基线实际增量 **19 HTTP、459,725 token、4 ORCA**。最终 live006 自身累计 38 HTTP、854,725 token，其旧快照和 13 段 outcome 均未重复相加。账本与去重模型 accounting 一致，全部原始失败和预约仍保留。

冻结价格估算费用上界合计 **USD 0.9317052**、续作增量 **USD 0.2251716**；没有未结算模型回执，提供商实际扣费未查询。Python Attempt 共 9 次，记录/有效追加代码尝试均 6 次；4 项 ORCA 均完成，实际失败 0。CPU 下界约 **119.695343 s**、计算耗时下界约 **176.138 s**，资源采样不完整。本批限额 96 HTTP、4,400,000 token、12 ORCA，剩余 6 HTTP、2,627,939 token、8 项批次 ORCA 额度；当前 Run 的 4 项 ORCA 限额已用完。成本 JSON SHA256：`b4c73e53befabf775c6ae1a9ee74a7b2e64baf467a9109445fd7f1ef5b813d3d`。

未跑全量回归。 本次提交使用 `[skip ci]`，避免推送自动触发仓库既有全量 pytest 流水线；定向验证与 API 收据随本次提交保留。预算修订 **5 项新定向验证和 2 项既有严格预算反例通过**，保留[实际开发工具输出摘录](validation/budget-amendment-tests.json)，没有补造 JUnit，也未为存档重跑。其余输入修复、真实失败派生上下文、来源修复、归档预算/时限、终态 Result 选择和快照边界的必要测试见[验证记录](validation/README.md)；早期失败保留，重叠批次不相加为唯一数量。

[生产 API 核对](validation/api.json)通过状态与报告 GET 200，并对 **27 个按 SHA 去重的实际 Artifact 下载**逐响应核对原字节和 header SHA。覆盖原科学 Attempt、最新元数据、初始结构及实际代码；Run 原字节未变，新增模型/ORCA 均为 0，没有为了验证重复下载全部历史拷贝。

## 交付与退出条件

[下载结果包](delivery/hexane-results.zip)已生成，**1,023,174 字节、41 个条目**。包含初始 XYZ、原科学成功 Attempt 的 12 个候选、3 份 CSV 及对应 JSON/summary、4 组实际 ORCA 输入/stdout、实际运行的模型 Python、明确标记的开发者元数据代码、独立审计及 Artifact/hash 清单。未执行分析脚本的错误说法未改变库存或数值。逐个 ZIP 条目与原字节比较通过，不改科学值；GBW 等大二进制留在本地。[manifest](delivery/hexane-results.manifest.json)和[打包收据](delivery/hexane-results.receipt.json)记录来源与验证，ZIP SHA256：`6e24e28c5d9ebd75a0c02ed27ba5583ffc9d58703a4f85d4ab9aaba329dbed80`。

[最终独立审计](validation/finalaudit.json)核对 **210 条 Artifact/hash 来源、47 条控制文件 hash、6 项元数据修复及 20 个原字节文件**，确认 4 项原始能量、最终 Goal 绑定及完整交付一致；未执行模型代码、力场或 ORCA。审计 SHA256：`ef3aaa46d0081375dbfb81094b1050de3b2417510368875da443522e8f9851a0`。任务 stdout 别名与 native 原始 stdout 的字节存在换行差异，逐行内容一致，分别记录各自 hash；包内科学原始 stdout 使用 **native 回执指向的原字节和 SHA**，不宣称两者字节相同。

原始未执行修复、历史失败、各次显式授权和后续纠正均保留，来源与冻结见[证据索引](evidence/index.json)。本次完成真实有限搜索结果、完整产物及最终模型解释的交付，开发者元数据协助和科学未验证项已经披露；不作为步骤 6/7 或无人干预同版本重复验收。提交及远端核验由最终交付回复列明。**等待用户验收；用户接受后才记为验收通过。**
