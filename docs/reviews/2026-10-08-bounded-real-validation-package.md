# 后续批次真实验证最小包（待明确预算批准）

本文件是执行提案，不是执行许可。用户“完成所有后续批次”已授权实现、离线验证和准备新验证范围；原累计数值上限仍有效。未收到明确增补批准、记录批准文件 hash 并应用到原账本前，不执行本包的真实模型、PubChem、OPI 生成或 ORCA。本包 helper 正在完成离线验证，不声称已有真实通过证据。

## 固定范围与累计上限

保留原 B 阶段 C、条件 D、formal 剩余范围及其分项上限；不借用这些额度支付新代表任务。沿用唯一 `data/phase-b/batch-ledger.json`，保留每次已用/未知成本和旧批准 receipt，不创建第二预算账本。2026-10-08 审阅基线：已用模型 318 HTTP、993584 tokens、USD 0.3941835；ORCA reference 16、development 26、formal 0，共 42。

| 累计类别 | 现行 | 提议 | 增量 |
| --- | ---: | ---: | ---: |
| 模型 HTTP | 1068 | 1118 | 50 |
| 模型 tokens | 6590000 | 6881584 | 291584 |
| 模型美元 | 10 | 10 | 0 |
| ORCA reference | 16 | 17 | 1 |
| ORCA development | 48 | 54 | 6 |
| ORCA formal | 48 | 48 | 0 |
| ORCA 总计 | 112 | 119 | 7 |

算式：模型余量需覆盖本包 112 HTTP / 800000 tokens，加原 C 48 / 288000、条件 D 16 / 96000、formal 624 / 4704000；加已用 318 / 993584，得到 1118 / 6881584。此包 112 次包括原剩余八项 raw 的 44 次；不是再给旧八项一套重复机会。实际 usage 低于上限不授权追加任务或新 Run 重试。若批准前基线增长，重新核算并核对 scope，不静默挤占旧分项。

## 逐层固定槽位与停止门槛

候选源码、schema、prompt、依赖、配置、模型 profile 与预算冻结一次。新包使用 `disabled` 模型模式，每次 input 上限 12000 tokens、completion 上限 2000 tokens；固定 Run，保留首次失败和至多一次已有协议纠正，不因换 ID、暂停、恢复增加机会。旧 v16 thinking_low 的失败 N-06 仍保留，不计入新包通过。

1. 五项前置零 ORCA 模型诊断各一次、各至多 4 HTTP / 32000 tokens，总 20 / 160000：
   - `N-06/raw-unsupported-system`
   - `V-06/insufficient-additional-budget`
   - `V-07/array-location`
   - `V-09/different-method`
   - `V-09/missing-electron-state`
2. 上述五项通过独立实际输出复核后，执行原剩余八项 raw 各一次，总 44 HTTP / 352000 tokens：
   - `N-03/raw-electron-state-clarification`：6 / 48000。
   - `N-03/raw-ambiguous-reference`：6 / 48000。
   - `N-04/raw-authorized-defaults`：4 / 32000。
   - `N-04/raw-unique-inheritance`：6 / 48000。
   - `N-04/raw-unconfirmed-inference`：4 / 32000。
   - `N-07/raw-read-only-window`：6 / 48000。
   - `N-09/raw-user-goal-replacement`：6 / 48000。
   - `N-09/raw-preserve-goal`：6 / 48000。
