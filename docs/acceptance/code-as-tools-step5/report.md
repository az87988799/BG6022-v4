# Code as Tools 步骤 5 交付

> 本页保留原六次开发 Run 的历史交付时点；后续输入绑定修复、真实 ORCA 与最终交付见[步骤 5 授权续作报告](../code-as-tools-step5-completion/report.md)。历史失败与成本不改判。

日期：2026-10-19（Asia/Hong_Kong）。基线：`87d1e626fd2568362a598d0aff47d0f6fe943142`。**实现与定向验证交付；真实己烷退出条件未达到，等待用户验收。**

本次仅实施[方案步骤 5](../../CODE-AS-TOOLS-IMPLEMENTATION-PLAN.md)的通用分析、可追溯交付与现有界面改进，未实施步骤 6/7 正式冻结及跨任务重复验收。六次有限开发 Run 均未完成真实 ORCA 能量排名与完整交付；不能声称步骤 5 整体完成或验收通过。修复前后分别保存源码快照，不拼接不同候选或把续跑改称首次成功。

## 已实施范围

受管 Python 可解析原始文件、计算派生量，保存公式、单位、脚本 hash、逐源位置/hash/实际条件和派生产物。不同候选几何逐源保留；方法或阶段冲突形成失败检查；单项声明错误不清空其他有效观察。通用观察始终不因来源存在、格式正确或脚本 `passed` 获得 `QualifiedOutput`，明确科学最低证据仍单独判断。

报告逐输出呈现验证覆盖，区分执行、科学资格、目标满足和报告状态，索引初始结构、各次脚本/输入、原始输出及派生产物，保留失败、排除原因、结构关系和搜索范围。预算耗尽或模型报告失败仍提供确定性既有结果及停止原因。Web 显示实际动作、脚本、计算状态、验证说明和经授权/hash 核对的下载；查看与下载不暗中执行分析或计算。

一般目标不再因与旧采样端口重名进入旧门槛；显式分析引用、已确认阈值和用户明确条件继续保留。非法最低证据引用返回精确路径和原文。已授权自主选择常规步骤的一般任务不再被旧 SP/Opt 提示阻断。新增[0038](../../decisions/0038-incremental-plan-revisions.md)允许确定性沿用已结算步骤、依赖闭包与未改目标绑定；未知引用、冻结改写和未结算隐式执行仍拒绝。[0039](../../decisions/0039-preserving-scope-continuation.md)以有限语法零 API 消化保持原范围的续作消息，保留 Request、Plan、最低证据、权限、累计预算及原 deadline。

SDK 拒绝现从 host 响应反馈到 Result：最多扫描 32 个响应、保留 8 项、每项 detail 最多 2 KiB；归档脱敏 error 投影及原响应 hash，原 raw 不变，非 error payload 不进入新增诊断。它只补足失败可见性，不赋予权限或科学资格。未新增构象专用 Tool、生产解析器或科学求解算法，决定见[0037](../../decisions/0037-traceable-generic-analysis-delivery.md)。

## 有限真实开发记录

| Run | 状态 / 交付 | HTTP / 已知 token | 实际 ORCA | 结果与失败边界 |
| --- | --- | --- | --- | --- |
| live001 | failed / partial | 3 / 31,229 | 0 | 旧采样分派拒绝一般己烷目标，原回复作为离线反例保留。 |
| live002 | waiting_user / partial | 2 / 23,251 | 0 | 旧 SP/Opt 提示引发不必要澄清；旧 Run 未改判。 |
| live003 | failed / partial | 10 / 138,371 | 0 | 中文名称与英文查询别名的来源匹配未完成。 |
| live004 | failed / partial | 17 / 334,956 | 0 | 保留 18 个 C6H14 候选；4 项 SDK 请求因绝对依赖目标路径拒绝，修复回复截断。 |
| live005，含同 Run 续跑 | failed / partial | 20 / 389,529 | 0 | 身份和初始结构满足；进入 Python 后 4 项请求均缺显式 RKS，24,576 token 修复回复截断。 |
| live006 | budget_exhausted / partial | 19 / 395,000 | 0 | 首 Attempt 因 SDK manifest 缺少 `coords.xyz` 依赖而失败；`repair_v2` 已归档未执行，最终解释无剩余模型预算。 |

live005 初次为 12 HTTP、213,423 token、0 Python/ORCA，已归档的任务代码在增量计划结构协议处受阻。第一次同 Run 续跑新增 3 HTTP，被严格控制句白名单拒绝；第二次零 API 消化原 pending 消息后，仍因既有纠正耗尽停止；第三次新精确“继续”后进入 Attempt。脚本生成 13 个候选但只保存 4 份候选 XYZ，全部 SDK 请求被拒绝；脚本捕获异常却未保存具体错误，`sources=[]` 观察无法注册，模型经多次查询仍缺可见拒绝原因。原状态、失败回复和各次续跑快照保留，累计成本不重复相加、不清零。

live006 的新修复代码未进入第二次 Python。最后计划被增量补全同时加入原失败 Step 和同一逻辑身份的修复 Step，触发重复逻辑身份拒绝（`ValidationError loc=[]`）；随后最终解释请求因预算不足停止。这是增量沿用与代码修复的交互缺陷，不能归为模型缺少科学能力。运行期间 `source_changed_during_run=[]`。身份/初始结构满足不代表搜索、能量排名或报告目标完成，未执行修复不计作修复通过。

