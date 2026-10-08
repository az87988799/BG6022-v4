# r3 固定真实验证包：N06 通过，V06 失败并停止

日期：2026-10-08（Asia/Hong_Kong）。真实候选为 `697c0d77d7f46924debe366c8a812485219b2c0f`，prompt 为 `agent-json-v20`。用户“批准，直至完成”已按固定范围记入[批准文件](../phase-b/budget-approval-bounded-20261008-r3.json)，并向原账本应用第六次预算增补。“直至完成”没有增加次数、预算或失败后的重试机会。本包**未通过，已停止**。

## 实际结果

`N-06/raw-unsupported-system`：Run `run_f303f382ff7a4678b312a41ef7e6e4d5`。模型分别登记乙醇优化结构与优化后电子能两个必需 Goal，保留气相 RHF/STO-3G、中性单重态和仅登记需求的范围。7 项程序断言、全轨迹事实/语义及六轴独立审查通过。通信为 `registration_only`、`questions=[]`、`awaiting_reply=false`，Run 暂停；两项目标仍为 `insufficient_evidence`，没有科学完成声明或执行。reason 中更具体的“post-Opt SP”措辞未产生 Plan 或额外 SP 义务；此通过仅限当前登记。

`V-06/insufficient-additional-budget`：Run `run_39705f5de3884f40a63d86e983ee19db`。接受了初始 Plan 和最终 `stop`，执行 1 次生产 `analysis.finite_sampling`，复用 3 个合格历史能量 Result，新增 ORCA 为 0。模型正确复述内部最低采样点及邻点跨度 `0.16000000000042536 Å`，超过目标 `0.12 Å` 加 `1e-8 Å` 容差；程序检查为 `span_too_wide`、`qualified_outputs={}`、Goal 仍证据不足，Run 为 `failed`、交付 `partial`。

独立审查发现模型未说明追加 ORCA 预算为 0、科学执行不可用，却声称 “goal evidence is sufficient to answer within sampled range” 和 “no additional allowed action is needed because the required evidence is complete”。停止动作本身被接受，完整最终回复门槛通过；停止理由的事实和语义不合格。预算披露行为、limits 与 next_action 轴失败；quantity、unit、conditions、source 仅就准确的局部描述通过。最终 grade 为 `incomplete_or_failed`，分类 `failed`。这次 11983 / 12000 的反馈上下文成功发送；没有把原 r1 的上下文失败复写为通过，也没有把协议通过当作解释正确。

其余 11 个模型门槛、2 次 PubChem 查询、2 次 OPI 准备、1 次独立参考和 6 条水/甲烷科学轨迹均未执行。原 r1/r2 的失败、费用、Run 与 Result 均保留；没有复用旧通过来抵扣 r3 门槛，也没有换 ID 或恢复重试。

## 成本和证据

| 项目 | r3 新增 | 累计实耗 | 已批准累计上限 |
| --- | ---: | ---: | ---: |
| 模型 HTTP | 3（N06 1、V06 2） | 324 | 1121 |
| 模型 tokens | 13105（4685 + 8420） | 1018912 | 6893807 |
| 模型 USD | 0.0062742（0.0029229 + 0.0033513） | 0.4065609 | 10 |
| ORCA reference | 0 | 16 | 17 |
| ORCA development | 0 | 26 | 54 |
| ORCA formal | 0 | 0 | 48 |
| ORCA 总计 | 0 | 42 | 119 |

3 次 HTTP 均为 200、finish_reason 为 stop；总输入 10502 tokens、输出 2603 tokens，费用全已知，未知占用为 0。身份查询、准备均新增 0 次。一次 analysis Tool 操作成功不等于科学端口合格或用户目标完成。

[结构化记录](real-validation.json)包含实际请求/响应/决策、完整独立 review/grade、原始文件路径与 hash、历史来源及剩余未执行槽位。[预算收据](budget-amendment.json)保存原始字节；[候选审计](source-freeze-audit.json)证明当时 353 个源码文件及二进制 hash 匹配，并明确区分迁移前 321 条模型记录与审计期间新增 N06 消费。最终结算后原 321 条模型记录、16 reference、26 development 逐对象未变，仅追加本包 3 条已知模型消费；68 个 r1/r2 历史路径核验通过。

| 证据 | SHA256 |
| --- | --- |
| r3 candidate | `cac8a52e0d4df259725e7fadaa76da3a37315f01752b7040885124288ea6466e` |
| r3 批准文件 | `428fa58791993d77055b70ce99249616051edd86eab2b89153bb0e737adf47d2` |
| r3 预算收据 | `735b15ad21fb43c5191b7873370ca555ea07aa0a2a24cef465301c34c1ea3618` |
| N06 review | `9a44f69b7096441fcfffe65285159a0c2142b4b944ce2b3bd08ffcd37588273e` |
| N06 grade | `179e9aff87990959869b6fceaac10958859e0ad4a8abfa0a408160a75bacefc1` |
| V06 review | `889a405b9d6841426d6d2e0daad059a777fbef0b489008c185e1126848e9ff64` |
| V06 grade | `a59c69dc07d3d9b26989b987ff075559efb7bd29ac2d4ca9a5b9c5570d24ea3a` |
| 本次结算后账本 | `b5b0efbfb7b8a805a904b08fbf3d35e7a325809c897bab3c7c68ede2ae039b13` |

## 离线证据与后续边界

[v20 干净全量](../bounded-gap-v20/offline-validation.json)对应 `329e595`：2444 通过、193 跳过、零失败，Ruff 通过；其中真实能力开关关闭和本地历史缺档仍按未验证报告。实际批准记录和 pin 随后在 `697c0d77` 提交，其[审批/预算/包守卫定向验证](approval-validation.json)为 233 通过、Ruff 通过。两层验证分别记录，不声称批准提交重新完成了相同全量，也不以离线通过代替本次真实解释验收。

后续解释边界修复属于离线工作；原冻结回复、review、grade、费用和科学检查保持不变。新的真实机会须另行明确固定范围和冻结候选，本包剩余机会不能自动转为调试调用。批次六其余真实门槛、水/甲烷重复验证和原阶段 B 剩余矩阵仍未完成。本报告交付实际失败事实，等待用户验收。
