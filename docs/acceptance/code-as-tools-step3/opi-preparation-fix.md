# OPI 初始几何准备修复验证

这是 live007（`run_24a4a3c7acb5403f96e61734fe10f105`）失败后的独立诊断和验证。原 Run 报告 `generated XYZ count or row shape differs`，但当时未保留生成的 XYZ，因此不能从原始证据重建或核验其具体末尾字节。原 Run、失败日志及缺失事实均未改写。

本机固定 OPI 2.0.0 的 `to_xyz_block()` 输出原子数、空注释行、坐标及额外终端空行。修复只在校验视图中忽略末尾纯空白行，保留生成原始字节与哈希；内部空行、缺少或多余坐标、NaN、身份不符和异常键长仍拒绝。worker 在原有 65,536 字节限额内先保存候选，再校验；失败候选不获得输入资格，成功后才发布 worker 回执。

| 验证 | 真实结果 | 范围 |
| --- | --- | --- |
| 四个相关单元模块 | 129 passed、1 skipped，控制台 47.49 秒 | 实际 OPI serializer 配合合成坐标；水、甲烷及通用图的正负例；失败候选保存与超限拒绝。真实生成探针在此轮跳过。 |
| 独立受管理 OPI 探针 | 1 passed，控制台 5.11 秒 | 原 Store → dispatch → `_generate` → Windows Job 路径，真实 OPI 三维生成。 |

探针采用与 live007 相同的五个 worker 输入值：`CCO`、`ethanol`、charge 0、multiplicity 1、`structure-identity-2`。身份记录来自合成 PubChem 格式夹具，没有重新进行名称检索；这与 live007 的真实身份来源不同。真实运行版本为 OPI 2.0.0、RDKit 2025.9.6、Python 3.11.4。原始 XYZ 共 859 字节，以两个换行结束，SHA-256 为 `5341a7b8b4268ed330dc280e58a3dcf05b1fd766ee331dc1de2eff7cac9d4257`，与归档 Artifact 和 worker 回执一致。

Job 原子分配确认成功，1 核、总提交内存上限 1,073,741,824 字节；峰值提交内存 65,896,448 字节，wall 1.797 秒，user/kernel CPU 分别 0.65625/0.78125 秒，exit code 0，最终活动进程为 0。仅获得 `structure-prepare-2` 初始几何输入保证，未证明结构优化、频率性质或科学 Goal 完成。模型调用、ORCA 预约及实际启动均为 0；此可信 worker 的 Job 结果不构成新增文件或网络隔离证明。

真实探针测试需要 **Windows 且 `ORCA_AGENT_STRUCTURE_SMOKE=1`**，否则跳过；不需要 `--live-model`、`--live-orca` 或 `--live-container`。本次默认单元命令的 1 skipped 正是该探针。单独命令仅在自身进程中设置专用开关；如果调用方显式把它保留在环境中，即使运行普通 pytest 命令，也会启用这一受管 OPI 测试。默认单元测试仍阻断网络和普通子进程；探针父进程同样使用离线网络桩，身份查询被合成响应替代，但没有声称子进程网络隔离已由本测试证明。

生产源码 `src/orca_agent/tools/structure.py` 的验证时字节哈希为 `fbbcf8736f8c2efcfff30be20158694f87224a4cc2383446509bcf182e33e0bd`。[最小证据包](validation/opi-preparation-fix.json)保留必要 probe 字段、准确 XYZ、12 个源码／日志／JUnit／原始文件的哈希索引。选中的工作目录仅做路径脱敏，改为仓库相对路径；其余保留数值、失败事实和 XYZ 字节不变。原始文件仍位于 `data/code-tools-step3/opi-trailing-xyz-*`，未复制凭据、用户目录或完整重复的进程记录。
