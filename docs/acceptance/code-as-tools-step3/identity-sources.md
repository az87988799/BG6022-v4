# 通用身份与条件选择开发证据

日期：2026-10-18。此记录属于步骤 3 开发验证，等待用户验收。它不代表真实模型生成计划或真实 ORCA 计算通过，也不授予新物理量的生产科学资格。

## 实现与本地选择

一般文本身份保留用户逐字名称和引文，查询别名独立标为推断。程序使用冻结的通用图规则，不含乙醇名称、CID 或 SMILES 的生产特例。已有 v1 水/甲烷结构回执和检查范围保持原状；新身份契约仅选择 v2 结构检查，并经 Store、计划和实际输入消费校验。

本次选择了比单纯保留模型翻译更保守的名称确认路径：PubChem 同义词不能确认别名时，使用 Wikidata 的精确中文标签/别名及 PubChem CID 交叉引用核对同一图。Wikidata 是公开社区维护数据，属于带来源的名称映射；它不证明特定实验样品、唯一构象或电子基态。这项独立映射要求是本次局部实现选择，不能被描述为总蓝图对所有名称翻译的强制要求。[Wikidata P662 定义](https://www.wikidata.org/wiki/Property:P662)、[Wikidata 数据访问说明](https://www.wikidata.org/wiki/Wikidata:Data_access)。

HTTP 只允许固定 PubChem 性质/同义词和 Wikidata 搜索/实体读取端点；无代理、重定向、凭据或重试。每个 resolve Call 最多四次请求、累计检索期限 120 秒。性质响应仍限 256 KiB；同义词和每个 Wikidata 响应各限 1 MiB；实体候选至多五个。原始响应必须完整才能用于确认，同一次身份查询调用的实际 HTTP 数另列于回执。

## 真实公共数据探针

真实探针结果与生产 Tool 结果分别保存：

| 观察 | 实际结果 |
| --- | --- |
| PubChem 原始中文名称“乙醇”性质查询 | HTTP 404，完整响应 156 bytes，`No CID found`；保留失败 |
| PubChem 推断英文别名 ethanol 性质查询 | HTTP 200，296 bytes，CID 702、SMILES CCO；此步单独不确认中文别名 |
| PubChem CID 702 同义词 | 先触及原 256 KiB 上限；显式扩大该类响应至 1 MiB 后完整取得 449348 bytes、9535 条，未含“乙醇” |
| Wikidata 无联系 URL 的初始客户端标识 | HTTP 403，2194 bytes，保留 robot-policy 返回；不将其当作空搜索结果 |
| Wikidata 合规客户端标识 | 按公开 User-Agent 要求提供项目联系 URL 后，固定 API 返回 200；不伪装浏览器、不换代理 |
| Wikidata 搜索与实体 | 原中文名搜索五个候选；实体响应 552031 bytes，响应无 Content-Length。唯一具匹配化学交叉引用的 Q153，其中文标签精确为“乙醇”，P662 唯一为 702 |

客户端标识遵循 [Wikimedia User-Agent Policy](https://foundation.wikimedia.org/wiki/Policy:User-Agent_policy)。初始独立探针脚本曾错误解析已经截断的同义词响应并抛出 JSONDecodeError；该开发探针错误未用于身份确认，生产实现先检查完整性。随后完整取证和正式 Tool 路径另有成功记录，不抹去第一次失败。

生产 `structure.resolve` 通过已有 Store/Call/Result/Artifact 链实际完成四次公共 HTTP，消费一次身份查询额度并归档五个 Artifact。`run_fbde0d295bbf4223b924c291b80aea65` / `result_3479e98a614c4d0c8ce32cad20668f93` 的结构 v2 回执为 completed，CID 702、CCO、别名已由独立来源支持。该运行使用测试作者编写的语义候选和只做身份查询的 Plan；**没有真实模型调用、几何生成或 ORCA 启动**，不能混为自主任务验收。

完整本地证据位于 `data/code-tools-step3/identity-production-9b216caf/`，初始公共源探针位于 `data/code-tools-step3/identity-proof/`。可提交的小型 [hash 索引](identity-evidence.json) 记录完整生产 Artifact，以及探针结束后为首轮模型运行保存的结构源码身份；后者不是补造的探针启动回执。大体积原始响应留在本地。Wikidata 实体响应没有提供所请求字段范围之外的修订号，因此回执 `entity_revision=null`；实际字节 hash 和取证时间已保存，不补猜版本。

## 离线验证与限制

`general-structure-004.log`：新旧结构及选择测试共 116 passed，43.46 秒。覆盖一般名称与多种分子图、v1/v2 不混用、真实 Store 的选择来源/许可/目标保留、伪造 hash/引文、未知或未授权字段、准备后选择拒绝、中文别名精确交叉引用、错 CID/近似名称/多 CID/重复或多余实体拒绝，以及固定端点和分响应大小边界。测试中的 PubChem/Wikidata 响应和初始 XYZ 明确为合成夹具，不能冒充真实检索或生成计算。

当前准备仅覆盖已确认/配置默认的中性单重态、无自由基及无立体中心/立体键图。解析保留未指定立体信息，准备保守拒绝，未扩大立体构型、带电态或基态科学保证。新几何仅通过有限坐标、原子映射及图键距离检查，未验证优化收敛、所有构象、频率或极小值。方法和基组的选择保留官方来源与理由，电子态不能通过该入口从电子奇偶性推断。网络失败或未确认名称仍可能使具体请求无法继续；有界失败属于真实结果。

## 首轮模型语义失败与离线纠正

另一个真实模型 Run `run_4a510aba347840b18119f25d73cb8f7d` 在语义入口停于 waiting_user，共两次模型调用、8246 tokens、零 ORCA。按 `run.model_records` 顺序，`model_f766…` 被原入口拒绝，`model_5edb…` 纠正后曾被接受，但仍将有授权的方法/基组选择误记为未知，并将一般频率目标误记为未注册端口；原提示同时残留了水/甲烷专用检查范围的全局表述。该 Run、原始回复和先拒绝后接受的事实不改写。

本轮修复将代码授权路径的专用检查范围明确标为旧科学资格范围，统一从语义契约投影一般目标和自动选型规则；模型必须明确保留逐字物理量、端口、几何关系及已授权选型，程序只提供有限纠正，不替模型补写。真正未知的字段、目标缺项及用户明确要求的更高证据保持约束。明确单位仅对已覆盖的有限句式保留逐字最低证据；没有新增单位检查器，其他语言表达未获完整理解保证。

`tests/fixtures/code_intake_live001.json` 保存两条真实回复、原响应 hash 和历史状态。离线回放只重绑本地测试消息 ID，不改写语义字段；另一个测试作者编写的正确候选只证明程序路径。`intake-correction-003.log` 为 11 passed，覆盖真实回复拒绝、完整目标/授权保留、明确与未知几何、单位要求和 qualified 不降级。它不代表下一轮真实模型已成功纠正。

扩大回归 `intake-regression-002.log` 为 237 passed、4 failed；四项失败均为旧范围提示的文字断言，原历史响应和科学条件校验未失败。只更新这些断言后，新语义及旧通知三个模块在 `intake-final-006.log` 取得 29 passed；schema 的独立列表示/引用展开等价回归 `binding-005.log` 取得 10 passed。全仓库和后续真实任务结果由主验收报告单独记录。