006 结束后，另做 0038 最小修复，避免自动携带与本次修复 Step 同逻辑身份的原失败 Step；显式旧项、遗漏新绑定、未知和未结算修复仍拒绝。typed `repair_of` 及等价完整形式的非代码合同保持冻结。真实失败提案派生的最终 [13 项定向验证](validation/post-run-plan-repair-tests.xml)通过（含既有 6 项），未再调用 API/ORCA，不改判 006。该补丁只修 Plan 结构，不证明缺失输入 manifest 或后续脚本执行已解决。`source_changed_during_run=[]` 只覆盖运行窗口，后置补丁不属于该真实运行的冻结源码，不能据此宣称最终全代码已有真实成功证据。

live004 测试配置曾误设 `extra_orca_starts=0`、总上限 4；live005 改为追加 3、总上限仍 4，不归罪产品或改判旧失败。真实截断后，新 Run 输出上限依次从 **16,384 → 24,576 → 32,768** 调整。live006 的 v65 仅明确显式 RHF/RKS、相对 SDK 依赖、保留拒绝诊断和全候选等通用契约，未注入科学算法、Plan、ORCA 输入或任务脚本。整批仍限 **72 HTTP、360 万 token、12 次 ORCA**。

## 独立核对与限制

[live004 只读预审](validation/independent-preliminary-004.json)重算归档 hash，独立读取 SDF/CSV/XYZ，确认 18 项为 20 原子 C6H14、有限坐标且单键图与归档身份一致；4 项选中结构与表格对应，初始结构 hash 不变。表格保留 14 项未选中和 4 项失败及原因，ORCA 能量为 0 项。生成文字中的“已用 ORCA 评估四项”和“初始结构未找到”，分别与零启动、归档初始结构仍存在冲突，不能作为执行事实。

该预审未调用 API、执行生成脚本、重算力场或 ORCA，不证明 MMFF94 能量、搜索完整性、局部极小或全局最低。live006 没有实际 ORCA 输出，无法验证能量、收敛、排名或差值；全候选保留与完整交付亦未达到。已有初始结构、生成脚本、SDK/执行诊断和未执行修复作为部分证据保留；来源追踪不独立证明生成解析或科学解释正确。

[live005 最终只读审计](validation/independent-final-005.json)和 [live006 最终只读审计](validation/independent-final-006.json)分别复核 15/6 项来源 hash 及确定性报告绑定，均明确 `exit_condition_met=false`。它们没有修改 Run、调用模型、执行任务代码或重算科学结果。

## 必要定向验证

未运行全量回归。批次有重叠，不相加为唯一用例数；详见[测试记录](validation/test-records.json)。

| 范围 | 实际记录 |
| --- | --- |
| 来源/历史/强制证据、报告/终态 | 7 项来源与 5 项选定防伪反例通过；最终报告模块 29 通过，较早 73/53 批次重叠。 |
| Web、下载与原生准入 | Web 3 项、内部下载 2 项通过；原生输入/资源 61 通过；Node 渲染及 JS 语法检查通过。 |
| 上下文、身份及目标分派 | 初批 49 项中 5 项失败保留，修正后上下文 33 通过；身份 2 通过。目标/旧采样首批 36 通过，最终 6+1 项有重叠。v64 初批 2 项中 1 项旧文案断言失败，修正后该项通过，原 JUnit 保留。 |
| 后续精确反例 | 0038 增量计划 6 通过；0039 控制续作 7 通过；SDK 错误可见性/有界脱敏 2 通过；v65 当前 SDK 契约指引 2 通过。变更 Python Ruff 通过。 |

## 成本与交付

按[只读累计账本](validation/cumulative-costs-final.json)核对六个唯一 Run：**71 HTTP、1,312,336 个已知 token、0 个未知占用 token；实际/保留 ORCA 均 0，Python Attempt 共 3 次，记录的代码重试和内部工作量均 0**。每个 Run 的计数与模型 accounting 一致；live005 的原 outcome 和三次 continuation 只按 Store 最终累计值计一次。已知 token 按冻结价格估算费用上界为 **USD 0.7065336**，无未结算模型回执；提供商实际扣费未查询。计算耗时下界约 `21.562 s`、CPU 下界 `7.852469 s`，资源采样不完整。

批次限额下尚余 **1 HTTP、2,287,664 token、12 次 ORCA**，六个 Run 名额已用完；本轮不再调用 API 或 ORCA。汇总只读取最终 Run/model 账本并记录逐源 hash，未修改 Run；JSON SHA256 为 `4265516958a1635ee6426f49940348350216fdb095c74c91670c7acb0807aade`。本地原始记录位于 `data/code-as-tools-step5/live-001/`—`live-006/`、`live-005-continuation*/` 和 `live-store/`。

可移植证据见 [Run/模型/Artifact 索引](evidence/batches/live-store/index.json)、[live006 冻结与结果](evidence/batches/live-006/index.json)、[原 Run 三次续作](evidence/batches/live-005-continuation-3/index.json)。逐文件保留 hash，公开文本按内容寻址；二进制、大体积模型请求上下文和续作前的重复上下文只索引，原件留本地，环境敏感字段明确投影。只读导出及[再次逐源核验](validation/evidence-export-verification.json)通过，公开体积低于既有 10 MiB 上限；未执行工作负载，未提交 ORCA 二进制运行产物。导出使用 `--hash-only-contexts`，模型响应、Run/Artifact 账本和各次 outcome 仍公开；不宣称可脱离本地原件完整重放模型请求。[辅助证据索引](validation/additional-evidence-index.json)包含驱动、独立检查规则与成本审计脚本。

未重跑完整隔离矩阵，未验证 Windows 新增科学路径或本轮真实浏览器端到端；资源采样和每 Attempt 磁盘硬配额局限保留。本轮开发样本不替代步骤 6/7 正式重复验收。**步骤 5 交付实现、定向验证和有界失败事实，真实己烷退出条件未达到。提交及远端核验结果见交付回复；等待用户验收。**
