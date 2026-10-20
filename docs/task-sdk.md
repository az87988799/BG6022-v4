# 任务 Python 的薄 SDK

隔离任务内使用 `orca_task_sdk`。`run_orca`、`checkpoint` 仍使用现行有界 IPC、同一 Run 协调者与累计预算；本页的来源辅助函数不请求计算或读取文件，也不增加任务算法。

## 显式声明收据来源

```python
from orca_task_sdk import receipt_source

# receipt 是已有 run_orca 返回值；先明确选择实际解析的原文件名。
source = receipt_source(receipt, "stdout.out", location="lines 100-120")
record = {
    "name": "energy",
    "value": parsed_energy,  # 脚本从上述文件实际解析的值。
    "unit": "Eh",
    "sources": [source],
    "artifacts": ["energy-table.csv"],  # 仅列出脚本实际生成的任务相对文件。
    "limitations": ["Generated parsing; no registered scientific qualification."],
}
```

`receipt_source(receipt, filename, *, location=None)` 返回现有观察 manifest 的 `{internal_request_key, filename}`，给出 `location` 时另保留该字段。脚本仍显式将记录写入 `observations.json` 的 `{version:1, observations:[...]}`；观察名称必须属于冻结 Step 的 `observation_names`。

- `filename` 必须精确命中 `receipt["files"]` 的原始键；只读副本路径、大小写不同、未知文件或路径归一化请求均拒绝。函数不按后缀、顺序或输入路径猜输出文件。
- 收据键遵循现行 Identifier 格式；只接受 `completed`、`failed`、`timed_out`、`cancelled` 的已结算状态。失败来源可以用于保留诊断，不因此满足要求已完成计算的 Goal。
- 每次调用只声明所选文件。未调用的收据、未选文件以及未声明的产物不会被补入；多来源观察必须逐项显式列出。
- `location` 是最多 1000 字符的声明，不证明已读取该位置。辅助函数也不证明脚本正确解析了值、单位或公式。

任务侧收据是可变数据，辅助函数不是安全边界。现有收集器仍独立按当前 Run 的原收据、原 Attempt 和已归档文件重新绑定 Artifact ID/hash，并核验文件身份。新通用策略通过持久化收据的精确原文件键绑定来源，允许 `first/data.out` 与 `second/data.out` 共存；不按 basename 猜测。已存在的收据文件映射缺项、hash 不符或原键不符时拒绝，不回退到模糊匹配。旧策略及早期没有文件映射的平铺来源保留原兼容范围。实际条件来自可信收据；脚本不能填 `checks`、`status`、`actual_conditions` 或授予科学资格。多个显式来源的实际条件分别保留，只汇总共同条件并报告冲突。原声明格式与手写来源继续可用。

## 部署与验证范围

SDK 仍是单个仅依赖 Python 标准库的文件。Windows 运行库复制 `task_sdk.py` 为 `orca_task_sdk.py`；Linux 构建上下文同样复制该文件进入固定镜像。现有 Linux 环境检查比较 SDK 源码 hash 与冻结镜像 manifest：SDK 更新后，旧镜像会被拒绝，需显式准备并验证新镜像及新环境快照，不能自动升级已有 Run。

已有 Run 的容器对账使用其记录的镜像和句柄身份，不比较当前 SDK 源码，仍可检查历史执行状态。继续执行新的 Attempt 则会再次检查 SDK hash；新源码不能配旧镜像启动。历史 Run 继续执行需使用与其冻结环境匹配的产品版本和配置，不能仅改为新镜像。

显式准备新镜像的现行入口见 [`scripts/container_runtime/README.md`](../scripts/container_runtime/README.md)。下列最小烟测供得到新固定 image ID 后运行，验证 SDK 可导入及来源 helper；它不验证完整隔离、IPC、ORCA 或科学能力。示例不会启动 ORCA、MPI 或模型，不挂载宿主目录或请求拉取镜像：

```powershell
$sdkImageId = 'sha256:<new-image-id>'
$sdkSmoke = @'
import hashlib, json
from pathlib import Path
import orca_task_sdk as sdk
receipt = {"request_key": "offline", "operation_status": "completed",
           "files": {"stdout.out": "/synthetic/readonly/stdout.out"}}
source = sdk.receipt_source(receipt, "stdout.out")
assert source == {"internal_request_key": "offline", "filename": "stdout.out"}
print(json.dumps({"source": source,
                  "sdk_sha256": hashlib.sha256(Path(sdk.__file__).read_bytes()).hexdigest()}))
'@
& 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' --context desktop-linux run `
  --rm --pull never --network none --read-only --cap-drop ALL `
  --security-opt no-new-privileges --user 10001:10001 --workdir /tmp `
  --pids-limit 32 --cpus 1 --memory 128m --memory-swap 128m `
  --entrypoint /opt/orca-agent-runtime/venv/bin/python $sdkImageId -B -I -c $sdkSmoke
```

输出的 `sdk_sha256` 应与新镜像 manifest 及本次产品 SDK 源文件一致。Docker 路径及 context 使用实际显式配置；既有 Run 的 image ID/manifest/环境快照保持原值。

本辅助接口的离线测试使用合成收据经过真实文件归档与观察收集路径：来源 hash 与手写声明一致，未声明来源不补入，不存在的收据、虚构文件或未结算来源由收集器拒绝。

2026-10-20 已复用缓存 wheelhouse 和固定本地基础镜像离线构建新版 SDK 镜像，并通过真实容器内纯 Python 导入、显式来源声明、错误文件/未知状态拒绝及 SDK hash 烟测。`inspect_environment` 在本地配置更新前后均通过；仅更新 gitignored `config.local.toml` 的新 Run 镜像与 manifest 身份，其余配置、历史 Run 快照、旧镜像及卷保留。详见[构建与烟测摘要](acceptance/model-context-efficiency/sdk-runtime.json)，完整构建日志留在忽略的 `data/model-context-efficiency/sdk-runtime-build-001`。

本次没有网络下载、真实模型、ORCA 或 MPI 启动；纯 Python 烟测不构成完整任务隔离、SDK 计算链或科学验收。新版本的这些真实验证仍未开展。

上述为上下文优化交付时点。随后合并核验已同步两个 Linux 示例的 SDK 镜像，并在该镜像执行四核 SDK→ORCA、`receipt_source` 观察归档及真实隔离探针；范围、成本和限制见[合并与步骤 6 前置检查](acceptance/code-as-tools-merge-readiness/report.md)。这些开发者探针不计作真实模型自主任务或步骤 7 正式验收。
