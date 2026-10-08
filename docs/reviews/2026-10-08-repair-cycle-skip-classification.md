# 历史档案跳过项与便携核心回归

本记录落实[已采纳修复方案 §8.1](2026-10-08-best-repair-plan.md)的分类要求。数字 **49** 来自 v21 干净、无 `data/` checkout 的实际 JUnit，源码 `cd2196bf32d4c2b66c596ced95b5fab56763e7f7`；不是当前候选的预计跳过数。该次全量为 2485 passed / 193 skipped，其中 130 为未启用真实模型、13 为未启用真实 ORCA、49 为缺少外部历史档案、1 为 Windows symlink 权限。当前候选的实际总数由其独立全量记录给出。

来源为 [v21 离线验证记录](../acceptance/bounded-gap-v21/offline-validation.json)及原 `pytest-junit.xml`，后者 SHA256 为 `9e67cbadf37e5245e40a7c71275fa78529523611f28e4a1704585f4ca9296cf3`。分类仅只读核对测试与该 JUnit；没有为分类重新执行历史测试。

## 分类原则

“该旧 Run 的原请求、原答、review、usage 和全部文件 hash 仍未改变”必须读取相应外部档案，缺档案仍跳过并报告未验证。与之不同，来源绑定、条件隔离、未知单位、预算、协议纠正、恢复以及当前交付等产品行为必须在干净 checkout 中有小型便携回归。新回归可以使用明确标注的合成正反例，或带来源 hash 的真实派生小 fixture；不能把合成记录称为原历史科学证据，也不能把行为通过记成旧失败通过。

因此下表不是把 49 项的全部断言统一豁免为可选：其原档案审计部分继续保留；所含核心行为由下一节对应的便携测试承担。不删旧测试，不复制大型运行档案，不以清零 skip 为目标。

## v21 的 49 个具体节点

以下路径均在 `tests/unit/`；`[...]` 表示原 JUnit 中的参数化节点。

| 文件与节点 | 数量 | 必须依赖外部档案的部分 |
| --- | ---: | --- |
| `test_context_action_budget.py::test_retained_v06_budget_actions_rebuild_without_changing_history[first_plan/analysis_feedback/correction]` | 3 | 原 V06 三份真实请求、使用量与控制信息逐项回放并保留原字节 |
| `test_context_actual.py::test_actual_array_slice_feedback_keeps_pending_scalar_and_fits_bound` | 1 | 原数组 Run 的特定 Result、pending scalar 与原 Run hash |
| 同文件 `test_actual_sampling_three_sp_and_analysis_preserve_facts_within_bound`：2 个 Run × 2 阶段 × 2 profile | 8 | 特定真实三 SP/分析轨迹及当时事实的回放 |
| 同文件 `test_actual_import_stop_budget_labels_include_this_transmission_without_mutation` | 1 | 特定第三次真实 HTTP 前的累计与剩余额度 |
| 同文件 `test_actual_array_correction_preserves_native_control_within_bound[persisted/structured_ready_ids]` | 2 | 原数组纠正响应和持久化诊断 |
| `test_context_clarification_scope.py::test_real_v5_post_normalization_context_keeps_explicit_scope_and_unknown_unit_without_mutation` | 1 | 特定 v5 登记请求的原文、规范化结果和未知单位 |
| `test_context_execution_facts.py::test_retained_query_import_results_keep_their_own_tool_and_effects`：2 个 Run | 2 | 原只读/导入调用与 Result 的具体身份及原请求 hash |
| `test_context_fact_contract.py::test_retained_v8_request_rebuild_preserves_facts_without_rewriting_real_evidence`：12 个输入时点 × 2 profile | 24 | v8/v14/v15 原 HTTP、纠正/澄清记录及原始对象不变性 |
| `test_phase_b_model_cases.py::test_existing_real_references_fit_context_without_writing_historical_runs[V-02/free-energy-protected,V-09/compatible-water,V-06/insufficient-additional-budget]` | 3 | 原独立参考 Store 中的真实 Run、Result、artifact 和原记录 hash |
| 同文件 `test_legacy_archive_restoration_preserves_original_bytes_and_rule` | 1 | Phase A 原档案的逐文件恢复及旧 `orca-hf-1` 记录原貌 |
| `test_semantic_named_identity.py::test_retained_v12_failure_stays_exact_while_new_context_explains_named_identity` | 1 | v12 特定模型原答及其固定 receipt hash |
| `test_semantic_placeholder.py::test_actual_n06_sentinel_replay_keeps_bad_questions_and_original_failed_review` | 1 | v15 N06 的原错误问题、原 sentinel 与原失败 review |
| `test_semantic_registration_policy.py::test_actual_n06_request_rebuilt_with_separate_scope_and_registration_without_rewriting_reply` | 1 | 特定 N06 原请求和原模型回答的完整只读回放 |
| **总计** | **49** | **未执行的档案审计不得由便携结果替代** |

