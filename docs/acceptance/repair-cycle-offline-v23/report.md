# v23 干净离线验证

源码 `65947b1043526be1ae877b563fe4f2fd7608ab5b`，提示 `agent-json-v23`。完整独立干净 checkout 验证为 **2785 passed、202 skipped、0 failures、0 errors**，共 2987 项；pytest、Ruff 退出码均为 0。未复制 ignored data 或原虚拟环境，使用锁文件离线安装；验证前后 tracked 字节清单相同且 checkout 干净。

[原 receipt](receipt.json) 为逐字节副本，SHA256 `07d3bdff716d2ba11e85c718698f439c760385669e244f76f84881dfa7ec3e94`。[精简摘要](summary.json) 逐项核对原 JUnit 与日志，保存原始路径、大小和 SHA256；未复制大型日志/JUnit/文件清单。[环境对照](runtime-version-comparison.json) 保留原字节。

202 项跳过分为真实模型未启用 139、真实 ORCA 未启用 13、缺少本地历史档案 49、Windows symlink 权限 1。它们仍是未验证，不能计入通过；核心便携覆盖与外部档案分类见[既有说明](../../reviews/2026-10-08-repair-cycle-skip-classification.md)。

耗时分别为 pytest 文本 **1231.73 s**、JUnit suite **1231.411 s**、pytest 子进程 **1235.312 s**，不混为同一时钟。定向 383 项及独立 13+2、守卫回归属于另外的重叠证据，均不与全量数相加。[定向索引原件](targeted-index-at-collection.json) 在全量运行期间生成，其 pending 字段仅记录当时状态。

已测 E2E 单请求最紧输入界限 11979/12000，余 21；SC 纠正反例达到 11994，随后因剩余最终解释预算停止。SC 三条正常链完整预约和为 37867、37843、37968，均在各自 48000 内。E2E 完整合成链的输入/输出界限和为 59622–79294，实际模型用量仍未知；合成结算不证明真实全链必能在 48000 tokens 内完成。

本目录只证明该提交的离线验证，不宣称真实模型或 ORCA 成功。[D2 实际门槛](../repair-cycle-development-2/report.md) 随后记录两项失败；分层科学、自主 E2E、C、条件 D 及正式验收仍未完成。
