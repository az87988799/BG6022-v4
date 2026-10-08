# 第三开发候选真实验证：2 通过、1 失败，已停止

候选 `repair-cycle-20261008-development-3`，源码 `84f42aca55378bbe5c24be3d0e753ff262c07b46`，提示协议 `agent-json-v24`。候选凭据 SHA-256 为 `9ed3e45d6c6ddcf32b84a55ec0fcef2328b3ceff9a67975fc1cfc653c4b3bb0a`。这是同一冻结候选下的 3 个真实模型门槛；未拼接任何旧候选的通过结果。

| 槽位 | 实际结论 | HTTP | tokens | 已知费用 USD | 动作与交付 |
| --- | --- | ---: | ---: | ---: | --- |
| N06 unsupported system | 通过 | 1 | 3675 | 0.0017334 | 首答接受 normalize；只登记，不启动科学计算 |
| V06 insufficient additional budget | 通过负例验收 | 2 | 7033 | 0.0030162 | 两答接受；一次既有证据分析、接受 stop；原 sampling 目标仍未满足 |
| V07 array location | 失败 | 2 | 6697 | 0.0025734 | 首答计划接受；第二答调用被拒；仅一次字段发现，无最终回答 |
| 本候选合计 | 2 通过 / 1 失败 | 5 | 17405 | 0.0073230 | ORCA、身份查询、结构准备均为 0 |

所有实际费用均已知，未知 tokens/费用为 0。共享账本在归档时累计为 **337 HTTP / 1067287 tokens / USD 0.4274787**；本报告不重置或额外应用额度。运行原文、原始文件、费用及 SHA 见 [summary.json](summary.json)。

## 首答、反馈与失败事实

N06 首答同时保留乙醇目标、optimized 关系和方法条件，明确区分 H2O/CH4 科学能力限制与自身未绑定几何。用户只要求优化后的电子能，按决定 0015，energy + optimized 不额外强制坐标 Goal。登记无需 stop；目标仍为 insufficient_evidence，Run paused。槽中 interrupted.json 是登记暂停的通用文件名，不表示未知进程。该有限通知与实际原答通过不构成任意措辞的一般正确性保证。

V06 正确区分“既有证据可以分析”和“无许可/配额追加 ORCA”。三点中间点是离散内部最小值，但最近跨度 0.16000000000042536 Å 大于 0.12 Å + 1e-8，原 acquisition Goal 未满足。真实 stop、六轴原答审查、当前终止收据及报告绑定通过；Run failed / partial 是诚实负结果，与门槛通过并不矛盾，不是新科学成功。

V07 首次 initial_plan 被接受，正确路径与 required slice 均已存在于当前 Request/Step。第二次 feedback 提议却混合 `{step_id}` 与 `{tool,parameters}`，把 effect 名 `read_registered_artifact` 当作 Tool；内联路径及理由遗漏 `Dipole_Moment[0]`，误把 observation 关联到 g2，并越过尚未执行的 slice。实际仅发现根对象四个字段；数组切片、geometry index 0 的值读取及最终交付未发生。完整轨迹事实/语义审查失败，六轴均 null / not_verified，未拿 initial_plan 或程序 fallback 冒充最终模型回答。

第二答是正常反馈阶段提议，不是纠正答；它被拒后 **没有发送纠正 HTTP**。Run 以 `BudgetExceeded: required final explanation lacks remaining call/token/decision/time budget` 停止。已发送两请求 input upper bound 分别为 8261、11359，均在 12000 内；持久化记录没有 ContextLimitError。纠正与未来终止的预算预约问题需要离线分析；不能把未发送的纠正重建当作真实请求或新费用。

V07 immutable outcome 为 ordinary failure，影响依赖 `budget / protocol / query / terminal_delivery`，SHA-256 `7f74ba3619b3824be8ffaabd6e5a4f1641ce81ff646c1c14ee8a307dce9ea8f9`。本候选后续真实执行已关闭。

## 剩余范围与证据边界

固定 13 门槛中其余 10 个未执行，逐项见 summary；随后分层输入/参考/科学、文本 E2E、C、条件 D 及完整正式验收均未执行。此报告不宣称总体完成、科学通过或用户验收通过。旧候选失败、原始回答与既有费用保留不变。

本目录逐字节复制三槽 metadata、grade、review、immutable outcome、slot receipt 与运行摘要；原始大运行文件仅记录原路径/SHA。metadata 的 model_executed=false 等准备时字段保持原样，实际模型执行依据持久化 HTTP/model records 与最终 grade。

[便携反例](../../../tests/fixtures/phase_b/repair-cycle-v24-query-rejection.json)是实际记录的派生容器：两份请求 body、两份 response record 的 UTF-8 字符串可按原 SHA 验证；Request、Run、Plan、Result 为解析对象，明确不是容器文件的“原始字节”。它未执行新模型或科学计算；后续修补的离线验证须单独报告，不改判本次真实失败。
