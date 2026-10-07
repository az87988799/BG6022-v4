# 缺口闭合交付与当前真实包结果

日期：2026-10-08（Asia/Hong_Kong）。候选提交：`8a2dad5346e22875664fc756cdd3a88127e8d3bb`。**批次二至五的代码与离线验证已交付；批次六真实验证尚未全部完成。等待用户验收。**

最终干净 Windows 检出为 **2288 passed / 193 skipped / 0 failed / 0 errors**，Ruff 通过；细节和此前失败见[离线报告](offline-report.md)。随后按已批准的固定范围执行真实包：N06 单槽通过，V06 在分析反馈进入下一模型轮次前触发上下文限制。当前包已按停止条件停机，没有重跑 V06、放宽冻结上限或继续后续槽位。完整事实与原始证据 hash 见 [real-validation.json](real-validation.json)。

## 已交付实现与离线范围

| 批次 | 本次交付 | 仍不能据此宣称 |
| --- | --- | --- |
| 二 | 将优化结构资格绑定到最终收敛阶段，保留电子能与优化结构的独立资格。 | 任何新真实优化或完整 ORCA 能力验收。 |
| 三 | 严格交付协议、截断和纠正边界、最终回答预算保留、逐目标报告及可移植失败材料。 | 所有实际反馈都能在冻结上下文内完成；V06 已保留一个反例。 |
| 四、五 | 纯文本入口、受限名称、可信后续澄清、带许可和预算的 PubChem 身份查询、OPI 初始结构准备、版本和用途绑定及恢复边界。 | 新真实身份取证、OPI 生成、模型自主选择输入获取链或科学计算闭环。 |
| 六 | 固定候选与预算变更已经记录，执行两个真实模型诊断并保留一成功、一失败。 | 真实包完成、三次独立重复稳定性或阶段 B 整体验收。 |

代码范围和各组定向证据分别见[批次二](../bounded-gap-batch-2/report.md)、[批次三](../bounded-gap-batch-3/report.md)、[批次四、五](../bounded-gap-batches-4-5/report.md)。这些报告中的“等待后续集成验证”由本次最终离线记录补充，历史失败和原始收据不改写。

## 固定条件与实际轨迹

真实候选 freeze SHA-256 为 `0e52fa923132902018acfdae91d5127ace708c7af4eab82f979c97fbc1d8ef2f`。模型为 `deepseek-flash`，profile 为 `disabled`（关闭 thinking），提示版本 `agent-json-v18`，输入界限仍为 12000。逐文件复核 freeze 中的 343 个源文件 hash 全部未变。实际工作区与干净离线检出对应同一提交；38 个 Python 源文件仅有 CRLF/LF 换行字节差异，归一化后完全相同。离线原始源码 manifest 与实际 freeze hash 分别保留，不声称原始字节 hash 相等。

| 真实槽位 | 执行和审查结论 | 已知用量 |
| --- | --- | --- |
| N06：`raw-unsupported-system`，第 1 次 | 接受一次 `normalize_request`；7 个程序断言、协议及独立事实/语义审查通过。乙醇优化结构和优化后能量目标保留，未替换成已登记分子；只登记通知与缺项分开，未启动 Tool 或科学计算。Run 为 paused，两个科学目标仍为 insufficient_evidence。 | 1 HTTP，3875 tokens，USD 0.0019698 |
| V06：`insufficient-additional-budget`，第 1 次 | 接受 initial_plan，执行一次 `analysis.finite_sampling`。Call 和 Result 的操作状态完成，但没有合格输出，目标仍 insufficient_evidence。随后触发 `ContextLimitError`；没有第二次 HTTP，也没有被接受的最终回答，Run 为 failed、交付为 partial。该槽未通过。 | 1 HTTP，4092 tokens，USD 0.0017028 |

N06 的通过只支持本次“未支持乙醇、仅登记”原文解释和边界处理。该轨迹没有 Tool 结果需要解释，最终 stop 不适用；科学目标未完成。不能把它扩大为所有模型边界通过，也不能把登记暂停收据 `interrupted.json` 的文件名误记为模型传输失败。

