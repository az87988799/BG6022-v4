# 固定批次操作顺序

本目录是用户授权修复后的新候选复测冻结包。原七题各一次，H1/H2 已暴露，不是新保留任务；下列命令不会自动开始模型或科学计算。辅助程序没有科学求解器，也不为产品填写 Plan、ORCA 输入或任务代码。

## 核验

从仓库根使用冻结的 `.venv\Scripts\python.exe`，每次使用新的 Python 进程：

```powershell
.\.venv\Scripts\python.exe scripts/code_tools_acceptance.py --directory docs/acceptance/code-as-tools-step7-refreeze-1/freeze verify
.\.venv\Scripts\python.exe scripts/code_tools_acceptance.py --directory docs/acceptance/code-as-tools-step7-refreeze-1/freeze verify --inspect-runtime
```

第一条读取已保存运行环境证据，不声称重新检查 Docker；第二条显式读取本地 daemon、已有镜像和版本化 manifest，不拉取镜像、不启动容器或 ORCA。正式开始及结束每一槽前后都要运行第二条，保存输出/hash/时间。产品文件集合、工具/SDK、提示资料、安装依赖、运行环境或验收材料变化均不能继续按原候选计通过。依赖核对是本地完整性检查，不是抵抗恶意管理员同时改写代码、账本和冻结证据的密码学信任根。

`product-freeze.json` 的源提交可早于交付 HEAD；只允许后者新增验收材料。`evaluation-freeze.json` 记录 scripts、tests/helpers、当前验收目录及本步单元测试的精确集合和 hash。它排除自身、report.md、validation.json 三个精确文件以避免循环依赖；这三份说明/结果不得作为执行代码或产品输入。新增辅助文件也会触发不匹配。

## 预算与独立工作区

所有金额是**上限而非预期消耗或启动许可**。`batch-policy.json` 为唯一固定槽表，包含 21 正式槽、21 独立参考槽、18 控制槽，各槽完整 BudgetLimits、原文 hash 与五项累计预约。总上限是各槽预约总和。失败、修复、内部工作及参考重跑都占原槽，不删除失败，不另开成功槽补齐次数。代码/模型细分上限不能在槽之间临时修改。

```powershell
.\.venv\Scripts\python.exe scripts/code_tools_acceptance.py --directory docs/acceptance/code-as-tools-step7-refreeze-1/freeze init-batch --inspect-runtime
.\.venv\Scripts\python.exe scripts/code_tools_acceptance.py --directory docs/acceptance/code-as-tools-step7-refreeze-1/freeze prepare --inspect-runtime --task T1 --repeat 1 --category formal
```

批次位于 freeze-event 的固定 `batch_root`。环境根必须等于生产 `default_environment_root()`；所有正式、参考和控制 Run 共享该全局环境租约，不能单独创建“验收专用环境”绕过其他 Run。初始化只创建零用量账本，prepare 才不可退还地预约一个槽。相同预约不会重复扣款；已准备工作区不重建，恢复使用原文件和 Run。根或槽文件丢失、孤立收据、链接、外部注入文件、换 ID 均停止并保留证据。

每个新工作区只有 `original.txt`、完整派生 `config.json`、`run-budget.json`、`workspace.json` 和空 `data`。派生只改 `data_root` 与预先冻结的 `text.budget`。正式输入由原文、两个 LF 和种子后缀组成；1/2/3 分别使用 1701/1702/1703。提供商不支持模型 seed 时不伪造确定性。

后续显式驱动通过现有 `initialize_text` 创建唯一的初始 Run 壳，取得产品分配的 ID 后立即调用 `code_tools_batch.bind_run(batch_root, slot_id, run_id)`，绑定成功才允许模型或任何执行动作；驱动实例化 `Store(data_root, environment_root=workspace_manifest["environment_root"])`。初始壳只能含冻结原文、配置/许可/预算、空来源及原文初始消息，不能有 Plan、Attempt、工具调用或用量。恢复使用原绑定，不生成新 Run。只能调用冻结生产入口；产品的动态计划、输入和代码必须来自本次真实模型。prepare 并没有启动 Run，也不证明后续使用其他脚本的行为合规。

正式首次运行使用以下固定调用片段；它属于本清单的已冻结驱动约定，仅在步骤 7 获得执行授权后调用。`manifest` 是 prepare 的工作区返回值，`batch_root` 取冻结事件原值。执行前后分别核验冻结并保存完整输出；不得在片段中添加任务算法或替产品填写输入。

```python
from pathlib import Path
from orca_agent.config import Config
from orca_agent.store import Store, default_environment_root
from orca_agent.natural import initialize_text
from orca_agent.entrypoints import execute
from tests.helpers.code_tools_batch import bind_run

workspace = Path(manifest["data_root"]).parent
config = Config.model_validate_json((workspace / "config.json").read_bytes())
assert str(default_environment_root().resolve()) == manifest["environment_root"]
assert not any(config.data_root.iterdir())
store = Store(config.data_root, environment_root=default_environment_root(),
              configuration=config.run_configuration())
text = (workspace / "original.txt").read_bytes().decode("utf-8")
run = initialize_text(store, config, text)
bind_run(batch_root, manifest["slot_id"], run.id)
result = execute(store, config, run.id)
```

