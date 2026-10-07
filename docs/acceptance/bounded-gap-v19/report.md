# V06 上下文修复与固定续验准备

日期：2026-10-08（Asia/Hong_Kong）。离线候选提交：`468254b005b8be5df8f29da269867e3c514261cb`。本次完成已授权的离线修复与续验准备；真实验证包仍未完成，等待用户验收。

## 已实现范围

`agent-json-v19` 仅在紧上下文的允许动作恰为 clarify/stop 时缩写重复的响应说明。完整八字段闭合 schema、参数校验、reason 模板仍保留；互斥且穷尽的 if/then 分支等价改为 oneOf。AUTHORITY、DATA、TOOL_CATALOG 和 check_contract 均未因此删减。决定及替代方案见 [0017](../../decisions/0017-protocol-and-delivery.md)。

来自真实失败 Run `run_7871e00efaf44e0e896972a686f9657e` 的[可移植 fixture](../../../tests/fixtures/phase_b/v06-feedback-context/provenance.json)保存完整构造输入、历史消息和 15 份来源 hash。离线回放保留 failed 状态及原费用；保守输入上界从 12372 降至最高 **11983**，覆盖 Result 创建时刻和剩余秒数位数边界。该数字是保守预约上界，不是实际 tokenizer 计数；距 12000 仅余 17，不代表任意其他上下文都能容纳。

续验 helper 使用同一执行链和原账本，新增固定包选择 `bounded-20261008-r2`。原包关闭执行及新槽准备，仍可查看原提案、复核已存轨迹。新包模型槽从 package 入口和底层 model evaluation 入口均检查明确批准、源码及运行环境冻结、累计预算、未知占用、前序真实复核和候选 SHA；不允许 resume、额外重复或套用旧通过记录。第五预算变更的离线测试覆盖中断后恢复，并逐条保持原费用与收据。

## 验证

上下文相关回归 280 项通过，其中 34 项真实派生与严格 schema/时间边界回归另经独立复跑。续验相关六个测试文件 201 项通过；最后补齐候选绑定反例后，续验专项 42 项通过。中间运行的 16 个本地历史回放表示断言失败保留在工作记录中：8 个未还原 v18 已允许省略的 False 权限值，4 个假定空 CONTROL 总是存在，4 个假定动作示例总是存在。修改只恢复默认值或改为核对完整 schema，原事实、文件 hash 与动作范围断言保留。

最终候选 `468254b` 在无本地 data 的干净 Windows 检出运行默认离线全量，结果为 **2364 passed / 193 skipped / 0 failed / 0 errors**，Ruff 通过。38 个生产模块实际从该检出导入，源码 hash 前后不变，结束时 git clean 且仍无 data；完整退出码、JUnit、日志 hash 和跳过分类见 [离线报告](offline-report.md) 与 [结构化记录](offline-validation.json)。跳过项为真实模型 130、真实 ORCA 13、缺少本地历史档案 49、Windows 符号链接权限 1，均未计为通过。

## 历史证据与真实状态

原 [N06 通过、V06 失败记录](../bounded-gap-final/report.md)保持原样。V06 不仅缺少模型最终回答，首轮单位和预算限制说明也未通过独立复核；本次代码修复不将这些失败改判为通过。尚无 v19 的真实模型响应或新科学结果。

本次离线修复新增真实模型 HTTP、PubChem 查询、OPI 生成和 ORCA 启动均为 **0**。实际账本仍累计 320 HTTP、1001551 tokens、USD 0.3978561，ORCA reference/development/formal 为 16/26/0，总计 42。核对以下原文件 SHA256 均保持不变：

| 原文件 | SHA256 |
| --- | --- |
| batch-ledger.json | `15f07f9cf0f86619c4be382349411e33f32c6d34e83bd195626b5fceb0acbe51` |
| 原批准 JSON | `9339874fb7f4359782ad7c9128f5dc535f6703b586ad45737748411968e3bc58` |
| 原 candidate.json | `0e52fa923132902018acfdae91d5127ace708c7af4eab82f979c97fbc1d8ef2f` |
| V06 grade.json | `ba8bf49c1edc8b496812e61be5bdbf2a483e00b714d8e5dc6946878bafdbdf18` |
| V06 review.json | `fc5e1f4339914ad5073bddc19c8f54a0764e1eea5aa7966f63b68f4908e03645` |

## 待明确批准的续验

[续验提案](../../reviews/2026-10-08-bounded-real-validation-renewal.md)完整列出 13 个零 ORCA 模型门槛、2 次身份查询、2 次初始结构准备、1 次独立参考和水/甲烷各 3 条模型＋科学轨迹。旧包未执行的机会由新批准范围替代，不同时保留两套机会。提议累计模型上限为 1120 HTTP、6889551 tokens，分别比当前批准值增加 2 和 7967；USD 10、ORCA 总计 119 及其分项上限不增加，原 C、条件 D、formal 分配保留。

当前新批准文件不存在，`RENEWAL_APPROVAL_SHA256` 为 None；未应用新上限、未创建新 candidate、未执行新包。用户此前批准的固定包要求“源码/profile/预算改变需要新冻结及范围决定”，且“实际 usage 低于上限不授权追加任务或新 Run 重试”，所以此前批准不能自动替代本次续验决定。

批次六的其余真实门槛、水/甲烷三次独立科学重复及阶段 B 原开发/正式矩阵仍未完成。续验包即使通过，也只覆盖 harness 取得并冻结初始几何后的独立模型/科学轨迹，不证明模型三次自主选择 resolve→prepare。当前状态：离线修复交付、续验待批准，等待用户验收。
