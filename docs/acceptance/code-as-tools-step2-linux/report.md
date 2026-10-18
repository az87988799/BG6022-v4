# 步骤 2：本地 Linux 容器续验

日期：2026-10-18（Asia/Hong_Kong）。基线：`4136843aa66370a9ed44b0b9bc1d055d61fc0cdf`。本次仅继续步骤 2；步骤 3—7 未推进。步骤 1 的用户验收状态不变。Linux 最小链路与限定边界已有真实验证，**等待用户验收**。

## 本次实现

Windows 的界面、Agent 和 Store 保留。新 Run 可显式选择本地 WSL2/Linux Docker 后端；旧版 Run 固定解释为 Windows，不自动迁移。Linux 的 OPI 输入、原生输入和 Python SDK 均复用 `managed.launch_orca`，统一执行许可、预算、资源、原始证据和科学检查。Windows LPAC 继续明确拒绝并行输入；没有串行回退或取消隔离。

一个尝试使用一个容器和一个 cgroup，Python 与内部 ORCA/MPI 共用四核、1024 MiB、零 swap、进程数上限和环境租约。任务 Python 为 UID 10001，ORCA 为 UID 10002，维护期限的 PID 1 为 UID 10003；固定 helper 处理传输与启动门。能力全部移除，根文件系统只读，无宿主目录/socket挂载、无网络，Python 在加载代码前安装 seccomp。SDK 只允许一次新内部 ORCA 请求，不含求解算法。

启动意图、容器身份和 gated launcher 先持久化；只有同一 PID/start_ticks 的 `/proc/PID/exe` 与固定 ORCA 文件相符，才计入实际 ORCA 启动。无法观察到加载证据时保留预留预算，结果不能宣称执行完成。`started.json` 与 `actual-start.json` 分开，恢复核对二者身份后结算，不能用旧 gated 记录覆盖实际启动事实。

宿主失联后 PID 1 的 15 秒心跳期限结束整个容器。收集使用固定有界 helper；原计算停止后仅启动只读收集容器，不重启计算。路径、单链接文件、数量、字节、传输和收集时间分别检查。只有原容器及派生收集容器都确认终止才释放租约。输入副本与收集输出分开保存，失败和部分原始传输保留。

## 冻结环境

| 项目 | 实读或固定身份 |
| --- | --- |
| Docker Desktop / Engine | 4.95.0 / 29.8.2 |
| WSL2 Linux kernel / cgroup | `6.18.54.1-microsoft-standard-WSL2` / v2 |
| Linux CPython / OPI | 3.11.2（Debian `3.11.2-6+deb12u9`）/ 2.0.0 |
| ORCA / Open MPI | 6.1.1 Linux x86-64 / 4.1.8 |
| 最终生产镜像 ID | `sha256:705838c326773e91f0b1d33bd572b3716931a07828bd66e48e4a0d5aaff8cb7e` |
| 最终 manifest SHA-256 | `535a71876cae4b67cbf8a7dd63f051b917a7e6c1b50f2c1afbade8776b1f104f` |
| ORCA 用户安装包 SHA-256 | `a0bc1d6d2c3c00620367bbc5dbf2b3a7018abc92d1ff65f06cec46f75350b9be` |
| 最终 helper SHA-256 | `8c1ad21f9104f9df0213aa1e52d8d2e6b231a1b461c12c76ccfcb43c45df769c` |

用户提供 Linux 包并已授权接受 EULA。归档通过完整 XZ 校验、路径/类型检查；上面的 SHA 是本地内容身份，未与发布者签名或官方 checksum 比对。ORCA 安装包、程序、私有镜像、wheel 缓存和大型二进制产物不提交或上传。

Docker 原有版本的宿主桥接无法使用，正常升级后可用；没有重置 Docker、注销 WSL 或清理其他用户容器。基础 Debian 镜像与两份本地基础镜像按 digest 固定，科学 wheel 按版本/大小/hash 锁定，最终生产构建离线完成。基础镜像较早的 apt 解析并非可重现的日期快照；不能承诺从远程 apt 重建出逐字节相同的基础镜像。依赖、许可证与构建说明见 [运行镜像目录](../../../scripts/container_runtime/README.md)。

