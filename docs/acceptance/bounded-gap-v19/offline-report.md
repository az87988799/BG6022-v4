# v19 干净离线验证

2026-10-08（Asia/Hong_Kong），精确候选 `468254b005b8be5df8f29da269867e3c514261cb` 的完整默认离线结果为 **2364 passed / 193 skipped / 0 failed / 0 errors**，共 2557 项；Ruff 全库通过。JUnit 耗时 699.773 秒，包含启动与收尾的实际耗时 701.578 秒。本次零真实模型、PubChem、OPI 生成或 ORCA 调用，**等待用户验收**。

在全新本地 clone 中 detached checkout，未复制开发者 `data/`。复用 `E:\BG6022-V4\.venv\Scripts\python.exe`，cwd 固定为新检出、`PYTHONPATH` 精确指向其 `src`；38 个 tracked Python 模块逐一真实 import，`__file__` 均为对应新文件。测试前后源码 hash 一致，结束时 Git 工作树干净、无 `data/`。依赖为 Python 3.11.4、orca-pi 2.0.0、RDKit 2025.9.6、pytest 9.1.1、Ruff 0.16.10。

| 固定项 | 值 |
| --- | --- |
| Git tree | `d9c548128cfec829076616241a137fd6375f4f2c` |
| 生产源码 manifest SHA-256 | `c5a054052868cf5554c5ca6f1896d1b46de23244d07b9fdf0913204b11da4935` |
| `uv.lock` SHA-256 | `616f5e8155ca5522ca7cc488f91ef00a5c73db46e59c6b0497c85db8e56e885b` |
| JUnit SHA-256 | `b1cf22e71074278f70bfc3e9da0c547ba9472bd6d4fe60bf7f3021b316953ec1` |
| pytest stdout SHA-256 | `0e2ceb817a89140c1d5e9610b33f1c91ea3303f121eeb343b8d9e34799ec907a` |

命令为 `python -m ruff check . --no-cache` 及 `python -u -m pytest -q --junitxml <log-root>/pytest-junit.xml`。没有 live 开关或 maxfail，整套一次自然结束，不修改检出或重跑个别失败。完整退出码、38 个导入路径、逐源文件 hash、原始日志 hash 和 12 条 skip 原因见 [offline-validation.json](offline-validation.json)。JSON 证据通过本目录 `.gitattributes` 的 `*.json -text` 保留跨平台原始字节。

193 项跳过为真实模型未启用 130、真实 ORCA 未启用 13、本地历史档案缺失 49、Windows 测试符号链接权限不足 1；这些项未验证，不计为通过。

本次全量包含新增 V06 实际失败回放 **34 项通过**、续验门槛 **42 项通过**。v19 回放从 12372 降至最多 11983，冻结输入上限仍为 12000；测试覆盖结果生成时刻及截止前的不同时间宽度，保留 AUTHORITY、DATA、Tool/check_contract 与失败状态。闭合八字段 schema 的两 action 分支穷尽且互斥，改写仅压缩冗余表示。fixture 只读已跟踪的输入和 provenance，不读取开发者 data；其原始来源 hash 可审计。此回放不是新的模型成功。

续验测试覆盖旧包关闭、低层入口、未固定审批 pin 的拒绝、候选 SHA 绑定、前序同候选独立 review、原账本和未知费用边界。候选中的新审批 pin 仍为 None；本次没有应用新限额、冻结真实续验候选或发起真实调用。

此前记录完整保留：920 首次 harness 参数错误及后续中断的 1 个 F（具体断言未知）；094 缺少开发 data 的失败；508 两个旧可靠性脚本失败；8a2 的 2288/193 完整通过。精简历史、日志位置/hash 与修复说明保存在本 JSON 及[旧离线记录](../bounded-gap-final/offline-validation.json)，不被本次通过覆盖。真实 V06 失败仍见[原包报告](../bounded-gap-final/report.md)。

本轮原始日志根的精确 JSON 路径为：

```json
"C:\\WINDOWS\uff3cTEMP\\orca-clean-468254b-213d67b209fa455784246f927bb3321b"
```

U+FF3C 是路径内的全角反斜线。原始 stdout/stderr/JUnit 留在该本地临时目录，仓库仅存精简事实及 hash；不把临时路径当作永久跨机器档案。离线通过不证明真实模型解释、科学闭环、阶段 B 完成或用户验收通过。