模型没有单独解释乙醇超出当前支持范围；`unsupported_system:ethanol` 是程序保持的事实。这一限制与单槽通过同时保留。

V06 的第一份提议满足协议，不等于结果解释交付通过。程序保存的实际诊断是：

> required model context exceeds the 12000 global bound, Run input limit, or remaining token reservation

本次在分析结果反馈后触发该错误，未通过删掉目标事实、扩大 12000 上限或补发请求取得“成功”。独立审查与程序 grade 的具体状态、模型文本 hash、请求/响应收据 hash、Run/Result/Call 引用均保存在结构化记录中；缺失的最终回答不由程序报告或首轮理由代替。

V06 的独立审查还发现首轮解释有两项缺陷：把距离字段与能量字段一并放入 Eh 单位说明，以及未披露零追加 ORCA 预算和禁止追加科学执行的关键限制。因此事实/语义审查也未通过；不能把失败仅归因于上下文容量。程序分析得到相邻采样跨度约 0.16000000000042536 Å，大于目标 0.12 Å，理由为 `span_too_wide`；这是确定性 Result 的结论，模型没有收到后续上下文，也未解释该结论。

## 预算与保留证据

已应用的最小原始预算变更收据原样保存在 [budget-amendment.json](budget-amendment.json)，SHA-256 为 `158513a6bce40dd81af71104631d99eb97d7ec7fcb34bc780ebdf81b8133a06e`；它与运行账本 `limit_authority.receipt_sha256` 一致。授权源 hash、变更前账本 hash、变更后快照 hash 和限额前后值都记录在 real-validation.json。

| 项目 | 本包增量 | 累计账本 |
| --- | ---: | ---: |
| 模型 HTTP | 2 | 320 |
| 已知 tokens | 7967 | 1001551 |
| 已知 USD | 0.0036726 | 0.3978561 |
| 未知 tokens / USD | 0 / 0 | 0 / 0 |
| ORCA reference / development / formal | 0 / 0 / 0 | 16 / 26 / 0，共 42 |
| PubChem 查询 / OPI 生成 | 0 / 0 | 本报告不据此重算历史累计 |

USD 为项目按冻结计价依据和已知供应商 usage 记账的金额，不代表独立核验供应商账单。旧 16 个 reference 条目、26 个 agent science 条目和 318 个模型记录逐条未变；仅追加两个已知模型记录。预算迁移沿用原账本，没有通过换 Run、换槽位或重建账本清零。

原始数据目录中的首次请求、响应、决策、分析结果、暂停/失败状态及独立 review 都保留。本目录保存精简结果、证据位置和 hash，不复制大型历史原始档案。离线通过记录与真实失败记录分开，不能相互替代。

## 停止后的未执行范围

剩余 **11 个模型诊断/原文门槛槽位** 未执行：V07 array-location，V09 different-method / missing-electron-state，N03 electronic-state-clarification / ambiguous-reference，N04 authorized-defaults / unique-inheritance / unconfirmed-inference，N07 read-only-window，N09 user-goal-replacement / preserve-goal。结构查询 2 次、结构准备 2 次均未执行；1 次独立参考 ORCA 与 6 次 development ORCA（共 7 次）均未执行。

因此，水优化和甲烷固定准备几何单点各三次真实重复仍未证明；真实模型自主选择 `resolve → prepare` 三遍也未证明。固定包本身将输入获取作为生产 Tool 的显式操作，并不覆盖模型自主选择该链；即使后续执行完包内科学重复，也不能自动获得这一额外结论。

原阶段 B 的 C、条件 D、formal 矩阵保持未完成。本次停止不授予继续真实执行的新范围；若修复上下文并提出新的候选及固定执行范围，应另行记录、审查并取得所需授权，保留当前 V06 首次失败。当前交付状态为：**代码和离线证据已交付，真实包部分完成并停止，等待用户验收。**
