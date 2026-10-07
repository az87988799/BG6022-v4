# 显式、有限的模型推理配置

日期：2026-10-07。范围：同一 DeepSeek 生产调用链的可选诊断配置；默认行为不变。对应蓝图 P05/P06/P14/P15/P18、§10.2、§11；原阶段 B 方案中的 disabled 是首版取舍。

## 问题与取舍

v15 已完整提供当前条件、历史资格、协议 schema 和纠正反馈，真实模型仍有首案事实错误及多余字段。继续同义改写提示没有充分依据；JSON object 模式只约束 JSON 语法，不保证符合项目 Proposal schema。开发门槛继续失败。

增加一个由程序配置选择的 `thinking_low` 诊断 profile，与既有 `disabled` 共用 SDK、预约、发送、解析和持久化责任链。默认 disabled；没有自动 fallback，模型提案不能修改 profile，同一 Run 的已冻结模式不能在恢复中改变。配置选择本身不授予额外 Run、模型预算或 ORCA 权限。

暂不迁移 Responses API 或 Beta strict Tool calls；它们需要额外 schema 投影、传输/回执验证，不能仅换 URL 或静默弱化现有字段约束。通知与问题共用 questions 的语义问题另列为未解决项，推理模式不能被宣称一定修复它。

## 执行及信息边界

仅允许 disabled，或 enabled + reasoning_effort=low。保留固定模型、非流式 JSON、SDK 无隐式重试、原超时及 12000/2000 输入/输出上界。thinking 下 temperature 无效，诊断 profile 不宣称维持 temperature=0 的采样约束。请求规范化及 hash 明确绑定模式；默认请求保持历史形状。

最终 content 继续严格校验，不自动删除额外字段或修复 JSON。enabled 响应的 reasoning_content 不保存、不展示、不回传；disabled 收到该字段仍按既有规则拒绝。completion_tokens 包含整个生成输出并按原账本一次结算，不能扣除推理部分或重复计费；未知用量保守占用，超预约硬失败。推理占用同一 2000 上限，截断仍是真实失败。

## 输入上界依据

固定官方 `deepseek-ai/deepseek-recipe` 提交 `8cadfede7063c896b944e7bae05daa3549ae97ea`，tokenizer SHA256 `81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99`。源码路径为 `deepseek-recipe-encoding/src/v4/mod.rs` 和 `dsv41.rs`。公开 renderer 的 enabled+low 强度说明为 91 UTF-8 bytes；生成前缀少 1 byte；JSON 说明保持 105 bytes。两消息 System+User 的包装为 275 bytes，小于既有 640；允许的文本角色每消息最多 59 bytes，小于 64；计入自动首 System 的固定部分最多 263 bytes，小于 512。

这是固定公开实现支持的保守界，不能证明线上服务端逐字采用该版本。模式不提高预约上限，保留实际 usage 超界检测与未知占用。来源：[固定编码源码](https://github.com/deepseek-ai/deepseek-recipe/blob/8cadfede7063c896b944e7bae05daa3549ae97ea/deepseek-recipe-encoding/src/v4/mod.rs)、[强度编码](https://github.com/deepseek-ai/deepseek-recipe/blob/8cadfede7063c896b944e7bae05daa3549ae97ea/deepseek-recipe-encoding/src/v4/dsv41.rs)、[官方思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。

## 验收与状态

必须覆盖双模式实际 SDK 请求体、非法模式/请求篡改拒绝、恢复模式绑定、reasoning 不泄露、disabled 行为不变、完整 completion 用量、截断/未知/超界以及最重上下文。旧证据和失败不重写。完成离线实现后只能称“可选配置已实现、真实效果未验证”。

已批准的 v15 三个追加 Run 均已使用；本决定不自动启动诊断。新的真实范围必须另有明确次数和成本上限，仍接受全提案事实/语义及最终六轴独立审查；成功前不启动后八槽、科学开发或正式验收。
