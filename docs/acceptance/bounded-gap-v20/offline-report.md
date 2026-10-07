# v20 干净 Windows 离线验证

候选提交：`329e595a70eb60f41607da99d052ec00319c832d`；tree：`8d815f0a5b6df19f963704ddadd95f23a1bee9dc`。本报告只记录离线结果，等待用户验收。

对该提交建立全新、无本地 data 的独立检出，以共享 `.venv` 依赖运行；cwd 和 PYTHONPATH 精确指向新检出。38 个生产模块逐一导入并验证实际文件位置，源码及依赖锁 hash 前后不变，结束时仍无 data 且 git clean。源码 manifest SHA256 为 `5c3525859d971466c57d743510d4d684e1caf88cbd4a0da3969ae7f2a12b12f4`；锁文件 SHA256 为 `616f5e8155ca5522ca7cc488f91ef00a5c73db46e59c6b0497c85db8e56e885b`。

完整默认 pytest 单次自然结束，无 maxfail、无自动重跑：**2444 passed / 193 skipped / 0 failed / 0 errors**，退出码 0，耗时 722.578 秒。Ruff 退出码 0，耗时 0.610 秒。

跳过分类：real_model_disabled 130；real_orca_disabled 13；missing_local_archives 49；windows_symlink_privilege 1。具体原因、全部失败明细（如有）、38 个导入路径和日志哈希见 [结构化记录](offline-validation.json)。跳过项不计作通过。

此前定向验证：语义/context/protocol 482 项（最后 replacement 修复前），最终四个语义文件 96 项，helper 两组 114 与 118 项。独立生产路径反例复验为五例能量覆盖、四例否定撤回以及最终六例 replacement/N09。各组相互重叠；独立生产路径反例也不当作额外 pytest 用例，不能相加当作独立测试总数。定向审查发现的遗漏、误拒和替换授权旁路已在本候选修复；可核对结构化记录中原反例和修复后结果。

原 [v19 离线历史](../bounded-gap-v19/offline-report.md) 保留了更早失败、中断和修复记录；原 [r2 实际记录](../bounded-gap-r2/real-validation.json) 的 N06 独立语义失败及费用保持不变。本次没有新增模型 HTTP、PubChem、OPI 生成或 ORCA 启动，不能据此宣称真实包或阶段 B 完成。

另有独立只读审计核对 68 个历史证据路径 hash、旧账本与 r3 未执行状态，以及提案和 N06 fixture 的 Git/工作区/关闭 autocrlf 检出字节一致性。该审计用于证据保留与准备状态，不代替全量测试结论。

原始 stdout/stderr、JUnit 和校验元数据保留于 `C:\WINDOWS＼TEMP\orca-clean-329e595-8nudtx_c`；只提交精简证据，不复制巨型原始日志。