## 验证事实与失败历史

初始探测与生产证据分开：见 [probe 索引](probes/evidence-index.json) 和 [production 索引](production/index.json)。初始 MPI 四 rank 的初始化、通信和 finalize 通过；初始 ORCA 水单点四核正常结束，能量与预存串行参考在 `1e-7 Eh` 内相符。初始成功不能替代最终镜像验收。

最终镜像的真实执行组为 **7 passed，113.27 秒**：必要库/内核拒绝、实际超时、实际取消、实际 OOM、动态 Python→SDK→四核 ORCA，以及旧 OPI 四核 SP/Opt。SDK 通用结果保持 `scientific_validation=unverified`，不发布合格科学端口；旧 SP/Opt 经原检查器验证后才发布对应输出。

最终 SP 合格能量为 **−74.96299161531729 Eh**，严格 Opt 最终能量为 **−74.96590119219361 Eh**。这只覆盖固定水分子/RHF/STO-3G 样例；优化收敛不证明无虚频极小值。只读 OPI 消费已有 property JSON，未启动转换。SDK 同一容器实际采样到 UID 10001/10002/10003，峰值 275,865,600 bytes，Python 与 ORCA 没有各获一份 1 GiB 配额。

生命周期和攻击组验证内容包括宿主源码/合成凭据 canary、宿主真实回环监听器正对照与容器拒绝、恶意 IPC 权限/schema/路径、符号链接和硬链接、回执写入与重放、输出链接收集拒绝、跨 Store 的环境额度竞争、协调者硬退出及两个创建回执丢失窗口。创建后但未启动的容器保持 `unknown`、原租约和预留预算，连续恢复不启动副本；没有把它伪称为已终止。

已保留这些开发失败，不能只报告最后成功：

- MPI-001 在启动前因日志驱动选项冲突失败；MPI-002 缺 rsh/ssh launcher；固定 isolated launcher 后 MPI-003/004 通过。
- boundary-001 的最小 Python 缺 JSON；完整固定 Python 后通过。
- 初始 ORCA 镜像两次构建失败，分别是不存在的助手名称与 Docker 对裸 image ID 的解析；修正后构建成功，未提交科学输入。
- 生产 OPI 首轮两个请求的 timeout 超过冻结 90 秒上限，在预留/启动前拒绝，实际 ORCA 为零。测试在冻结 Run 前显式使用 90 秒，随后两例通过；没有改动历史 fixture 或放宽运行时限制。
- 首轮攻击组 7 passed / 6 failed：六项任务均取得真实拒绝，但测试未考虑容器回执新增的空 `files` 映射，完整对象比较失败。修正该预期后重跑，原始失败记录保留。
- 初轮 Windows 集成有六项 fixture 未接收新增 container 回调参数；更新统一入口测试替身后定向七项通过。合成 fixture 不充当真实隔离证据。

| 验证组 | 真实结果 | 证据范围 |
| --- | --- | --- |
| 执行、Store、输入、配置/环境、恢复与解析定向单元 | 380 passed / 1 skipped，106.10 秒 | 跳过项需要 Windows 符号链接权限；Linux 链接攻击另有真实测试 |
| Windows Runner、门禁、恢复、后端与 LPAC 集成 | 90 passed，232.45 秒 | 含合成控制故障及真实非科学进程，不计为真实 ORCA |
| 最终 Linux 执行组 | 7 passed，113.27 秒 | 含三次四核真实 ORCA |
| Linux 生命周期四例 | 4 passed | 首轮 security-001 中四项；心跳清理、双 Store 额度、两创建窗口 |
| 最终攻击与实际 ORCA 启动后硬退出 | 10 passed，122.53 秒 | 九项攻击及一次真实 ORCA 启动后恢复；退出后不重跑 |
| 基线通过项及紧上下文修复回归 | 131 passed，32.85 秒 | 与上述单元组有重叠，不相加声称独立总数 |
| Ruff / Git 差异检查 / 本地 doctor | 通过 | doctor 只检查固定镜像与环境，未提交科学输入 |

