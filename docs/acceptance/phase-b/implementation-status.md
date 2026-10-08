# 阶段 B 当前实施状态

更新：2026-10-08。**阶段 B 尚未完成，尚未提交整体验收；正式三次重复尚未启动。** 当前按用户采用的[最佳修复方案](../../reviews/2026-10-08-best-repair-plan.md)推进；本页是当前索引，历史批次不能替代当前候选的验收。

用户最新调整验证顺序：停止正在运行的测试，先集中完成剩余修复；中途不再重复全量测试，全部修复后统一进行一次全量验证。必要的定向检查与真实验收分别记录，不能把中断的测试计为通过。

## 当前检查点

- P1–P5 已实现并推送。独立干净检出、锁定安装的[完整离线验证](../repair-cycle-offline-v22/report.md)为 **2752 passed、202 skipped、0 failed**，Ruff 通过。202 跳过项按真实模型、真实 ORCA、外部历史档案和 Windows symlink 分列，不计为通过。
- [第一开发候选](../repair-cycle-development-1/report.md)冻结源码 `8ae3fd2`、提示 v22。N06 一次纠正后登记通过；V06 两答均被协议拒绝，第二答还错误关联几何与能量。原始失败及独立审查已保留，该候选停止后续执行。
- v23 已修补协议呈现、规划与终止解释的区分、证据成员对应，以及现有守卫覆盖，见[决定 0021](../../decisions/0021-readable-protocol-and-planning-evidence.md)。源码 `65947b1` 的[干净锁定依赖全量验证](../repair-cycle-offline-v23/report.md)为 **2785 passed、202 skipped、0 failed**，Ruff 通过；不把离线脚本轨迹称为真实模型或科学通过。
- [第二开发候选](../repair-cycle-development-2/report.md)冻结 v23。N06 程序接受纠正答，但能力边界和缺输入的告知混淆；V06 两答被规划协议拒绝，纠正答还误称 energy 已绑定 sampling。两个门槛失败、其余十一项未运行，原答及独立复核修订均保留。
- v24 按[决定 0022](../../decisions/0022-registration-notices-and-goal-targets.md)修复有限登记告知、目标路由互斥类型、目标几何缺项以及有界上下文呈现。源码 `84f42ac` 的[干净验证](../repair-cycle-offline-v24/report.md)为 **2919 passed、202 skipped、0 failed**，Ruff 通过；此前失败与提前停止记录保留。
- [第三开发候选](../repair-cycle-development-3/report.md)在同一 v24 源码上通过 N06 登记与 V06 预算不足交付，随后 V07 查询失败：第二答混用调用形状、将效果名当工具名并漏数组索引。实际只有一次字段发现，没有最终模型回答。**2 通过、1 失败、10 未运行**；候选已关闭，科学验证和正式矩阵不能据此准入。
- v25 按[决定 0023](../../decisions/0023-grounded-query-call-choices.md)修补实际调用枚举与复制例子，并使投影选择考虑未来交付预留，复用既有紧凑表示。生产修补的新原答反例、原额度四调用查询链及相关合同的[定向验证](../repair-cycle-v25-targeted/report.md)共 **132 passed**，该截点 Ruff 通过；这是离线证据，最终干净全量按最新要求集中到修复末尾执行。
- [第四候选的固定转拨工具](../../decisions/0024-fixed-development-transfer.md)已实现并通过独立静态审查，原 scope 和累计限额不变。首次专用测试会话未取回最终结果，不计为通过；用户批准后完成[4 项关键守卫定向检查](../repair-cycle-development-4-safety/report.md)，全部通过，未运行全量。[固定转拨已获实际批准](budget-approval-repair-cycle-development-4-20261008.json)并应用到同一账本，正在准备冻结与真实门禁；新增额度仅为原分项之间的固定转拨。

仍需完成：v25 干净离线验证；获具体批准后的新源码冻结及同一候选全部 13 个真实模型门槛；分层输入和水/甲烷各三条科学轨迹；两体系各三条自主纯文本 E2E；原 C 与实际触发才执行的条件 D；完整正式矩阵和独立 E2E 重复。正式核心覆盖为 76 变体 / 228 槽，另列 6 条 E2E；覆盖映射不是执行结果，旧候选两项通过不能搬到新候选。

## 成本与执行边界

第三开发候选结束时，唯一账本累计 **337 HTTP、1067287 已知 tokens、USD 0.4274787**，未知消耗为零；ORCA reference/development/formal 为 **16/26/0**，合计 42。本修复周期新增 **13 HTTP、48375 tokens、USD 0.0209178**，尚未新增 ORCA、身份查询或结构准备。这是产品运行账本，不包含开发助手本身的使用。

用户已批准并应用本修复周期及[正式矩阵增补](../../reviews/2026-10-08-repair-cycle-formal-delta.md)：累计上限 **2140 HTTP、14746912 tokens、USD 20**；ORCA reference/development/formal/总计 **37/68/108/213**，新增身份查询与准备各 22，执行活动 96 小时。各候选、分项、Run 原有限额及第二正式轮触发条件继续有效。旧包机会不重开，换候选不重置历史费用。

模型只能提出受约束建议；程序控制权限、预算、Tool 和科学判断。本地环境仍最多一个计算任务，4 核、总内存 1024 MiB、ORCA MaxCore 每进程 192 MB。科学范围仍为已声明的 H₂O/CH₄、中性单重态、RHF/STO-3G、SP 和严格无约束 Opt；未扩展至频率、DFT、远程或任意体系。

## 历史索引

[原 v16 状态快照](implementation-status-v16-snapshot.md)保留其当时记录；[B04/B05](b04-b05.md)、[B06/B07](b06-b07.md)、[B08/B09](b08-b09.md)、[组合可靠性](b10-reliability.md)和历次 repair 评审继续可查。旧失败、原答、成本和未验证项不改成新版本通过，也不跨候选拼接成功。

全部方案退出条件完成后再提交用户验收；当前不记录验收通过。
