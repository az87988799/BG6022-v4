# 有界真实验证续验包 r4（待用户单独批准）

本文件提出固定执行范围，不是执行许可。r3 的第二槽 V06 因错误宣称目标证据充分且遗漏零追加预算而失败，已按已批准的停机规则结束。新标签为 `bounded-20261008-r4`；准备期间批准 pin 保持 `None`，不创建批准记录、不应用预算、不冻结候选、不调用真实模型、PubChem、OPI 或 ORCA。

## 历史和预算基线

唯一账本仍为 `data/phase-b/batch-ledger.json`，SHA256 为 `b5b0efbfb7b8a805a904b08fbf3d35e7a325809c897bab3c7c68ede2ae039b13`。累计已用 324 HTTP、1018912 tokens、USD 0.4065609，未知 token/费用占用为 0；ORCA reference/development/formal 为 16/26/0，共 42。全部旧记录、批准链、费用和原 C、条件 D、formal 分配保留。

r1 保留 N06 通过、V06 失败及 2 HTTP / 7967 tokens / USD 0.0036726；r2 保留 N06 失败及 1 HTTP / 4256 tokens / USD 0.0024306；r3 保留 N06 通过、V06 失败及 3 HTTP / 13105 tokens / USD 0.0062742。r3 剩余 11 个模型门槛、2 次查询、2 次准备、1 次 reference、6 次科学轨迹全部取消执行机会，仅由本提案的完整新包替代。r1/r2/r3 关闭执行、准备新槽和冻结入口，保留历史读取和 regrade；不得恢复旧 Run，旧通过不抵扣 r4 门槛。

| 类别 | 当前已批准 | r4 提议 | 增量 |
| --- | ---: | ---: | ---: |
| 模型 HTTP | 1121 | 1124 | 3 |
| 模型 tokens | 6893807 | 6906912 | 13105 |
| 模型 USD | 10 | 10 | 0 |
| ORCA reference | 17 | 17 | 0 |
| ORCA development | 54 | 54 | 0 |
| ORCA formal | 48 | 48 | 0 |
| ORCA 总计 | 119 | 119 | 0 |

计算：`324 + 112 + 48 + 16 + 624 = 1124 HTTP`；`1018912 + 800000 + 288000 + 96000 + 4704000 = 6906912 tokens`。r4 固定包最多 112 HTTP / 800000 tokens；C 为 48 / 288000、条件 D 为 16 / 96000、formal 为 624 / 4704000，不借给本包。实际使用较少不增加槽位、重复或新身份机会。批准前若账本基线改变，须重新核算并绑定新基线。

## 新源码和固定门槛

先完成终止解释的目标状态与预算披露约束、真实派生离线回归及干净全量验证，再提出实际批准。r3 实际第二次 V06 上下文保留了目标未完成、窗口过宽及零计算预算；完整模型回答仍将三份合格成员误称为足够目标证据。修复不得隐藏反例或降低评审，也不能以程序正确标记 failed 替代真实解释通过。离线修改与回放只证明契约和容量，解释可靠性仍须本包新真实证据。

批准后冻结一个干净提交的源码、schema、prompt、依赖、配置、模型 profile、二进制、预算与证据身份。thinking 模式固定 `disabled`（仍调用真实模型），每次输入上限 12000 tokens、completion 上限 2000 tokens；原有有界协议纠正在每槽预算内计费。没有额外恢复、换 ID 或失败重跑。

| 顺序 | 零 ORCA 模型门槛（各 1 个新 Run） | HTTP / tokens 上限 |
| --- | --- | ---: |
| 1 | `N-06/raw-unsupported-system` | 4 / 32000 |
| 2 | `V-06/insufficient-additional-budget` | 4 / 32000 |
| 3 | `V-07/array-location` | 4 / 32000 |
| 4 | `V-09/different-method` | 4 / 32000 |
| 5 | `V-09/missing-electron-state` | 4 / 32000 |
| 6 | `N-03/raw-electron-state-clarification` | 6 / 48000 |
| 7 | `N-03/raw-ambiguous-reference` | 6 / 48000 |
| 8 | `N-04/raw-authorized-defaults` | 4 / 32000 |
| 9 | `N-04/raw-unique-inheritance` | 6 / 48000 |
| 10 | `N-04/raw-unconfirmed-inference` | 4 / 32000 |
| 11 | `N-07/raw-read-only-window` | 6 / 48000 |
| 12 | `N-09/raw-user-goal-replacement` | 6 / 48000 |
| 13 | `N-09/raw-preserve-goal` | 6 / 48000 |