24 个 fact-contract 节点的 12 个输入时点为：`array_pending`、`array_final`、`import_initial`、`import_final`、`comparison_basis`、`comparison_unknown`、`v14_array_initial`、`v14_array_first_rejection`、`v14_array_second_rejection`、`v15_unknown_initial`、`v15_unknown_clarify_rejected`、`v15_unknown_clarify_accepted`；各有 `disabled` 与 `thinking_low`。两种 profile 的档案回放都不代表新模型调用。

## 必须便携的核心行为及对应节点

| 核心行为 | 不依赖开发机历史 `data/` 的证据入口 |
| --- | --- |
| V06 成员合格但目标不足、零许可/额度、纠正及终止容量 | [test_context_action_budget.py](../../tests/unit/test_context_action_budget.py) 的 `test_portable_insufficient_sampling_feedback_retains_all_five_members_at_revision_limit`；[test_context_terminal_replay.py](../../tests/unit/test_context_terminal_replay.py)；[test_context_terminal_outcome.py](../../tests/unit/test_context_terminal_outcome.py) 的 `test_actual_failure_is_retained_with_budget_and_goal_facts_visible`；新 [test_decision_purpose_capacity.py](../../tests/unit/test_decision_purpose_capacity.py) 的 `test_actual_v06_all_required_facts_fit_eight_thousand_without_relabeling_failure`、`test_unmet_goal_uses_last_slot_and_saves_exact_snapshot_without_future_double_count` |
| 数组分片、pending scalar、未知单位及控制诊断不能被压缩丢失 | [test_context.py](../../tests/unit/test_context.py) 的 `test_large_discovery_keeps_literal_locations_and_explicit_context_continuation`、`test_actual_raw_dipole_value_survives_large_descriptive_source_metadata`、`test_correction_requirement_is_control_fact_even_when_other_feedback_is_omitted`；[test_decision_purpose_capacity.py](../../tests/unit/test_decision_purpose_capacity.py) 的 `test_planning_schema_transport_and_raw_array_sidecar_report_roundtrip`；[test_phase_b_model_cases.py](../../tests/unit/test_phase_b_model_cases.py) 的 `test_v07_all_requested_reads_precede_completion_within_frozen_model_budget` |
| 采样的实际坐标、能量、缺成员与条件来源必须保留 | [test_context.py](../../tests/unit/test_context.py) 的 `test_sampling_analysis_projection_keeps_every_actual_coordinate_energy_and_gap`、`test_actual_joint_sampling_ids_four_sp_and_two_analysis_results_fit_without_dropping_feedback`；[test_delivery_snapshot.py](../../tests/unit/test_delivery_snapshot.py)、[test_sampling_check_intent.py](../../tests/unit/test_sampling_check_intent.py) |
| 当前条件与来源条件不能混用；原始观察单位未知不能猜测 | [test_context_fact_contract.py](../../tests/unit/test_context_fact_contract.py) 的 `test_scoped_condition_difference_uses_scientific_resolver_without_rewriting_evidence`、`test_unconfirmed_system_origin_is_unknown_and_canonical_equivalence_is_not_a_difference`、`test_unstated_observation_units_stay_unknown_in_execution_and_final_context`；[test_current_applicability.py](../../tests/unit/test_current_applicability.py) |
| 导入与只读的执行影响必须绑定实际调用 | [test_context_execution_facts.py](../../tests/unit/test_context_execution_facts.py) 的 `test_settled_effects_visible_even_without_catalog_and_without_plan` 和七类 `test_execution_effects_never_borrow_an_unbound_call` 反例；[test_phase_b_model_cases.py](../../tests/unit/test_phase_b_model_cases.py) 的 `test_import_then_actual_stdout_read_and_explanation_fit_three_offline_rounds` |
| 命名目标、结构与能量双目标、登记/待答问题、sentinel 原子退休 | [test_semantic_goal_binding_production.py](../../tests/unit/test_semantic_goal_binding_production.py)；[test_semantic_scope_notices.py](../../tests/unit/test_semantic_scope_notices.py) 的 `test_developer_scope_notices_keep_requested_targets_and_all_unmet_facts`；[test_semantic_placeholder.py](../../tests/unit/test_semantic_placeholder.py) 的 `test_raw_clarification_resume_retires_sentinel_atomically_but_keeps_unanswered_question`；[test_semantic_registration_policy.py](../../tests/unit/test_semantic_registration_policy.py) 的 `test_registration_notice_does_not_suppress_real_unknowns_or_grant_execution` |
| 旧规则不能自动升级；正常恢复必须保留每个文件、历史规则和成本 | [test_analysis.py](../../tests/unit/test_analysis.py) 的 `old_rule` 参数反例；[test_optimization_final_stage.py](../../tests/unit/test_optimization_final_stage.py) 的 `test_old_optimization_checks_do_not_acquire_new_local_rule`；新 [test_phase_b_archive_portability.py](../../tests/unit/test_phase_b_archive_portability.py) 的 `test_synthetic_legacy_restore_keeps_every_byte_rule_and_cost[water_sp-False/methane_opt-True]` |
| 接受原子性、源 hash 变化、崩溃恢复与旧 stop 兼容 | 新 [test_terminal_contract.py](../../tests/unit/test_terminal_contract.py)、[test_terminal_recovery.py](../../tests/unit/test_terminal_recovery.py)，包含接受前来源变化拒绝、接受后历史收据保留、报告篡改和四个崩溃点 |

上表真实派生小 fixture 的来源与 hash 保存在各自 `provenance.json`，包括 `v06-feedback-context`、`v06-terminal-outcome`、`v06-delivery-capacity`；语义原答小 fixture 保留原文。合成模型响应和新构造的成功轨迹始终标为离线，不记作真实模型或 ORCA 通过。

## 本次补齐与剩余边界

只读检查发现 `_legacy_reference` 的成功复制路径原先只有外部 Phase A 档案正例，便携测试仅验证外部 checkout 拒绝。新独立测试在临时目录中构造明确合成的 legacy Store、receipt 和 index，分别覆盖 SP 初始几何与 Opt 终态几何选择；检查所有 manifest 文件源/目标字节与 hash 相同、重复恢复幂等、旧规则与原计费哨兵不变、当前新 Run 没有预约或调用。合成数值和用量只服务于复制断言，不是历史科学事实。

本次核对后，未发现上述 49 项所承载的关键产品行为只能由缺档案 skip 证明。仍未便携的是其**指定历史档案本身**的完整审计；保留为外部审计回放即可，不声称已在干净 checkout 中运行。新测试的定向结果及当前候选最终数量分别以当次测试输出和全量验证记录为准。
