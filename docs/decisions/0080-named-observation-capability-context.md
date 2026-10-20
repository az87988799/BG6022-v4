# 保留实际收集能力的模型上下文投影

日期：2026-10-21。范围来自 a027 已发送规划请求的只读核查。

0079 已将真实命名观察收集能力集中在 Tool 定义，但 `readable_context` 的 Tool 字段白名单遗漏该能力，同时 `native_schema` 删除所有 description。因此 a027 的两个真实规划请求虽包含 `observation_names` 参数，却没有 `named_observation_collector` 或该参数说明。仅在 registry 中增加字段不足以让模型知道原生收集器只归档原件。

现有可读 Tool 投影仅增加两个值：来自同 Tool 定义的 `named_observation_collector`，以及来自其 `parameter_schema.observation_names.description` 的单个 `output_contract`。不开放整份 check_contract，不重复展开全部 schema 描述，不按工具名称分支。没有此能力的普通 Tool 不增加字段；能力已获授权但参数 schema 被推迟展示的 Tool 仍保留这两个输出事实，同时明确 `deferred; existing validated parameters only`，不提供新的可调用参数或许可。后续压缩及最终发送体必须保留这两项；64K 输入上限、权限、执行、预算和科学检查不变。

a027 继续使用其原不可变 v91 候选；新投影只用于后续候选，不修改已发送模型请求、历史结果或运行状态。验证仅覆盖正常/紧凑投影的最终请求体以及 a027 原规划请求的完整内存重建，核对原件 hash，不调用模型、工具执行或 ORCA。该修复保证信息可见，不据此宣称模型一定会正确规划或科学目标已经完成。

直接验证 3 项通过：正常投影、强制紧凑回退及 deferred schema 均在最终发送体保留能力与原说明，普通 writer 不增加该能力。实际 a027 第四次规划请求内存重建的输入上界为 58,652 / 64,000（原预约 58,040），两个 Tool 的字段与 schema 原说明均可见，权限不变，3 份不可变原件 hash 未变。活跃 Run 只读一次后在内存重建历史 basis；不要求协调者在验证期间停止写其正常运行记录。证据为 `data/workflow-root-repair/a027-capability-context-replay.json`。
