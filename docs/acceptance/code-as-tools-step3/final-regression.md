# 最终回归证据（步骤 3）

第三轮默认全套自然结束：**3765 通过、2 失败、178 跳过**，pytest 报告 2212.53 秒（36 分 52 秒）；该轮没有通过。两个失败都是过期测试字面预期，已在当前候选复现，随后仅修正两行测试预期；四个完整模块的当前回归 **138 项通过**。原全套失败日志、JUnit 和复现红例均保留。

本记录采用“运行期间发生变化的全套 + 当前候选定向回归”，**不声称最终冻结版本已通过完整全套**。详细来源、逐文件 SHA-256、失败 traceback、失败节点的当前闭环和测试快照见 [机器可读记录](validation/final-regression.json)。

## 原两项失败及闭环

| 节点 | 实际原因 | 当前处理与验证 |
| --- | --- | --- |
| `test_context.py::test_actual_proposal_schema_and_bounded_default_context` | 旧测试要求 `agent-json-v35`；全套实际为 v37，当前复现为 v38。 | 仅将当前提示版本预期改为 v38；旧历史 fixture 不改。完整 schema、权限及容量断言保留，当前绿色门禁通过。 |
| `test_semantic_contract.py::test_query_schemas_come_from_registry_and_only_permitted_read_tools` | 旧字符串 `Ports/rules:minimum_evidence_rules` 已同义精简为 `Rules:minimum_evidence_rules`。 | 仅更新这段字面预期；实际注册表来源与允许的查询 schema 断言保留，当前绿色门禁通过。 |

## 各组实际结果

计数逐组报告；组间有重复节点，不相加成新的通过总数。下列名称对应 `data/code-tools-step3/<名称>.xml`，日志同名 `.log`（driver 62 项没有留存该同名日志，JSON 明确记为缺失）。

| 范围 | 结果 | 证据名称 |
| --- | --- | --- |
| 第三轮默认全套 | 3765 通过 / 2 失败 / 178 跳过 | `final-default-003` |
| 当前候选复现原两失败 | 2 失败 | `final-failures-current-001` |
| 修正字面预期后的四完整模块 | 138 通过 | `final-context-protocol-current-002` |
| v38 上下文/计划容量相关四模块 | 104 通过 | `context-v38-081` |
| Artifact / Plan 反馈 | 77 通过 | `plan-artifact-feedback-003` |
| 开发批次预算/driver | 62 通过 | `development-budget-unit-001` |
| OPI XYZ / 身份 / 准备回归 | 129 通过 / 1 跳过 | `opi-trailing-xyz-unit-001` |
| 独立真实受管理 OPI worker | 1 通过 | `opi-trailing-xyz-managed-001` |
| 共享 ORCA 权限/预算初始红例 | 3 通过 / 9 失败 | `shared-orca-budget-red` |
| 共享预算首轮窄回归 | 63 通过 | `shared-orca-budget-unit-001` |
| 共享预算中间广回归 | 244 通过 / 6 失败 / 1 跳过 | `shared-orca-budget-final` |
| 合法历史夹具后的定向恢复回归 | 21 通过 | `shared-orca-budget-recovery-fixed` |
| 共享预算最终九模块回归 | 250 通过 / 1 跳过 | `shared-orca-budget-final-002` |
| OPI→native 衔接红例 | 9 通过 / 4 失败 | `opi-native-admission-red` |
| OPI→native 最终边界回归 | 271 通过 | `opi-native-admission-final` |

当前 138 项门禁覆盖 `test_context`、`test_semantic_contract`、`test_semantic_binding_schema`、`test_context_plan_schema`，源码 62 项前后 SHA-256 完全一致；两条测试的修改没有降低预算或删除 schema/权限检查。相关测试 Ruff 通过。

v38 容量记录另保留 5 个测试的 6 次实际消息构建：12,000 上限的余量分别为 190、221、1443、302、730；授权代码/native 路径为 22,011 / 48,000。消息及限制没有通过提高历史 12k 上限来容纳。此容量证据的 context/decision 两个源码 hash 与当前一致。

## 源码冻结与新增安全修复

默认全套于 2026-10-18 14:51:15 UTC 启动、15:28:09 UTC 结束。其运行期间 **5 个源码文件**变化：

