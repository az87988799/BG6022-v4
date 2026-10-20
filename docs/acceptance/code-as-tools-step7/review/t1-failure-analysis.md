# T1 首次正式运行的直接 ORCA 启动前失败分析

分析范围：只读检查冻结源码、T1 原始 Result、Attempt 和步骤 7 driver；未修改产品、测试或运行记录，未启动计算，也未重试 T1。

结论：T1 触发了冻结产品内部的准入版本不一致。`managed-native-1` 输入检查产生 `native-orca-input-3`，但直接 `orca.run` 的工作量预留函数仍只接受 `native-orca-input-2`。该拒绝发生于 `launch_orca` 调用之前。现有证据不支持把本次失败归因于 driver 恢复 `LOCALAPPDATA`。

## 可直接核验的事实

本次 Run 为 `run_8227464d553b466e958a29af147d8904`，Attempt 为 `attempt_9d138ff134854f58b5f93a51ce3c232b`，Result 为 `result_a6a8cbc474434a01ae41a378fb205206`。

下表中的 `T1RUN` 指仓库相对路径：

`data/code-tools-formal-batch/workspaces/formal-T1-001/data/runs/run_8227464d553b466e958a29af147d8904`

`ATTEMPT` 指 `T1RUN/steps/step_b4b0786129c648748e2bac9717a7829a/attempt-001`。

| 原始证据 | 可核验事实 |
| --- | --- |
| `docs/acceptance/code-as-tools-step6/product-freeze-event.json:3` | 冻结产品提交为 `5e6e9e38c7e24a10180d474bf95068692b7f9cca`。 |
| `data/code-tools-formal-batch/first-repetition-evidence/T1/pre-verify.json:11` 与 `post-verify.json:11` | 运行前后冻结检查均以退出码 0 返回；`stdout` 内 `status=matched`，`code_commit` 均为上述冻结提交，运行时重新观测通过。 |
| `T1RUN/configuration.json:28` | 本次冻结 `native_input_policy.profile=managed-native-1`。 |
| `ATTEMPT/intent.json:8` | 预留时 Tool 为 `orca.run`；相邻字段 `state=intent`、`execution_kind=orca`、`finished_at=null`、`started=false`。 |
| `ATTEMPT/prepared.json:22` | 已完成的输入准入记录明确为 `admission_version=native-orca-input-3`；下一字段保留 `managed-native-1` 策略。 |
| `ATTEMPT/prepared.json:4` | 准入记录的输入 SHA-256 为 `cd6ad2c925bd8d233a360b46fe79e18087bcb86759467acd26db1d71058e70b9`，与原始 Attempt 的 `frozen_step.parameters.input.sha256` 一致。 |
| `ATTEMPT/execution.json:2` | `state=failed`，原文诊断为 `native workload requires its admitted, unstarted direct Attempt`；`not_started=true`、`handle=null`、`exit_code=null`。 |
| `T1RUN/results/result_a6a8cbc474434a01ae41a378fb205206.json:8` | `operation_status=failed`，`execution_completed=false`，`scientific_validation=unverified`，无合格科学输出，保留上述原文诊断及输入、intent、prepared、execution 的 Artifact 引用。 |
| `data/code-tools-formal-batch/first-repetition-evidence/T1/outcome.json:12` | `internal_work_units=0`、`orca_starts_reserved=1`、`orca_starts_actual=0`；driver `error=null`，不是 driver 向外抛出的执行异常。 |

原始 `prepared.json` 另有 `execution_impact.internal_work_units=157`，表示输入声明的工作量；`outcome.json` 的累计实际预留工作量仍为 0。二者属于不同记录，不能把 157 写成本次已执行工作量。

## 冻结源码的调用顺序

1. `src/orca_agent/tools/managed.py:862` 调用 `prepare_native_input(...)`，传入 Run 冻结策略和输入绑定，并于第 865 行先保存 `prepared.json`。
2. `src/orca_agent/orca/native.py:325` 将输入交给 `validate_native_input`。该函数在第 176–180 行依据 `managed-native-1` 调用 `validate_managed_input`。
3. `src/orca_agent/orca/native_managed.py:16` 定义 `ADMISSION_VERSION = "native-orca-input-3"`，第 498 行将其写入返回的准入记录。本次 `prepared.json:22` 直接证明实际走到了这一版本。
4. `src/orca_agent/tools/managed.py:867` 在设置 `invoked=True` 以及第 869 行调用 `launch_orca(...)` 之前，调用 `store.reserve_native_workload(run, attempt, prepared)`。
5. `src/orca_agent/store.py:1008` 的拒绝条件硬编码为 `prepared.get("admission_version") != "native-orca-input-2"`。对于已经保存的 `"native-orca-input-3"`，该条件必为真，第 1011 行抛出本次原文 `StoreError`。此版本差异本身足以触发失败，无需假定其他 OR 条件也失败。
6. 工作量检查与写入位于 `store.py:1024` 及第 1029–1030 行，均在上述拒绝之后。因此本次没有写入 `Attempt.native_workload`，也没有累计该 157 单位工作量。
7. `src/orca_agent/tools/managed.py:922` 捕获异常。由于第 868 行的 `invoked=True` 尚未执行，记录 `failed` 与 `not_started=true`，随后保存 `execution.json` 并收集失败 Result。

已执行以下只读差异检查，退出码 0、差异为空：

```text
git diff --exit-code 5e6e9e38c7e24a10180d474bf95068692b7f9cca -- src/orca_agent/store.py src/orca_agent/tools/managed.py src/orca_agent/orca/native.py src/orca_agent/orca/native_profile.py src/orca_agent/orca/native_managed.py
```

同时直接查看冻结提交中的 `src/orca_agent/store.py`，其相同守卫仍只接受 `native-orca-input-2`。因此，这个不一致已存在于冻结产品提交，不是步骤 7 工作区临时改动。

## 对 driver 环境恢复的判断

事实：`docs/acceptance/code-as-tools-step7/run_first_repetition.py:179–182` 从冻结事件读取既有环境目录，再恢复 `LOCALAPPDATA`。第 104 行要求 `default_environment_root()` 必须等于 manifest 的环境目录；第 106 行构造 Store，第 125 行调用产品的 `execute(...)`。`src/orca_agent/store.py:162–165` 使用 `LOCALAPPDATA` 计算默认环境目录。driver 没有改写准入版本、准入策略或 `reserve_native_workload` 的判断。

推论：此次异常的充分触发条件是两处冻结产品代码对准入版本的约定不一致；它与环境目录内容无关。环境租约核对在 `store.py:1016` 才进行，位于已经触发的版本拒绝之后，因此这条异常不是租约路径不匹配产生的错误。本分析没有以其他计算排除所有可能的环境问题，也不宣称运行环境不存在任何其他限制；仅确定本次已观察失败的直接原因。

## 影响和边界

静态推论：在该冻结版本中，走同一路径的直接 `orca.run`、且使用 `managed-native-1` 的任务，会在同一准入版本守卫处被拒绝。该结论不自动扩展到任务 Python 内部的 SDK 调用路径，因为内部调用使用不同的预留入口。

本次事实仅支持：真实模型执行与初始结构准备已经发生，直接 ORCA 启动预留已发生，ORCA 实际启动为 0；优化和频率目标未满足。后续修复必须作为独立产品变更处理，不能改写本次失败事实，也不能在本批冻结运行中暗中修复或重跑。
