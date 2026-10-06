# B-01：契约、用例与独立参考交付

日期：2026-10-06（Asia/Hong_Kong）。状态：**B-01 已完成，等待用户验收**。本轮用户明确选择“先完成 B-01 并交付”；B-02～B-11 不在本轮实施范围，阶段 B 尚未整体完成或验收通过。

## 本次交付

- [最小架构决定](../../decisions/0004-agent-revisions-and-evidence.md)：受控修订、无 Plan 查询 Run、异类规则、反馈幂等及模型/批次计费。保留普通保存防越权、旧文件零预算、历史输入快照与唯一协调者；未发现方案与总蓝图的实质冲突。
- [用例清单](cases.md)及 [58 个机器可读变体](../../../tests/fixtures/phase_b/cases.json)：覆盖 V-01～V-11，每项列出输入、目标、前置、允许动作、最低证据、反例、停止条件和预算。所有未来运行时评测仍为 `not_run`。
- [开发 profile](profile.json)、[计价依据](pricing-basis.json)和 [提示模板](../../../tests/fixtures/phase_b/agent-json-v1.txt)：冻结模型/上下文限制及独立测试开关契约。SDK、实际提案 schema 和最终提示渲染由 B-02/B-04 验证后锁定；本轮没有安装新依赖或调用模型。
- [候选几何](../../../tests/fixtures/phase_b/sampling-candidates.json)、[参考审查](reference-review.json)及 [证据索引](evidence-index.json)：全部新参考来自独立预写输入和原始 stdout/XYZ 检查，不用产品 Result 数值回填 expected。生产后端只负责既有授权、预算、环境并发、进程树及收集。

生产 `src/`、依赖及科学检查没有改动；新增 Python 仅为验收工具和离线校验。该批不能宣称自然语言 Agent、自动修复、成功再规划、分析 Tool 或新增科学能力已实现。

## 已取得的真实参考

15 个 H₂O 单点均收敛；另 1 个 `MaxIter=2` 的真实计算仍因 SCF 不收敛而失败。所有计算采用 RHF/STO-3G、中性单重态、TightSCF、ConvForced 1、4 核与 MaxCore 192 MB，沿用同一全环境额度和 Windows Job Object，未触发 JSON 转换。

采样几何源于阶段 A 独立严格优化的水分子；固定另一根 O–H 键长和夹角，只沿原方向移动指定 H。每个候选均重算实际距离、夹角、原子映射并核对 hash。单位 Å，公共目标宽度 `w=0.12`：

| 参考组 | 中心 c | h | 初始最近邻跨度 | 独立参考支持的动作 | 动作后跨度 |
| --- | ---: | ---: | ---: | --- | ---: |
| 左加密 | 1.019409222408 | 0.08 | 0.16 | 增加登记的较小距离中点 | 0.08 |
| 右加密 | 0.959409222408 | 0.08 | 0.16 | 增加登记的较大距离中点 | 0.08 |
| 充分停止 | 0.989409222408 | 0.04 | 0.08 | 初始三点已足够，停止 | 0.08 |

两组加密的初始中心均为明确内部最低点，较低的粗邻点指示不同方向，独立中点复算均确认对应侧。数值区分阈值为 `6.13686837721616e-11 Eh`，由原始打印精度及浮点舍入上界推导；最终最小邻点能差约 `8.7590e-4 Eh`，远大于阈值。阈值仅用于区分输出数值，不是方法误差条。结论只覆盖已登记的离散采样，不声称连续/全局最低、振动稳定性或 TS。

`MaxIter=2` 参考输出有第 1、2 次迭代记录，并打印 `SCF NOT CONVERGED AFTER 1 CYCLES`；保留 ORCA 原文计数，不将其改写为 2。它确认了耗尽候选，结合历史 `MaxIter=1` 失败可冻结后续用例，**尚不构成真实模型驱动的两次失败闭环**。

电子能差参考选用本批已合格的两个不同水几何：A=`sampling-left-center`，B=`sampling-left-minus_half`，原始值复算 `ΔE=E(B)-E(A)=-0.0008759006639991185 Eh`。具体 Result/Attempt/Artifact、原始字段行号、hash 与 `orca-hf-2` 均在参考审查内；分析 Tool 本身仍待 B-07 实现。

真实未注册字段使用阶段 A 的 `Geometries[0].Dipole_Moment[0].dipoleMagnitude`，只冻结观察来源，不给予偶极矩科学资格。该历史样例曾触发内部 JSON 后处理，此事实保持原样，不能用来证明零后处理执行；本轮只读及所有新参考均未转换。

## 评测隔离与可恢复账本