合计最多 64 HTTP / 512000 tokens。每槽先审查实际 HTTP、所有提议的事实与语义、quantity/unit/conditions/source/limits/next_action 六轴、必需最终回复、程序检查和实际成本，再进入下一槽。初始提议、程序成功或退出码不能替代完整独立验收。

## 结构和科学执行

仅在 13 个模型门槛全部通过后依次执行：

1. 水、甲烷各 1 次 PubChem 查询，共 2 次；固定官方 property endpoint，最多 256 KiB。connect 5 秒、单次读写 10 秒、stream 软 deadline 20 秒（最后阻塞读可延至约 30 秒）；节流锁最多等待 30 秒，额外间隔最多 2 秒，记录实耗。每 Run 最多 1 次，失败不重试。
2. 两项身份检查通过后，生产 `structure.prepare` 各 1 次，共 2 次；固定 OPI 2.0.0 / RDKit 2025.9.6，每次 1 核、1024 MiB、30 秒，不启动 ORCA。整个环境同时最多 1 个准备或科学进程。每个体系的 XYZ、hash 和来源只冻结一次。
3. 对新包冻结的甲烷 XYZ 执行 1 次独立 RHF/STO-3G SP reference，ID 为 `bounded-20261008-r4-methane-prepared-sp`。独立预写输入、独立读取 raw stdout，不来自产品 Result；4 核、总内存 1024 MiB、MaxCore 每进程 192 MB、120 秒，不做 JSON 后处理。
4. 水严格 Opt、甲烷固定 prepared XYZ SP 各 3 条独立真实模型＋科学轨迹，共 6 次 development ORCA，顺序为水 1–3、甲烷 1–3。每条最多 8 HTTP / 48000 tokens，共 48 / 288000；每条 ORCA 仅 1 次，无追加科学或结构生成。科学默认 300 秒，Run 总时限最多 1800 秒。

水 Opt 使用原独立参考 `data/acceptance/reference-water_opt.json`，SHA256 `01f0124270697f2df3d1509a07fafc8c9cff97eff5849b5d39564c5afdbc5a3f`。必须通过 `optimization-final-stage-1`；终点能量误差不超过 1e-7 Eh，按 atom mapping 比较原子对距离误差不超过 1e-5 Å。不匹配即失败，不放宽阈值，也不宣称任意初始结构必然到同一最低点。甲烷 SP 仅对照本包新独立参考，不能套用旧 Opt。

## 停机规则和证明范围

任一门槛失败立即停止整个包，不借后续槽诊断。未知 HTTP 成本、进程或输入调用状态先停止并对账。源码/profile/schema/prompt/配置、预算或证据 hash 变化即停止；重新执行须另经用户明确决定。剩余额度或本提案本身不增加固定失败槽的执行机会。

每个体系仅由 harness 调用真实生产 Tool 准备一次，三条模型/科学轨迹从冻结 XYZ 开始。此包不证明三次模型自主从纯文本选择 resolve→prepare 的完整可靠性。同引擎独立输入和读取属于工程交叉核验，不是跨引擎或实验精度保证。C、D、formal 未由本包覆盖，原阶段 B 矩阵仍须另按既有范围执行；旧失败永久保留。

## 批准记录与入口

`python -m tests.helpers.phase_b_bounded_package proposal --package bounded-20261008-r4` 仅查看固定提案，不创建 Run 或记账。

只有用户明确批准后，才能创建 `docs/acceptance/phase-b/budget-approval-bounded-20261008-r4.json`：保存实际问答、本文原始字节 hash、完整 r4 scope、当前账本 SHA 和前一 r3 批准 SHA `428fa58791993d77055b70ce99249616051edd86eab2b89153bb0e737adf47d2`，再固定 `R4_APPROVAL_SHA256`。本准备不预造用户批准。

批准后的命令均显式指定 `--package bounded-20261008-r4`：先 `apply --execute` 向原账本追加第七次 amendment 和原文件快照，再 `freeze`；按顺序逐槽 `model ... --execute --live-model`，然后 `resolve ... --execute --live-network`、`prepare ... --execute --live-opi`、`reference --execute --live-orca`、`science ... --execute --live-model --live-orca`。底层 model evaluation 同标签执行/准备也校验批准、候选、预算和前序 gate，固定 development、disabled、repetition=1，禁止 resume；历史 regrade 保持只读。

状态：**待用户单独批准；离线修复和 helper 准备不代表真实通过，等待用户验收。**
