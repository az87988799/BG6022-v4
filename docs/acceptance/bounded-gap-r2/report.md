# r2 固定真实验证包：首槽失败并停止

日期：2026-10-08（Asia/Hong_Kong）。真实候选为 `4df15b29318f9a6da66ff0ca2a3998647e217bbe`。用户“批准，继续”已记录于[批准文件](../phase-b/budget-approval-bounded-20261008-r2.json)，并按[续验提案](../../reviews/2026-10-08-bounded-real-validation-renewal.md)向原账本应用第五次预算增补。批准、执行成功和验收通过分别记录；本包**未通过，已停止**。

## 实际结果

仅执行 `N-06/raw-unsupported-system` 一个新 Run：`run_9e630ccacf2841c186bf501d78b7f235`。用户要求登记乙醇优化及优化后的电子能，不启动计算。模型正确说明乙醇超出当前科学范围、没有登记几何、仅登记需求，但提交的唯一 Goal 的实际端口为 `optimized_geometry`，没有 `energy` Goal。Goal 名称和 reason 中出现电子能不能代替所需物理量的独立目标。

原有 7 项程序断言和提案协议检查均通过；独立审查发现电子能目标遗漏，因此 quantity 轴和整体语义审查失败，最终 grade 为 `incomplete_or_failed`、分类为 `failed`。模型描述的事实本身正确，其余五轴仅在实际描述范围内通过，不表示目标覆盖完整。

另发现程序交付缺陷：已登记 `registration_only`，模型 questions 为空，但程序把能力限制的自由文本 gap 当成必须答复的缺项，保存 `awaiting_reply=true`，Run 进入 `waiting_user`。该状态由程序导出，不能归为模型直接设置等待标志。

本包没有执行 Tool、生成 Result 或启动科学计算。V06 及后续 11 个模型门槛、2 次身份查询、2 次 OPI 准备、1 次独立参考和 6 次科学轨迹均未执行。没有恢复旧 Run、换 ID 重试、改写失败为通过或借用后续槽诊断。

## 成本与证据

| 项目 | r2 新增 | 累计实耗 | 已批准累计上限 |
| --- | ---: | ---: | ---: |
| 模型 HTTP | 1 | 321 | 1120 |
| 模型 tokens | 4256 | 1005807 | 6889551 |
| 模型 USD | 0.0024306 | 0.4002867 | 10 |
| ORCA reference | 0 | 16 | 17 |
| ORCA development | 0 | 26 | 54 |
| ORCA formal | 0 | 0 | 48 |
| ORCA 总计 | 0 | 42 | 119 |

HTTP 为 200、finish_reason 为 stop，输入 2974 tokens、输出 1282 tokens；费用已知，未知占用为 0。退出码 0 仅说明评测 helper 完成记录，不表示槽位通过。PubChem 和 OPI 准备各新增 0 次。

完整原始文件清单、逐项检查、独立 review/grade、候选与环境冻结、预算迁移和 hash 见[结构化记录](real-validation.json)。[预算收据](budget-amendment.json)保留原始字节；原账本累计费用和 r1 失败未重置。

| 证据 | SHA256 |
| --- | --- |
| r2 candidate | `46fccfc7746796b9e00f3cb5b1a3bf2379095800c93f7cbbed7a2d1f8e0b95b6` |
| r2 批准文件 | `97ba032010094862df3854e06e642e42b2872ba833b96f1a89c0f40c3e6b89b9` |
| r2 预算收据 | `a8088812f0c54132019e43ad11c0d62af67526c37ec73a091ec2902d210b9698` |
| N06 review | `90058155652733efffcd1e0a3eae3808811ed52c50b4bafc91543dcdf3c50941` |
| N06 grade | `1a0b9fc2a66b7e6f477e301f9249b44d238213a4708aa5a55e3634c802bdf43e` |
| 本次结算后的账本 | `822d08480b166cfd62183f7ba688052c6beca78562191fb44554f89664ec6d5d` |

冻结候选的 348 份源码/配置/测试文件及运行环境在真实调用前已全部核对；此事实不意味着随后离线修复的开发工作区仍与该候选相同。[v19 干净全量](../bounded-gap-v19/offline-report.md)为此前源码的离线证据，不能替代本次真实语义通过或后续新源码验证。

## 后续边界

本次发现的目标覆盖和登记待答状态问题继续按已授权的缺口方案离线修复，原始 Run、Request 版本、模型响应和审查字节保留。新的真实执行机会须重新明确固定范围并冻结候选；本包剩余额度不能自动变成失败后的重试机会。

阶段 B、批次六其余真实门槛及水/甲烷重复验证仍未完成。本报告交付实际失败事实，等待用户验收。