已有壳或中断时先核对唯一 Run 并完成原绑定，不重复 initialize；恢复用账本原 ID 调用 `execute(store, config, run_id, resume=True)`。产品 `run.batch_category` 保持 `None`，不向 execute 传本步账本作为 `batch` 参数：该参数属于旧固定协议。本步 formal/reference/control 分类由固定槽账本承担。`orca-agent ask` 会直接初始化并执行，不能用于需要先插入绑定的正式槽；工作区配置为 JSON，按上述接口读取。

每槽结束或异常后调用 `settle_slot(..., known_usage=..., unknown_fields={metric: reason})`，五项全部填写且互斥。只有真实可核对数字才是已知，未知用 null 和理由；账本保守占用完整预约，已知超支也保留且阻止继续。预约不退款，结算不能改写。结算后来取得更多资料时另存对账证据，不覆盖原未知结算。外部供应商实际费用、开发助手 tokens 和采样缺口另报，不从 token 上限推算已付金额。

## 正式任务、参考与控制

公开任务、保留任务、scoring.md 与 controls.json 一起定义退出条件。正式任务七项各三次，每次都需实际模型和 ORCA，开发结果不替代正式重复。没有自动科学评分器；独立评审按逐性质规则读取原始文件，记录 pass/fail/unverified、单位、容差、证据/hash 和理由。缺结果、缺参考或开发者介入均不能记为自主通过。

参考槽仅供独立评审。其文本不是产品问题，不送到模型；模型额度为零。实际科学输入在产品选择方法后，按已冻结参考协议从原始输入/几何派生，启动前单独保存逐行差异、方法/文件 hash、版本和成本预约。参考计算仍走受管执行入口和共享环境租约，不绕过资源/权限，不使用无记录宿主 shell 计算。其数值和脚本留在参考工作区，不能回流到仍运行的产品。

参考初始壳使用 `Store.create_run` 创建原文、无 Plan、`agent_enabled=False` 的 Run，再 `bind_run`。随后独立评审经既有 `enqueue_message`/`natural.apply_user_update` 登记参考条件，经 `tools.dispatch.execute_call` 的 `workspace.write` 保存输入，使用 `store.commit_revision`（含 `model_usage.current_basis`）校验并激活参考 Plan，再走 `entrypoints.execute`。全程保留零模型预算及共享环境根。该链路基于现有接口，本轮没有执行完整真实参考，不能声称已取得数值证据。

控制 case 的脚本化反例与真实模型决策分别报告。控制所需 fixture 在首次执行前登记来源、生成方式和 hash，限于已固定故障/证据类别；不能携带求解答案。与公开任务或保留任务相关的实际结果必须由本批次已计费槽产生。C06A/B 使用完全相同的中性原文、许可和预算，两者都允许最多两个新点；仅原始数值证据不同。独立评审在给模型前固定判据和预期分支，不将“需加密/不需加密”的答案或评分标签放进模型输入，也不通过零预算迫使其中一支停止。不能观察回答后改判据。C10A/C10B 先分别预约，再同时驱动共享环境。

C11 越界探针使用专门创建的无秘密哨兵文件验证拒绝；不读取真实凭据内容。超时、内存、取消、失联和并发必须以真实操作系统边界证明，离线 monkeypatch 仅算离线证据。目标故障未出现记 `not_established`，不能当作控制通过。

单 Run 预算、宿主/容器资源硬边界和批次预约是不同层次。整批预约由受控驱动遵守，辅助账本不能限制绕开驱动的任意管理员进程；每 Attempt 磁盘硬配额及资源采样下界等既有限制继续报告。CPU 4 核、总内存 1024 MB、每进程 MaxCore 192 MB、环境计算并发 1 不变。新的累计上限通过离线校验，任务是否能在上限内完成须留到步骤 7 实测。

步骤 7 新产物和采证索引写入批次 data 根或另建 `docs/acceptance/code-as-tools-step7/`，不在本冻结目录追加可执行辅助或改变规则。实际评审小程序按预定参考协议在参考工作区生成，执行前单独记录 hash，不进入产品任务目录。

## 本次复测限制

原步骤 6 的题目、评分、60 槽上限模板继续冻结，但本次只预约七个 formal repeat=1。其余槽不执行。H1/H2 由用户要求沿用，来源选择字节保存在 prior-holdout-selection.json；holdout-selection.json 的新时序仅表示新候选冻结后确认复用，不能作为新颖性证据。原批次保留，新批次共享原环境根。新成果、运行审查和报告放在本目录外的父目录，不能改变本冻结文件集合。
