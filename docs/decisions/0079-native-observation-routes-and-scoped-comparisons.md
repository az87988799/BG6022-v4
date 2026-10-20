# 原生产物声明与逐来源环境比较

日期：2026-10-21。范围来自 a026 真实执行后交付失败的根因修复授权。

## 问题

a026 两次原生尝试在启动前因输入语法被拒，修正后两次 ORCA 完成，随后 Python 分析完成。绝对能目标仍未完成：原生 Tool 允许 `observation_names=["energy"]` 被当作可交付端口，但实际收集器只归档原文件，不生成命名值。通用观察检查将原生步骤没有 `parameters.code` 与真正的 Attempt 身份错误合并为 `generic_observation_attempt_purpose_mismatch`。此次两条来源的 Request 版本、Result、Step 及 logical 身份均匹配，观察名称没有在修复中丢失。

派生差值另被环境检查拒绝。Request 正确地把气相与水环境分别声明在两个源 Goal，派生 Goal 和全局 Request 没有一个共同的 environment。既有比较入口要求全局来源恰为 `default`，因而把合法的完全未声明当成未知环境。

## 决定

Tool 定义新增单一 `named_observation_collector` 能力：任务 Python 为 `task_manifest`，原生执行为 `none`。既有 `generic_observations` 标志继续控制原权限、输入与执行检查，不改成 false。当前输出声明只对真实 manifest 收集器追加命名观察；工具参数说明、计划路线及纠正反馈都依据同一能力。原生参数的旧观察名称仍能读取，但不承诺生成值。

新通用值目标不能直接绑定只归档原件的执行步骤，包括其 `observations`、`artifacts` 集合端口。显式 Python 分析可读取真实、已固定 hash 的原件，产生与原 Goal 名称一致、有来源和限制的观察，再绑定相应 Goal。原集合仍可作为文件或结构依赖；真正的准备、写文件及其他既有类型化端口保持原契约。不引入按性质预写的解析器或求解器，不把原生完成等同于能量观察。

任务 manifest 的值路线也必须在 `observation_names` 中显式声明名称，包括恰好名为 `observations` 或 `artifacts` 的 Goal；只有原始集合端口并不承诺同名值记录。实际记录仍经原收集、来源和条件检查。

历史 Plan 的读取使用内部验证上下文保留旧声明的结构可读性；新提案、初始计划及提交继续核当前真实能力。旧 Result 不新增观察、不改检查或科学资格。通用观察评估先保留原身份检查，再明确区分缺失任务 manifest，避免把正确的原生身份错误描述成 purpose 不匹配。

环境比较入口仅在全局 environment 的值、来源和条件证据完全不存在时，复用既有比较路线；明确 unknown 或残缺声明不视作缺省，原 default 路径保持。Goal/System 的单一环境约束仍有效。逐来源继续核同 Run、Request、体系、明确源 Goal 的环境、全部环境覆盖、方法/基组/电荷/自旋等不变条件及 fixed_initial 的真实 XYZ 依赖和 hash；生成代码的公式不授予执行或科学资格。

## 验证与历史

只用 a026 已有 Request、Plan、Result 和原件回放，以及命名值/原集合错误路线、历史读取、显式分析路线、身份错配和环境边界的直接反例。原文件前后 hash 核对，不重算、不调用模型，也不运行全量回归。a026 的真实成本、失败状态及既有报告保持原样；新代码的回放不记为该候选历史成功。

路线直接测试 11 项及原有严格科学 Goal 反例 1 项通过；环境直接测试 15 项及原 default 正例 4 项通过。完整历史 Plan 读取保持一致，新提案及提交拒绝其虚假原生数值路线；两条原生结果仍 unresolved，只把理由改为需要 task manifest。路线回放核对 92 份已读原件 hash 未变，证据位于 `data/workflow-root-repair/a026-native-goal-route-after.json`。环境回放在原差值观察上通过有来源交付检查，科学资格仍为 false，核对 211 份原件未变，证据位于 `data/workflow-root-repair/a026-scoped-comparison-replay.json`。

提交的路线 fixture 是 Request/Plan 元数据投影：writer 正文换为非求解占位，原 CodeRef hash、身份和 decision hash 保留用于追溯，不是可执行任务样板。完整实际提案回放仅读取 ignored 原件，未执行其中代码。