3. 全部模型门槛通过后，PubChem 水、甲烷各查询一次，总 2 次，生产 Tool 显式许可；每 Run 查询上限 1，失败不自动重试。仅固定官方 property endpoint，响应最多 256 KiB；connect 5 秒、单次读写 10 秒、stream 检查软 deadline 20 秒（最后阻塞读可延至约 30 秒），环境节流锁等待最多 30 秒、额外间隔等待最多 2 秒，记录实耗。保留原始响应与身份检查，命中名字本身不代表优化几何。
4. 两个身份结果均通过后，经生产 `structure.prepare` 为水和甲烷各准备一次，总 2 次；每 Run 准备上限 1，每次 1 核 / 1024 MiB / 30 秒，环境同时最多一个准备或科学进程；不启动 ORCA。固定 OPI 2.0.0 与 RDKit 2025.9.6。每种生成结果冻结一次 XYZ/hash/来源，后续三轨迹复用。生成失败、未知进程/费用、hash 或权限不符即停，不通过新 Run 再生成。
5. 用**该次冻结甲烷 XYZ**新增 1 次独立 SP reference：独立预写 RHF/STO-3G 输入和 raw stdout 读取；4 核、总 1024 MiB、MaxCore 192 MB/进程、计算超时 120 秒，无 JSON 后处理。独立参考不来自产品 Result。旧甲烷 Opt 不可代替新 XYZ 的 SP reference。
6. 两类代表任务各三次独立真实模型＋ORCA轨迹，总 6 次 development ORCA，每轨迹最多 8 HTTP / 48000 tokens，合计 48 / 288000；每轨迹 ORCA 上限 1，无额外科学重试，无重新生成：
   - 水：准备初始 XYZ → 严格 Opt → 合格优化结构和优化后电子能。
   - 甲烷：冻结 prepared XYZ → 固定几何 SP → 合格电子能，明确不得称为优化结构。

每项独立复核实际模型提议、六轴解释、程序 Check、来源和成本；程序的 `goal_complete` 不能代替独立验收。共同协议失败立即停止受影响包，不用后续科学槽尝试修复；源码/profile/预算改变需要新冻结及范围决定。失败不抹除，不自动追加机会。现有 ORCA 单次资源上限继续有效（科学代表默认 timeout 300 秒；每 Run 总时限最多 1800 秒）。

## 独立参考与可证明范围

水 Opt 可复用历史独立参考 `data/acceptance/reference-water_opt.json`（SHA256 `01f0124270697f2df3d1509a07fafc8c9cff97eff5849b5d39564c5afdbc5a3f`）：新结果必须满足当前 `optimization-final-stage-1` 严格最终阶段检查，再与该独立终点能量比较（1e-7 Eh）及按 atom mapping 比较所有原子对距离（1e-5 Å）。它证明相同条件下达到一致终点，不宣称不同初始几何必然收敛到同一最低点；若不匹配就记录失败，不放宽阈值。

每种准备只发生一次；三个重复独立的是模型/科学决策轨迹。缺几何获取层经真实生产 Tool 链执行，由验收 harness 选择 Tool；三条模型轨迹从已自动生成并登记的 immutable XYZ 开始，用户无需手写 XYZ。**本最小包不独立证明模型从纯文本缺几何自动选择 resolve→prepare 的三次真实端到端可靠性**；该整链已有离线模型式动作验证，真实模型选择获取仍须单列未验证。不得把此边界写成全部纯文本全链已验收。

同一 ORCA 引擎/后端的独立输入与读取只提供工程交叉核验，不宣称跨引擎或实验精度。所有原历史 Check/Result 保持原样；新源码验收另存记录。

## 可执行入口与批准记录

`python -m tests.helpers.phase_b_bounded_package` 默认只打印确切 scope，不调用网络、生成或科学执行。源码的 `BOUNDED_APPROVAL_SHA256 = None` 默认拒绝真实执行。批准后在 `docs/acceptance/phase-b/budget-approval-bounded-20261008.json` 记录用户明确回复、该提案 hash、原批准链和 helper 输出的完整 `development_package`，将实际文件 SHA256 固定到 helper 后提交，再冻结干净候选；不得预填 `user_approved`。

入口顺序为 `apply --execute`（只修改原累计上限并留 receipt）、`freeze`（只读环境/文件身份）、`model --variant <固定ID> --execute --live-model`、`resolve --system <water|methane> --execute --live-network`、`prepare --system ... --execute --live-opi`、`reference --execute --live-orca`、`science --system ... --repetition <1|2|3> --execute --live-model --live-orca`。实际模型输出复核由既有 `phase_b_model_evaluation --regrade --review` 完成；未通过前下一槽拒绝执行。harness 使用既有 AcceptanceBudget，不绕过生产执行边界。

用户需要明确批准的是表内新的累计数值及该固定新机会包；`完成所有后续批次` 已覆盖实现和准备工作，但不自行扩张此前数值预算。当前状态：**待预算批准、harness 离线验证中、无新增真实调用、无新验收通过声明**。
