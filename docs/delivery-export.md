# 显式导出已有交付

```powershell
.venv\Scripts\orca-agent.exe --config config.local.toml export RUN_ID --output E:\exports\delivery.zip
```

将 `RUN_ID` 替换为已有 Run。目标父目录必须已存在，ZIP 必须是新文件，并位于配置的 `data_root` 和执行环境目录之外。已有同名文件不会覆盖；符号链接、Windows junction、设备名、ADS 和 `..` 路径均拒绝。导出是用户明确要求的新文件写入，`report` 和网页只读 GET 不会隐式生成 ZIP。

导出复用现有 `build_report`，包内包含：

- `report.md`、`report.json`：同一份确定性报告，仅将 Artifact 文件链接改成包内路径。
- `validation.json`：原操作状态、逐输出检查、科学资格、目标完成情况、缺口及限制。
- `manifest.json`：Run 快照 hash、导出界限、每个源 Artifact 的 ID、SHA256、role、来源 Run/Attempt、用途和包内路径；报告和各成员文件的大小及 SHA256。
- `artifacts/`：报告索引中与当前 Run 有关、且既有下载权限允许读取的原字节文件。相同 hash 和大小的源文件共享一个 ZIP 成员，每个 Artifact 身份仍保留在清单中。

所有源文件在读取前、复制时和发布前核对大小及 SHA256，ZIP 成员再次读回核对。任一被索引文件缺失、损坏、越权或超限时整个导出失败，不发布部分包；Run、控制消息或权限在导出期间改变也会拒绝发布，需用户明确重试。科学失败或未验证的结果可以导出，但原 `failed`、`unverified` 等标记保留。导出成功仅证明包完整，不表示科学通过或用户目标完成。

固定上限为 **256 个 ZIP 成员**（包含 4 个说明文件）、**单文件 16 MiB**、**全部未压缩文件 64 MiB**；源 Artifact 数量与读取总量也受同一界限控制，重复源身份仍占源计数和读取字节界限。使用现有报告的 128 个 Result 上限，超出时拒绝导出。压缩后另有 64 MiB 加每成员 512 字节 ZIP 开销的上限。包内路径使用固定目录和保守文件名；不执行或提取所包含的脚本。

ZIP 使用固定时间戳、稳定排序和同一压缩格式；同一软件环境与相同快照重复导出得到相同字节。先写临时归档，再通过同目录无覆盖 hardlink 发布；不支持该操作的文件系统会失败，不降级为覆盖写入。失败会清理本次临时归档。命令返回 ZIP 路径、SHA256、成员数、字节数及报告状态。

这一步不调用模型、ORCA 或任务 Python，不补算、修复来源或修改 Run，不新增运行时领域对象，也不改变已冻结科学执行许可或预算。它只将已有授权文件复制到用户指定的外部位置；实际导出大小在返回值和清单中可查。保留原有报告的文字省略和验证范围，不将包描述成完整科学复核。

离线回归命令：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_delivery_bundle.py tests/unit/test_report_extended.py tests/unit/test_terminal_recovery.py tests/unit/test_web.py -q
.venv\Scripts\python.exe -m ruff check src/orca_agent/delivery_bundle.py src/orca_agent/cli.py tests/unit/test_delivery_bundle.py
```