内部验收名称包含 left/right/stop，因此专门冻结了 [三份中性模型输入](../../../tests/fixtures/phase_b/model-inputs)。它们使用不含答案的 system/geometry ID、相同用户原文、实际坐标及必需标记，不包含参考能量、正确动作或内部路径。B-04/B-09 还必须检查最终发送的 JSON；仅过滤一个 `expected_action` 字段不够。

[参考命令](../../../tests/helpers/phase_b_reference.py)默认只读，仅 `--execute` 才启动。每次启动前在固定 `data/phase-b/batch-ledger.json` 加锁预约，随后绑定 Run；原 ID 重放只核验收据和文件 hash。未知执行仍占用预算，不能换 ID 重发；分类不可更改，已知完成后的新启动也继续计费。离线测试覆盖收据发布与账本更新之间崩溃、篡改收据/文件、未知占用和额度耗尽。

已有交付快照但本地账本缺失时，命令要求先恢复原归档，拒绝重新初始化零用量；新检出目录不能据缺少 `data/` 再获得 16 次参考额度。

```powershell
# 只读审查本机已冻结账本；不需要 DeepSeek，也不启动 ORCA
.venv\Scripts\python.exe tests\helpers\phase_b_reference.py
.venv\Scripts\python.exe tests\helpers\phase_b_reference.py --id scf-maxiter-2

# 默认离线：核查小型真实输出副本、几何、参考判据、预算及权限反例
py -3.11 -m uv run --offline --locked pytest tests/unit/test_phase_b_reference.py tests/unit/test_phase_b_review.py tests/unit/test_phase_b_contract.py -q
```

该命令是开发验收入口，不注册为模型 Tool，不允许成为生产任意输入通道。后续模型计费与正式执行必须接入同一批次计数；本轮账本内模型上限是冻结配置，尚无生产模型传输或计费实现。

## 成本、环境与限制

[实际环境](environment.json)：Python 3.11.4、OPI 2.0.0、ORCA 6.1.1、MS-MPI 文件版 10.1.12498.18。全部新参考核验 4 核、1024 MiB 进程树提交内存上限、退出后活动进程为 0；峰值提交内存最大 171,515,904 bytes。参考计算累计 wall time 181.435 秒、CPU time 281.234375 秒；这不是整轮开发耗时。

[批次账本快照](batch-ledger-snapshot.json)：新增 16 次 ORCA（独立参考 16、正式 0、开发 0），原生后处理 0；DeepSeek HTTP 0、tokens 0、费用 $0。参考子额度已用完；后续仍可用正式 48、开发/重跑 32，共 80 次，不能新建账本清零或重标分类。历史证据只读复用没有新增成本。

当前进程未发现 `DEEPSEEK_API_KEY`。B-01 不需要真实模型；B-04 开启前需由用户在本地环境提供密钥、重核价格、固定 SDK 并验证输入 token 上界和传输边界。密钥值未读取到日志、上下文或产物。真实模型行为、三次联合重复和远程环境均未验证。

小型原始 stdout/input/XYZ 副本及 provenance 已随仓库保存，可离线复核；完整 Run、原始旁文件、收据和预算账本保存在固定本地产物目录，并打包于 [archive.json](archive.json) 所列归档。归档未上传为云端副本；需要跨机恢复时保留 `data/` 路径结构并核验 hash。独立输入与读数不使用待测构造/解析器，但仍使用同一 ORCA 引擎和受管后端；不是跨软件或实验准确度验证，也没有人类专家签署。

## 验证与交付状态

新增定向测试 **93 passed**；默认全量回归 **397 passed、14 skipped**（13 项未启用真实 ORCA，1 项 Windows 符号链接权限不足；skip 不计通过）。Ruff 和离线构建通过。具体命令、通过/失败/跳过和原因见 [validation.json](validation.json)。首次新增测试收集曾因测试目录导入路径失败，已修正并保留事实；独立参考归档脚本第一次遇到检查字段结构差异，未重跑 ORCA，修正后只读重新收集。代码审查发现收据 hash 漏校验、非有限采样值及模型输入标签可能泄漏答案，均在本批修复并新增反例测试。

ORCA 输出含 CRCRLF，行号统一按原始 UTF-8 的 LF 分隔记录、从 1 起算，定位到字段实际首 token；不能用 `str.splitlines()` 对照这些文件。修正 locator 后只读重提取审查记录，原始执行收据及其 hash 完全保留，不覆盖历史事实。

B-01 的参考门槛已经满足。58 个未来运行时变体仍未执行，默认离线测试通过不能计为它们已通过。交付后等待用户验收，再按用户范围进入下一批。
