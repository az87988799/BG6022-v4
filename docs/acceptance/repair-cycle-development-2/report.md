# 修复周期第二开发候选：真实门槛失败记录

冻结候选 `repair-cycle-20261008-development-2`，源码 `65947b1043526be1ae877b563fe4f2fd7608ab5b`，提示 `agent-json-v23`。候选 SHA256 `306ccd18b01613740d7bb40c62d43cf5d8bb1adf24e8b7b14744d80ddbe28020`；[候选原件](candidate.json) 绑定[干净离线验证](../repair-cycle-offline-v23/report.md) 2785 passed / 202 skipped。离线通过不代表实际模型门槛通过。

**本候选 2 个门槛均失败，其余 11 个未运行。** N06 记录 ordinary/request_semantics 失败后，按已采用的非共享依赖规则执行 V06；V06 记录 ordinary/protocol 失败后停止后续共享协议槽。没有新 PubChem、OPI 准备、ORCA、生产 analysis、分层科学、自主 E2E、C、条件 D 或正式运行。

| 槽 | 实际行为与独立审查 | HTTP / tokens / USD |
| --- | --- | --- |
| N06 未支持体系登记 | 首答额外 `type` 被拒；纠正答被接受，但通知混淆科学能力边界与缺少登记身份/几何。全程 facts 通过、semantics 失败，最终 limits 失败，其余五轴通过 | 2 / 7423 / 0.0036138 |
| V06 零追加预算 | 首答把 gap code 当 Goal ID；纠正答同时提供 `step_key` 与 `gap`，仍被拒。全程 facts/semantics 失败，无接受终态，最终六轴均未验证 | 2 / 8438 / 0.0035475 |

合计 **4 HTTP、15861 tokens、USD 0.0071613**，unknown 为 0，ORCA 为 0。归档时唯一账本累计 **332 HTTP、1049882 tokens、USD 0.4201557**；历史 ORCA reference/development/formal 为 16/26/0，共 42。已批准累计上限仍为 2140 HTTP、14746912 tokens、USD 20；ORCA 37/68/108/213。没有清零旧费用或借用后续分项。

## N06 审查修订

最终接受通知为“乙醇不在已登记体系范围内，无法解析其身份与几何。”它能说明缺少登记输入，却没有清楚分开 H2O/CH4 科学能力范围与乙醇几何尚未登记。程序补入的 unsupported 标记不能替代模型告知。初次独立审查曾把“登记范围”解释为能力披露而判通过；复核实际发送的区分要求和原答后撤回这一推断。[原审查](n06-prior-review-001.json) 和[最终审查](n06-review.json) 均逐字节保留，未改原答、规则或费用。

本次失败不来自单一 energy+optimized Goal：决定 0015 允许该表达，不强制额外结构 Goal。纠正答实际保留两个 Goal、无待答问题、不执行，登记通信程序门槛通过；这些事实仍不能抵消最终能力披露缺项。`interrupted.json` 是通用 harness 对 paused 登记的命名，不表示未知进程。

## V06 绑定与科学边界

首答将 `goal_has_no_evidence_binding` 写成目标 ID，遗漏真正的 `finite_sample_internal_minimum`。纠正答恢复正确 ID，却把未来 Step 输出与显式 gap 合并到同一 binding；该协议要求二者择一。两次计划均未接受，所以没有 Plan、analysis Result 或模型终止交付。第二答还声称已有合格能量“bound to the sampling port”，而原 Result 只有 energy 端口，当前 sampling Goal 没有证据绑定。

两答三组成员—能量引用均正确，不能沿用第一候选的“能量错配”结论。独立只读核对：左/中/右能量为 -74.962692158239 / -74.964906308165 / -74.954287966128 Eh；中间点是可区分内部最低点，但最近已采样邻点跨度 `0.16000000000042536 Å > 0.12 Å + 1e-8 Å`，原 sampling 取得目标不满足。两个可选未采样点不是失败的必需成员，结论仅限离散样本。此复核没有创建新的生产分析结果。

第二答正确披露零 ORCA、零追加额度，并提出被许可的 existing-evidence analysis；不因它选择 analysis 而判错。错误来自绑定协议与当前科学证据表述。所有最终六轴及最终预算披露保持 null/not_verified，不拿被拒原文或程序 fallback 报告补造最终答案。

## 证据与未完成范围

[精简归档清单](summary.json) 核对同目录所有小收据、metadata、grade、review/prior 的原始 SHA；[N06 outcome](n06-outcome.json) 与 [V06 outcome](v06-outcome.json) 均为 recorded failed。V06 grade 顶层为 not_verified（缺接受最终回答），其 full-trajectory review 与周期 outcome 明确失败，这些层次分别保留。

[便携反例](../../../tests/fixtures/phase_b/repair-cycle-v23-rejections.json) 是明确标注的派生 JSON：包含两 Run 的请求、全部四次原提议与拒绝信息；内嵌原 request/response/run 文件 UTF-8 文本，重新编码可逐字节核验原文件 SHA；`run_state` 为完整原 run.json 解析对象，并以既有 `run_record` hash 绑定，不从 metadata 推导。整个派生 fixture 本身不称原文件。后续合成修正不得覆盖这些原答，也不能算新的真实通过。

第一候选及旧 bounded 失败与费用保持原样。后继补丁须经离线验证与新冻结后再逐槽评审；本候选无可复用通过槽。分层科学、自主 E2E、C、条件 D、正式完整验收和最终交付仍未完成，不能据本报告宣称整体完成。
