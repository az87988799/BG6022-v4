# Live017：原始乙醇请求自主 Opt/Freq 闭环

原始请求为“请为乙醇生成结构，选择合适的方法和基组，优化结构并分析振动频率，说明计算条件和结果验证情况。”Run `run_411fb7f2c8ce48249d3fdb6755587142` 于 2026-10-19 06:37:35 UTC 结束，记录状态 `completed`、交付 `complete`。具体证据及原文件大小、SHA256 见 [独立审计索引](live017-autonomous-optfreq.json)。

真实模型自行完成语义归一、文献检索、B3LYP/def2-SVP 选型及 Plan，生成 4,833 字节、156 行 Python，并通过 `workspace.write`、`python.execute` 和薄 SDK 启动一次真实 Linux ORCA 6.1.1。初始模型上下文无 Plan，许可输入 Artifact 列表为空；成功执行的 Python 与原模型第 10 次响应、写入 Artifact、sealed/task.py 逐字节一致。没有开发者注入 Plan、输入或脚本。

模型生成的原生输入包含 `RKS Opt Freq`，电荷 0、多重度 1、气相、1 个 ORCA 进程、MaxCore 192 MB、SCF/几何/响应迭代上限 128/64/64。模型字符串与 task/job.inp 是 248 字节 LF；SDK staged 及实际 ORCA 输入是 266 字节 CRLF，18 个换行差异之外文本相同。这些不同 SHA256 均保留，未声称输入副本逐字节相同。

一次 Python Attempt、一次内部 ORCA 调用均完成、exit code 0；声明 work units 为 93，不能解释为实际 SCF 次数。SDK 返回的输出副本及 35 项已归档文件 hash 独立匹配；生成代码收集了 9 原子几何及 27 个频率，原始 stdout、Hessian、XYZ、轨迹和 observations.json 可追溯。

**科学边界：** Python Result 的 `qualified_outputs` 为空；几何与频率两个观察均为 `unverified`。文件身份/hash 检查通过不证明数值、单位、收敛或最低点，实际条件来源标为 admitted input。终止交付契约通过，报告明确“不宣称科学资格”，目标完成依据 `reported_with_limitations`。独立科学 oracle 属于另外的开发验收证据，不改写该 Run 的资格。

本次有两次模型提案被拒：遗漏电子态默认值后由模型明确采用；遗漏已完成 resolve Step 后由模型修订保留。它们发生在执行之前，不是运行时代码故障修复。本 Run 的 `code_retries=0`，不能与 live016 的失败拼接成同 Run 修复成功。

真实模型成本：13 次 HTTP 200；128,563 输入 + 30,669 输出 = 159,232 tokens，unknown 0。逐条原响应 usage/hash 与记录匹配。冻结费率估算 USD 0.0753717，按未缓存最高档计价，未核对供应商账单。容器累计 CPU 观察为 86.121002 秒、墙钟 96.438 秒、峰值内存 678,707,200 字节，`resource_usage_complete=false`；最后成功采样是下界，尚无每 Attempt 磁盘硬配额。OPI 结构准备单独使用 Windows JobObject，并非 Windows ORCA 验证。

冻结于 06:32:20 UTC；06:33:43 UTC 和结束后的 06:38:07 UTC 独立比对 62 个源码/依赖文件及驱动均一致，驱动 outcome 同样记录无变化。这是前后哈希核对，不是连续文件写监控。审计前后原 Run 的 181 个文件 hash 保持不变。未运行新的模型、ORCA，也未改写原 Run。
