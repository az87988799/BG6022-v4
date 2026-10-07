# 有界真实验证续验包 r2（待用户单独批准）

本文件仅提出续验范围，不是执行许可。旧 `bounded-20261008` 在 V06 失败后已停止，实际证据已提交至 `bcbec57`。新包固定标签为 `bounded-20261008-r2`；本轮只准备 helper 和离线测试，不应用预算、不冻结候选、不调用模型、PubChem、OPI 或 ORCA。新批准 SHA pin 必须保持 `None`，不能预造 `user_approved` 记录。

## 已用成本及旧包处置

唯一账本仍为 `data/phase-b/batch-ledger.json`。审阅基线 SHA256 为 `15f07f9cf0f86619c4be382349411e33f32c6d34e83bd195626b5fceb0acbe51`：累计模型 320 HTTP、1001551 tokens、USD 0.3978561，当前未知 token/费用为 0；ORCA reference 16、development 26、formal 0。保留历史批准链、全部已用和未知占用，以及原 C、条件 D、formal 的剩余范围。

旧包保留 N06 通过（`run_17a6d29f44fb4a2287dc0b55caf57ee9`）与 V06 失败（`run_7871e00efaf44e0e896972a686f9657e`）：共 2 HTTP、7967 tokens、USD 0.0036726、0 ORCA。V06 在一次有限采样分析后因反馈上下文上限失败，缺少最终解释，另有预算披露及单位解释问题；不得通过改写 review、恢复旧 Run 或套用新源码改判旧证据。

旧包另外 11 个模型门槛、2 次查询、2 次准备、1 次 reference、6 次科学轨迹均取消执行机会，由本提案中重新批准的完整固定包替代。旧包实际没有结构或科学启动，不增加这些类别的累计上限。旧包 proposal 与只读 regrade 保留，所有执行、准备新模型槽和冻结入口永久关闭；旧 N06 通过不能替代新候选的 N06 复验。

## 请求批准的累计上限

| 类别 | 当前已批准 | r2 提议 | 增量 |
| --- | ---: | ---: | ---: |
| 模型 HTTP | 1118 | 1120 | 2 |
| 模型 tokens | 6881584 | 6889551 | 7967 |
| 模型 USD | 10 | 10 | 0 |
| ORCA reference | 17 | 17 | 0 |
| ORCA development | 54 | 54 | 0 |
| ORCA formal | 48 | 48 | 0 |
| ORCA 总计 | 119 | 119 | 0 |

计算：`320 + 112 + 48 + 16 + 624 = 1120 HTTP`；`1001551 + 800000 + 288000 + 96000 + 4704000 = 6889551 tokens`。其中 r2 固定包为 112 HTTP / 800000 tokens；旧 C 为 48 / 288000、条件 D 为 16 / 96000、formal 为 624 / 4704000。后者完整保留，不借给本包。模型费用总上限仍为 USD 10；实际使用较少不增加槽位、重试或新身份机会。批准前若基线改变，必须重新核算并重新绑定批准基线。

## 固定槽位、执行顺序与上限

已完成的离线上下文修复在真实 V06 派生反馈样本上为 11983 / 12000 tokens，仅余 17 tokens；这是该样本的有界表示证据，不保证其它轨迹同样余量，也不是新模型真实通过。旧 V06 的单位与预算披露缺陷仍未由真实模型复验。

先完成上下文修复、离线与独立审查，再在批准后冻结一个干净提交：源码、schema、prompt、依赖、配置、模型 profile、二进制及预算身份一起冻结。不得复用旧 candidate、Run、模型 pass 或甲烷 reference ID。模式固定 `disabled`；每次输入最多 12000 tokens、completion 最多 2000 tokens。原有限协议纠正仍受每槽预算约束；无额外恢复、换 ID 或失败重跑。

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

每槽通过对实际模型输出的独立审查后才能进入下一槽；合计最多 64 HTTP / 512000 tokens。逐一核对原始 HTTP、所有提议的事实与语义、quantity/unit/conditions/source/limits/next_action 六轴、必需最终回复、来源、程序 Check 和成本；正确初始提议不能代替最终结果解释，程序科学成功不能代替独立验收。

13 项全部通过后，按以下顺序执行：

