# Code as Tools 步骤 1 交付记录

日期：2026-10-18（Asia/Hong_Kong）。状态：**等待用户验收**。

本次依据用户采用的 [实施方案](../../CODE-AS-TOOLS-IMPLEMENTATION-PLAN.md) 执行步骤 1。起始提交 `b9ea0b4`（方案所述生产代码基线 `14a7bf9`）。本报告与实现处于同一提交，具体提交身份由 Git 确定。未推进步骤 2—7。

## 实现范围与退出条件

- [设计决定 0031](../../decisions/0031-code-as-tools-contract.md)、AGENTS 与唯一总蓝图 2.1 同步。模型生成的任务代码可经后续受管理执行边界运行；只读查询仍不执行代码。
- 方法、基组、电荷/多重度、分子身份、一般目标可在数据契约中表达；区分明确条件、默认、继承、推断、自动选择、实际条件。没有专用检查器不会删除目标或换成邻近物理量。一般方法不自动继承 RHF 电子态。
- 新增代码引用、带用途与 hash 的输入 manifest、内部调用回执和有来源的通用观察，均为现有七对象内的局部记录。初始计划与修订计划共享科学资格约束，明确证据要求不能由普通观察替代。
- 注册 `python.execute`、`orca.run` 的参数、影响及观察声明；当前 `execution_available=false`。可以保存合法代码步骤，但 Agent、调用分派和启动预留入口明确拒绝执行，不进行环境探测、启动或预算占用。
- 配置统一表达 CPU、进程树内存、MaxCore、环境并发、执行/收集时限、模型上下文/响应、累计调用/token、代码重试、内部工作量与产物额度。新 Run 保存不可变配置快照，恢复不读取当前配置替换旧值；额度与累计记录分开。
- 现有执行器按 Run 快照检查资源；产物导入以不可变预留记录核算数量/大小，失败预留不退款。环境并发大于 1 当前明确拒绝，避免未经实现的总量管理被宣称可用。
- 历史 Run 缺少配置字段保持缺失事实；旧产物预算保留原每次收集口径。旧检查版本、Result 指纹及终端交付快照保留，读取不修改原文件或升级科学资格。

旧 `orca.sp` / `orca.opt` 输入与解析仍限定其已验证的 HF/STO-3G 中性单重态责任链。通用方法在本步可表达，但不能经旧适配器被静默改写为 HF，或将实际 HF 输出重新标记为其他方法。一般自然语言入口、真实代码执行和通用交付评估分别留在后续步骤。

## 验证与证据

所有 pytest 命令均使用仓库 `.venv/Scripts/python.exe -m pytest`，未启用 `--live-model`、`--live-orca` 或远程测试。下列组有重叠，不能相加为独立用例总数。

| 测试组 | 最终结果 | 覆盖 |
| --- | --- | --- |
| `test_code_contract_planning.py`、`test_code_contracts.py`、`test_run_configuration.py`、`test_optimization_final_stage.py`、`integration/test_backend_windows.py` | 105 passed | 通用目标与代码步骤、证据保持、输入 hash/路径、旧指纹、配置快照、真实 Windows 执行边界 |
| `test_run_configuration.py`、`test_llm.py`、`test_model_usage.py`、`test_model_profile.py`、`test_model_profile_recovery.py`、`test_store.py`、`test_revision_store.py`、`integration/test_backend_windows.py` | 270 passed，1 skipped | 预算持久化、模型传输/回执、恢复、启动前资源检查、完整本地后端回归 |
| `test_code_contract_planning.py`、`test_code_contracts.py`、`test_run_configuration.py`、`test_registry.py`、`test_planning.py`、`test_delivery_snapshot.py`、`test_terminal_recovery.py` | 175 passed | 工具唯一声明、计划条件保持、交付及恢复兼容 |
| 只读 SP 回归、`test_context_plan_schema.py`、`test_agent.py`、`test_doctor.py` | 73 passed | 现有 Agent、模型计划 schema、环境配置投影 |
| `integration/test_runner.py`、`integration/test_recovery_edges.py`、`integration/test_message_launch_gate.py` | 57 passed | 运行器集成、崩溃/取消/恢复、用户消息到达后的启动阻断 |

改动涉及的所有 Python 文件与新增测试通过 Ruff；暂存差异通过 `git diff --cached --check`。跳过项为 Windows 创建符号链接需要开发者模式/权限；不将其记为通过。早期广域测试未完整结束，不作为整库 CI 通过证据。本次只声明相关定向与必要回归结果。

审查中发现并修复：初始计划遗漏通用观察资格校验；旧解析器在参数放宽后可能把 HF 原始输出标成其他方法；初始 Plan 绕过逻辑步骤额度；新配置字段缺失可能误判为历史 Run；固定产物收集上限与配置重复。对应反例保留在测试中。相关回归还校正了旧测试遗漏既有 dipole 输出，以及模型测试沿用旧顶层协议字段的断言/fixture，生产科学检查未因此放松。

真实执行证据仅为受管 Windows 测试进程：Windows build 26200，Python 3.11.4 x64，宿主可用 CPU affinity 数 16。测试检查了 5 CPU affinity、1536 MiB JobObject 总提交内存配置、96 MiB 分配拒绝、超时/取消与进程树清理；内核配置及子进程 affinity 均实际读取。它们不是 ORCA 计算，也不证明任务文件/网络隔离。

历史科学输出仅用于离线解析回归；`tests/fixtures/code_contracts/legacy_delivery.json` 为标明基线提交的离线契约 fixture，不是新增科学计算结果。代码、输入、输出来源均按原证据处理。

## 未验证与下一步边界

本次真实模型调用数、真实 ORCA 启动数、真实集群调用数均为 **0**。模型 token 成本、真实科学计算成本均为 0；离线模拟回执不计为真实调用。未做新科学能力声明，未更新 `capabilities.md`。

尚未实现/验证任务 Python 的操作系统文件和网络隔离、薄 SDK、通用 ORCA 启动、内部计算预算执行、Python 恢复及跨任务自主能力；`code_retries`、`internal_work_units` 当前仅具备有界配置与累计记录契约。并发大于 1 的配置可表达但拒绝启动。下一步须按方案步骤 2 对隔离与真实最小链路另行实施和验收。

初始未跟踪目录 `docs/acceptance/repair-cycle-development-4/` 保留，不纳入本次提交。
