# 优化最终阶段约束

日期：2026-10-08。实现提交：`fe54b5d7138100521a4de0476a13c385ae8a8abb`。对应方案批次二；规则决定见 [0016](../../decisions/0016-final-optimization-stage.md)。等待用户验收。

适配器将末次优化周期、严格阈值表、成功/失败标记、几何和正常尾段绑定到同一阶段。新增必需检查 `optimization_stage_binding`，来源局部版本为 `optimization-final-stage-1`。不完整的新周期、后续失败、错位阈值或不同几何不能借用早期成功。电子能的独立资格保留，优化后能量及下游几何消费仍要求有效优化结构。

定向 `test_optimization_final_stage.py` 的 24 项通过；相关收集、读取和消费回归 122 项通过、1 项 Windows 链接用例跳过。合成反例经生产收集和消费路径验证；历史真实甲烷 Opt 和水 SP 仅只读回放，无新计算。真实回放的原始输入 hash 和来源位于 `tests/fixtures/optimization_final_stage/real_methane_opt/provenance.json`，输出复用既有跟踪 fixture，不修改历史 Result。

历史优化结果缺少新检查时仍可查看，不能直接获得新的结构消费资格。新的真实优化证据和独立终态比较由已批准的批次六固定包提供；本报告不把合成失败或历史回放算作新真实计算。合并后干净检出的全量离线验证另行记录。
