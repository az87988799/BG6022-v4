# 在新开发候选显式选择已提供的 Pro 模型

日期：2026-10-21，依据用户持续真实测试直到原七题跑通的当前授权。

a019 已通过原先错误的计划准入，Flash 仍多次只写并执行注释占位脚本，之后反复返回无效提案；34 次调用时没有任何实际 ORCA。开发者在所有 Python 已结算且无内部科学调用时请求暂停该无效候选，保留在途模型结算、原件和累计成本，不把四个空脚本 completed 当成科学通过。原程序将 `pause/cancel requested before submission` 记为 failed；该最终状态及暂停信号均按原件保留，不改写为 paused。

当前服务只读 `/models` 返回 `deepseek-flash` 与 `deepseek-v4-pro`；[官方模型接口](https://api-docs.deepseek.com/quick_start/pricing/)及[模式说明](https://api-docs.deepseek.com/guides/thinking_mode/)确认两者均支持当前 JSON/非思考调用。后续新候选显式选择既有可配置的 `model.model_name='deepseek-v4-pro'`，保留 disabled 模式、相同源码、原题及物理量、许可与有限资源。此选择不是同一 Run 内自动回退，不重发旧请求，不升级历史模型或将不同模型的证据混称为原 Flash 正式验收。

父候选继续继承完整冻结配置，模型名称只能在新主候选明确选择。总 tokens、实际 ORCA、工作量、时间和 campaign 上限保持；Pro 的单价不同，当前程序没有该模型的冻结费用换算表，美元费用保留未知，实际 prompt/completion tokens 与模型名称准确记录。选择不保证模型正确，也不能替代程序修复、原始科学核对或用户验收。

该候选不向模型注入求解脚本、参考数值或开发者科学解法。仍由真实模型自行编写、执行与修复，逐题审查完整交付及原始证据；不新增生产任务求解器，也不以离线用例代替真实成功。
