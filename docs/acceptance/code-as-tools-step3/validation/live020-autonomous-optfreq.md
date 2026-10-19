# Live020 原文自主 Opt/Freq 闭环

证据索引：[live020-autonomous-optfreq.json](live020-autonomous-optfreq.json)。本次使用 agent-json-v49、thinking_low、64k 输入预算，属于修复 live019 后的冻结候选。live017 的 v47 首次成功保留为独立历史事实。

原文与实施方案、live017 完全相同。模型先检索方法/基组依据，再形成 Plan，分别用 workspace.write 生成 job.inp 与 Python；随后把二者真实 Artifact/SHA 与初始 XYZ 绑定到一次 Python 执行。独立核对模型原始提案、writer 文件、执行输入和密封脚本一致，没有人工 Plan、任务代码或计算输入注入。模型追加了身份目标，最终四个目标全部满足；原文要求的结构、优化和频率目标均在其中。

一份 Python、一次真实 Linux ORCA Opt/Freq 执行完成，typed terminal delivery 通过且报告已渲染。B3LYP/def2-SVP、RKS、气相、电荷 0、多重度 1；四进程、每进程 MaxCore 192 MB，Python 与内部 ORCA 共享四核/1024 MB 容器边界。声明工作量 157/400，无额外代码尝试。CPU 166.086505 秒，墙钟 77.062 秒，峰值采样 909,602,816 B；resource_usage_complete=false，统计仍有采样下界限制，没有宣称磁盘硬配额。

脚本显式读取 job.xyz 和 stdout.out。优化观察的九个原子坐标逐值对应真实最终 job.xyz，频率观察绑定原始 stdout 的频率区段。两项 validation_status 仍为 unverified，Result.qualified_outputs 为空；目标满足、操作成功和生产科学资格分别记录。独立科学 oracle 另行报告，不改变原 Run 资格，也不据此声称全局最低构型。

13 次 HTTP 调用全部结算：148,837 输入 + 25,823 输出 = 174,660 tokens；未知用量 0，冻结峰值费率估算 USD 0.0756387，非账单。第七次 Plan 提案被拒后第八次修正，未发生实际生成代码失败或重试；本次仅证明原文自主闭环，不证明故障修复。

07:18:39.546085Z 的结束独立检查确认 62 个源码文件与 driver 均未偏离冻结记录。185 个原 Run 文件在审计前后不变，35 个归档索引核验通过。原始 job.inp 为 255 B LF；SDK 密封及实际 ORCA 输入为 271 B CRLF，仅十六处换行差异，单列 SHA，不宣称字节相等。
