# 修复周期 v22 离线验证

提交 `4b79d1839c9fde81e32f52ac0fb6520564bda70a`、提示版本 `agent-json-v22` 的独立干净验证通过：**2752 passed / 202 skipped / 0 failed / 0 errors**，Ruff 通过。这确认离线门禁，P6 真实验收尚未完成，也不表示用户已经验收。

| 验证 | 源码 | 结果 | pytest stdout 耗时 |
| --- | --- | --- | ---: |
| 首轮干净全量 | `5b2c326` | 2743 passed / 202 skipped / 9 failed | 1193.13 秒 |
| 修复后干净全量 | `4b79d18` | 2752 passed / 202 skipped / 0 failed | 1198.65 秒 |
| 本机历史归档独立回放 | `4b79d18` | 49 passed / 0 skipped / 0 failed | 9.67 秒 |

两个干净检出均重新从 `uv.lock` 离线锁定安装，没有复制历史 `data/` 或旧 `.venv`，没有启用真实模型或 ORCA。最终检出保持 clean，tracked 文件前后字节不变。第二轮 JUnit suite 计时为 1166.945 秒，执行器子进程墙钟为 1201.765 秒；与 stdout 耗时分别保存，不混为同一测量。

首轮 9 个失败保留原记录：8 项是编码专用测试中辅助解码器读取缺省系统消息时报 `KeyError: content`；1 项是范围测试引用未提交的早期审批草稿。修复采用缺省空正文和已提交、hash 固定的真实采纳记录，未改科学成功标准；这 9 个节点在最终干净全量中均通过。

## 跳过与额外回放

最终 **202 = 139 真实模型 + 13 真实 ORCA + 49 外部历史档案 + 1 Windows symlink 权限**。与首轮逐节点及原因完全一致；相比 v21 仅增加三个 SC-01 真实模型变体各三次，共 9 项。139 项包括 120 个固定模型槽、18 个模型与科学联合槽及 1 个开发 probe；联合槽也需要 ORCA，未重复计数。

[历史跳过分类](../../reviews/2026-10-08-repair-cycle-skip-classification.md)所列便携核心回归在干净运行中通过。另在主工作区只运行同一批 49 个原档节点，全部通过；1636 个原 Run、Artifact、review、索引及账本文件前后 hash 不变。这是当前代码对本机原档的离线回放，不能将干净检出的 49 skipped 改记为 passed，也不改变历史模型失败的判定。词法路径及 Windows junction 反例通过，真实 symlink 分支仍因权限未执行。

归档回放的包装脚本在测试结束并保存前后 hash 清单后，因 Windows 反斜杠路径键发生 `KeyError`。补录摘要保留包装脚本 exit 1、未持久化的 pytest 子进程数值 exit 为 null；完整 JUnit 与 stdout 均记录 49 passed。没有重复测试或修改原档。

## 矩阵与证据

原矩阵核验记录在 `07da93a` 对 2954 个收集节点核对了 345 个映射节点；这是 collection 证据。归档时另只读核对最终 `4b79d18` JUnit：345 个节点全部存在，其中 207 passed、138 skipped，不能把映射存在称为真实验收。两轮正式 manifest 的 SHA 保存在汇总中。

本目录只复制小型原始记录并保留原字节：

- [首轮失败 receipt](clean-first-receipt.json)：`811a7f53610f912f5848fed83558fae472181a4a2731d239c2b4dbb28c2eb3af`
- [最终通过 receipt](clean-final-receipt.json)：`5acae4a2d1cc20e686484a77c2ab59dd30b877c045d25f1e7362a2b079a606f7`
- [历史回放 receipt](archive-replay-receipt.json)：`4a6c147fc096045d87062d9efab8aebd79fb6ccc666a8e9a3c8396a470034e6d`
- [矩阵核验记录](collection-map-summary.json)：`590de7d9d2c5399bfc871b297f21323c951bfc98230438849111d2ecdde63575`

[summary.json](summary.json)给出各原始本地日志/JUnit 的绝对路径及 hash。最终 [pytest 原始日志](E:/BG6022-V4/data/phase-b-delivery/repair-cycle-clean-cd74796b1b36422e/pytest.log) SHA 为 `0d7831385e128107035b4161bd7d9ce378367083059a18029199772b62aa4b43`，[JUnit](E:/BG6022-V4/data/phase-b-delivery/repair-cycle-clean-cd74796b1b36422e/tests.xml) SHA 为 `16711339223c4b98a68993fd8afae3af48bc2f2b69d138c2a507382d2c6b0839`；不复制大型运行材料。

## 真实执行边界

本次记录新增真实模型 HTTP、PubChem 查询、OPI 结构准备和 ORCA 启动均为 **0**。原周期及正式差额批准尚未应用，未冻结新真实候选；活跃账本仍为 324 HTTP / 1018912 tokens / USD 0.4065609，ORCA reference/development/formal 为 16/26/0，原字节 SHA 为 `b5b0efbfb7b8a805a904b08fbf3d35e7a325809c897bab3c7c68ede2ae039b13`。

后续仍须按已批准周期完成 13 个真实零 ORCA 门槛及独立回答审查、分层科学、自主文本 E2E、C/条件 D、完整正式矩阵与正式 E2E。合成容量测试不证明真实 48000 tokens 一定足够；真实执行继续逐调用预约、结算并遵守停止规则。整体任务等待上述真实证据及用户验收。