最终 Linux 四组共 **21 个不同真实用例**，均已通过各自断言；失败场景的产品结果仍按 failed/unknown 保留。框架报告见 [validation 索引](validation/index.json)。扩大到全部历史单元测试时发现旧契约断言失败，随后停止了未完成的全量运行；没有宣称全仓回归通过。对修改前 HEAD 的 205 个代表用例独立复现出 87 个既有失败、118 个通过；本次曾新增两项紧上下文容量回归，压缩工具目录元数据后恢复通过。最后重跑全部 118 个基线通过项及 13 项 managed 测试通过，没有扩大模型额度或修改历史测试来掩盖失败。对照使用相同依赖环境及原样复制的少量历史 fixture，完整适用限制见 [基线核验](validation/baseline-verification.json)。

## 成本与证据边界

本轮实际 ORCA 计算启动共 **8 次**：初始开发 probe 1 次，生产路径 7 次（旧生产镜像 SDK/SP/Opt 各一次；最终镜像 SDK/SP/Opt 各一次；最终镜像硬退出例一次）。生产路径 40 个 Run 的 CPU 下界合计 **81.286721 秒**；创建窗口和预检拒绝均未记作科学启动。初始 probe 成本单独保存。真实模型调用为 **0**，远程/集群运行为 **0**。所有动态脚本由开发者编写，只证明执行底座；不证明自主代码生成、自然语言端到端或任意科学算法。

cgroup CPU、墙钟和内存记录取最后成功观察，CPU 是下界，`resource_usage_complete=false`；独立收集容器与构建准备成本未完整计入。内部 ORCA 成本已包含在父容器中，不再加一次。OOM 的内核峰值可能超过 `memory.max` 一个页；报告保留精确原值，不将 MaxCore 等同于作业硬内存上限。

## 限制和未验证项

1. 仅验证本机固定 WSL2/Docker/Linux/ORCA/Open MPI 组合，不保证其他宿主、版本、MPI 传输或任意 ORCA 方法兼容。
2. 每次 Python 尝试仍最多一次新 SDK 调用；内部回执不完整时保留未知和占用。不实现多次算法调用、Python 检查点恢复或步骤 3—7。
3. 磁盘写入没有每 Attempt 的硬配额；产物传输/归档预算不能替代运行时磁盘配额。资源记录与瞬时进程采样不是完整审计轨迹。
4. daemon/WSL 实际重启、运行中 Docker 服务失联、收集器宿主同时硬退出及正式多次重复矩阵尚未逐项真实验证。身份或终止无法核实的路径按未知保留，不自动清空额度。
5. 创建回执丢失后保守保留 `created` 容器及租约；本步不提供自动清除未知占用的管理流程。证据卷和停止容器保留，不自动 prune。
6. 本轮未做真实模型、乙醇优化/频率、任意分子科学准确性或集群验证；原生输入准入与原科学检查器范围未扩大。原始观察、进程完成、科学检查与用户目标仍分别记录。

相关决定：[ADR 0033](../../decisions/0033-local-linux-container-execution.md)。用户已有 `docs/acceptance/repair-cycle-development-4/` 未纳入本次提交。小体积原始证据按 `.gitattributes -text` 保留字节；完整本地目录为 `data/container-proof-20261018/`。

本机忽略提交的 `config.local.toml` 已显式配置最终 Linux 镜像，doctor 检查通过，模型仍为 disabled；旧 Windows 配置保存在本地 proof 目录。已有 Run 不迁移，已运行的界面进程需重新读取配置后才用于新 Run。复现入口见 [配置示例](../../../config.container.example.toml) 与 [构建/验证说明](../../../scripts/container_runtime/README.md)；真实测试需要 `--live-container`，科学测试另需 `--live-orca`，每批使用新的 `--basetemp` 以保留历史证据。