1. PubChem 水、甲烷各查询 1 次，总 2 次；只允许固定官方 property endpoint，最多 256 KiB。connect 5 秒，单次读写 10 秒，stream 软 deadline 20 秒（最后阻塞读可延至约 30 秒）；节流锁最多等待 30 秒，额外间隔最多 2 秒，记录实耗。每 Run 查询上限 1，失败不重试。
2. 两项身份检查均通过后，生产 `structure.prepare` 各执行 1 次，总 2 次；固定 OPI 2.0.0 / RDKit 2025.9.6，单次 1 核、1024 MiB、30 秒，同环境同时最多一个准备或科学进程；不启动 ORCA。每种 XYZ、hash 和来源只冻结一次。
3. 对本包冻结的甲烷 XYZ 新执行 1 次独立 RHF/STO-3G SP reference，ID 为 `bounded-20261008-r2-methane-prepared-sp`。独立预写输入、独立读取 raw stdout，不来自产品 Result；4 核、1024 MiB、MaxCore 192 MB/进程、120 秒，无 JSON 后处理。
4. 水严格 Opt 与甲烷固定 prepared XYZ SP 各 3 条独立真实模型＋科学轨迹，共 6 次 development ORCA。顺序水 1–3、甲烷 1–3；每条最多 8 HTTP / 48000 tokens，共 48 / 288000；每条 ORCA 仅 1 次，无追加科学或结构生成，科学默认 300 秒、Run 总时限最多 1800 秒。

任一已知门槛失败立即停止本包，不借后续槽诊断。未知 HTTP 成本、进程或输入调用状态先停止并对账，不能继续另一个槽。源码/profile/schema/prompt/配置、预算或证据 hash 变化即停止；不通过新身份清零次数。新的执行机会须另经用户明确决定，不能把本提案当作失败后自动续验许可。

## 参考、证明范围与明确未验证项

水 Opt 复用原独立终点参考 `data/acceptance/reference-water_opt.json`，SHA256 `01f0124270697f2df3d1509a07fafc8c9cff97eff5849b5d39564c5afdbc5a3f`。新结果必须通过 `optimization-final-stage-1`，终点能量误差不超过 1e-7 Eh、按 atom mapping 比较原子对距离误差不超过 1e-5 Å。若不匹配就失败，不放宽阈值或宣称任意初始结构必然到达同一最低点。甲烷新初始 XYZ 的 SP 只能对照本包新独立 reference，不能使用旧甲烷 Opt 替代。

每个体系准备一次，由 harness 选择真实生产 Tool；三个独立轨迹从已经自动生成、登记并冻结的 XYZ 开始。用户不必手写 XYZ，但本包仍不证明三次真实模型自行从纯文本缺几何选择 resolve→prepare 的端到端可靠性。相同 ORCA 引擎的独立输入和读取是工程交叉核验，不是跨引擎或实验精度保证。C、D、formal 尚未在本包完成，旧失败不因新包通过而删除。

## 准备入口与批准落点

`python -m tests.helpers.phase_b_bounded_package proposal --package bounded-20261008-r2` 仅打印提案，不创建 Run 或记账。省略 package 参数仍显示原包的历史 proposal 和关闭状态。

用户单独批准之后，才可创建 `docs/acceptance/phase-b/budget-approval-bounded-20261008-r2.json`，记录实际问答、本文原文 hash、完整 `scope(package="bounded-20261008-r2")`、当前账本 SHA 和前一批准文件 SHA（`9339874fb7f4359782ad7c9128f5dc535f6703b586ad45737748411968e3bc58`）；再将真实批准文件 SHA 固定到 `RENEWAL_APPROVAL_SHA256`。本准备提交不创建该批准文件。

后续经批准的命令均显式包含 `--package bounded-20261008-r2`：先 `apply --execute`，向原账本追加第五次 amendment receipt 并保留原文件快照；再 `freeze`，然后依次 `model --variant ... --execute --live-model`、`resolve --system ... --execute --live-network`、`prepare --system ... --execute --live-opi`、`reference --execute --live-orca`、`science --system ... --repetition ... --execute --live-model --live-orca`。底层 model evaluation 的同标签直接 prepare/execute 也必须通过新批准、candidate、预算、前序 gate，并限定 repetition=1、development、disabled、禁止 resume；只读 regrade 可审计历史记录。

当前状态：**待用户单独批准；helper 和离线验证不构成真实通过或验收通过。**