- `context.py`：v38 分阶段 Artifact 计划、同 Attempt SDK 观察来源和冻结 MaxCore 说明。
- `decision_purpose.py`：加入 v38 兼容版本，保留旧版本。
- `proposals.py`：具体 Artifact 引用及部分 Plan 的校验反馈，不自动修补 Plan。
- `structure.py`：XYZ 校验视图允许末尾空白行，原字节保留；有界保留失败准备产物但不授资格。
- `store.py`：修复独立 ORCA / SDK 混合调用的追加科学许可和累计额外预约漏洞。

默认 driver 只记录源码前后 hash，没有记录每个模块导入时间，因此不能断言各测试均运行在启动版本或最终版本；测试文件也不在这 62 个冻结路径中。当前 138 项门禁另记录了相关测试文件 hash。默认收集之后新增的 driver 与预算测试以其独立门禁报告。

共享 ORCA 问题在提交前只读审查中发现，经 9 个离线红例确认，属于实际权限/预算缺陷，**不是第八次真实运行的失败原因**。修复共享追加许可与预算计数，以保留的 Attempt 顺序和 InternalCall 父关联作保守下界；保留原初始多 direct Step 合同，不能关联父尝试的旧内部回执拒绝新预约，原 Run 不回写。

中间广回归的 6 个失败都发生在新增 legacy 夹具以 H2 进入既有 H2O/CH4 校验时，尚未进入预约或恢复。夹具改用合法水结构，未放宽生产白名单；其 6 个节点均在最终九模块 **250 通过 / 1 跳过** 中通过。初始红例和中间失败完整保留。中间 `shared-orca-budget-final` 的 XML 时间 76.609 秒、日志时间 118.43 秒存在未解释差异，两者并列保留；最终 `final-002` 是独立记录（XML 86.027 秒，日志 88.48 秒）。

最终源码与第八次真实运行冻结相比有 `store.py`、`orca/native.py`、`orca/native_profile.py` 三处不同。这些修复只有离线回归，**没有修复后的真实模型/ORCA回归**，也未新增真实运行预约。

## 全套结束后的 OPI→native 衔接修复

原全套的五项源码变化列表保持不变。全套结束后，跨模块复核又发现两个原生校验器仍拒绝 OPI 的真实末尾空行；新增红例为 **9 通过 / 4 失败**。生产入口首先被 XYZ 校验拒绝；profile 的越界异常来自直接离线函数测试，不作为历史 live007 的实际异常。随后仅修改 `orca/native.py`、`orca/native_profile.py`，最终相关边界回归 **271 通过**。因此最终候选相对全套启动源码共七处变化，其中两处发生在全套结束之后；前述 138 项门禁的源码在该次运行内不变，但早于这两处修复。

额外离线验证直接读取已保留的真实 OPI worker 输出，859 字节、SHA-256 `5341a7b8b4268ed330dc280e58a3dcf05b1fd766ee331dc1de2eff7cac9d4257`，检查后原字节/hash 不变。原生校验仅在局部视图剔除尾部空白，profile 只读取已验证的 N 个原子；内部空行、多帧、多余原子、错误数量和非有限坐标仍拒绝。

此探针采用开发者编写的输入及临时条件绑定，只证明保留 XYZ 可经过当前准备与原生准入链，不是自主规划成功或实际 ORCA 启动。没有新结构生成、模型、ORCA 或 Docker 运行。原始探针与源码/test/fixture hash 见 JSON 的 `opi_native_admission_review`。

## 限制与后续判断

默认全套跳过 178 项：真实模型 139、真实容器 22、真实 ORCA 16、Windows 符号链接权限 1；跳过不算通过。全套还有 1 条 Starlette/httpx TestClient 弃用提示。

独立受管理 OPI 验证使用合成身份 fixture，确实运行真实 OPI/Windows Job worker，但不是新的 PubChem 网络证明、ORCA 计算或隔离边界证明。离线通过不代表自然语言科学流程已完成，也不授予生成观察科学资格；真实开发运行的失败另行报告。

**不再启动默认全套 004。** 原两失败已经当前候选红绿闭环，全套期间五处变化及其后两个原生校验器变化均有相关定向证据，共享预算和原生衔接修复各有最终门禁。保留“未在最终冻结候选重跑完整全套”的限制，不把不同版本或重复节点的证据合并成全套通过。
