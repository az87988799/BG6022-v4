# 模型上下文与机械往返优化：离线交付记录

日期：2026-10-20（Asia/Hong_Kong）。先按[决定 0048](../../decisions/0048-offline-model-context-efficiency.md)优化通用上下文和输出预约，再按用户提供的五项优先级及[决定 0049](../../decisions/0049-reduce-mechanical-model-roundtrips.md)减少文件发现、机械推进、来源声明、局部修复和导出中的重复工作。不进入步骤 6/7，不扩大科学资格。等待用户验收。

本轮新增**项目运行时真实模型 API、ORCA 和 MPI 启动均为 0**，没有网络下载。脚本化模型传输只用于离线对照；另有一次真实容器内纯 Python SDK 烟测，与科学执行分开报告。开发助手本身的 token 未计量，不包含在零项目 API 调用的结论中。

## 本轮五项实际实现

1. **现有文件直接可见。** 当前 Result 的有界索引展示已有精确文件键、Artifact ID/hash 和已知元数据，保留 Result/Attempt/Call 来源；默认最多 12 行、4096 字节，省略项明确计数。8 文件 fixture 的索引为 **1622 UTF-8 字节、0 次 evidence_reads**，可以直接构造既有 CodeRef/InputManifest 并经过原消费校验。索引不读取正文，不按后缀或“最新文件”猜测绑定，也没有未来文件 selector 或新的批量读取工具。[索引验证批次](artifact-index-tests.txt)为 **120 passed**。
2. **已批准的纯文件写入可以机械推进。** 新 Run 冻结策略后，唯一主循环仅在当前 Plan 的写入结果成功、检查及源文件完整、版本和目标不变、无新控制消息或未知执行时，继续既有 ready Step；科学结果、分析/查询观察和失败仍回到模型。三个已批准文件写入的脚本化传输对照中，模型入口调用由 **3 → 1**，实际仍是 **3 次写入、3 个决策轮**，两次机械选择持久化，最终交付仍为 partial，不因此科学通过或满足目标。中断恢复不重复写入；预算、终态解释预留与执行准入继续生效。用例见 [test_mechanical_progression.py](../../../tests/unit/test_mechanical_progression.py)。
3. **SDK 只辅助显式声明来源。** `receipt_source(receipt, filename, location=...)` 将脚本指定的已结算收据及精确原文件键转换成现有观察来源格式，不读取、扫描、补齐其他来源或推断数值。原收集器继续独立检查收据、hash、实际条件及最低证据，伪造来源仍拒绝，通用观察仍不获得科学资格。SDK 相关定向批次 **50 passed**；[sdk-runtime.json](sdk-runtime.json)另记录离线镜像构建、环境核验及一次容器内纯 Python helper/hash 烟测，不能代替真实 SDK→ORCA 链路或完整隔离验收。接口和部署说明见 [task-sdk.md](../../task-sdk.md)。
4. **局部代码修复不用重新提交完整脚本。** 同一 `workspace.write` 定义保留 `content`，并新增与其互斥的 Python `base` CodeRef + `edits`：最多 16 个针对原文的唯一、精确、互不重叠替换；基底和最终文本均不超过 64 KiB。预检和写入均验证许可、旧 Artifact/hash 及读取字节，生成新 Artifact，原文件不变；后续仍经过原 `repair_of`、输入和执行校验，不执行补丁代码。定向批次 **49 passed**；合成大脚本示例的完整参数为 **50,468 字节**，局部参数为 **249 字节**，仅为 UTF-8 JSON 表示大小，不是 token 或实际费用。用例见 [test_workspace_patch.py](../../../tests/unit/test_workspace_patch.py)。
5. **程序导出报告与原文件。** 新 CLI `export RUN_ID --output PATH.zip` 复用现有确定性报告和授权读取边界，写出报告、清单、验证说明及经大小/hash 核验的原文件；保留操作失败、科学 unverified、排除和目标缺口。已有终态解释合同继续使用，不增加一个总结循环。导出要求新目标位于历史存储之外，固定 256 成员、单文件 16 MiB、累计未压缩 64 MiB；缺失、损坏、越权、超限或导出中状态改变均拒绝整个包，不通过只读 GET 隐式打包。导出测试 **23 passed**，另有 **73 项既有报告、终态恢复及 Web 回归通过**。命令及限制见 [delivery-export.md](../../delivery-export.md)。

上述数量分别对应有限验证批次，包含重叠用例，不相加为本轮唯一测试数。它们证明所列程序行为及边界，不证明真实模型任务成功率、回答质量或实际账单改善。

## 0048 历史样本与测量方法

**[measurement.json](measurement.json)保留为 0048 时点的独立源码快照测量。** 其中 `implementation` 固定的是该次运行的源码 hash，随后五项实现继续变更了生产代码；此文件不是当前全部功能的效果测量，也未被本轮新结果覆盖。新的生产版本应另存新的测量文件。

该记录从[步骤 5 最终去重账本](../code-as-tools-step5-completion/validation/costs.json)定位 6 个唯一 Run 的 **90 对真实请求/响应**。分类为 normalization 18、planning 71、terminal 1；45 条为原生字面上下文，45 条已采用历史编码形式。每条请求使用当时与生产出站路径共用的纯函数 `optimize_model_messages`，保留其余 HTTP 参数；没有发送优化后的请求，没有执行归档模型代码或加载 pickle。

请求、响应与 Run 读取时核对冻结回执的 hash；账本、6 份 Run、90 份 request、90 份 response，共 **187 个历史 JSON** 的前后 SHA256 全部不变。各来源的相对路径、字节数和前后 hash 已列入测量 JSON。测量实现自身的源码 hash 另列于 `implementation`，与历史证据分开。

独立检查得到 **45 条 schema 等价、45 条 JSON 值完全相同**。AUTHORITY、CONTROL、DATA 的值、任务身份、用户原文、许可、预算与已有证据未被删改；schema 的有限改写用独立定义展开和约束规范化核对，字面 `const`/`enum`/`default` 数据不按 schema 解释。JSON 键的序列化顺序允许改变。

## 0048 快照已观测的离线结果

| 指标 | 原请求 | 优化投影 | 含义 |
| --- | ---: | ---: | --- |
| 90 条消息内容 UTF-8 字节合计 | 4,358,336 | 4,318,332 | 减少 40,004 字节，约 **0.918%** |
| 保守输入 token 上界合计 | 4,415,936 | 4,375,932 | 采用既有字节上界函数；不是实际 token |
| schema/协议分段值字节合计 | 898,161 | 858,157 | 包含动作 schema、工具声明和响应协议；分段值不含顶层标点 |
| AUTHORITY 分段值字节合计 | 1,153,149 | 1,153,149 | 原值保持 |
| DATA 分段值字节合计 | 1,324,436 | 1,324,436 | 原值保持 |
| 输出 token 上限合计 | 2,260,992 | 2,260,992 | **90 条均未缩减**；并非实际输出量 |
| 同一 Run 相邻请求公共前缀字节合计 | 386,427 | 691,291 | 84 对；消息角色和内容的相邻公共前缀 |
| 每对公共前缀字节均值 | 4,600.32 | 8,229.65 | 不是服务端缓存命中或缓存 token 预测 |

按调用用途，保守输入上界分别为 normalization **507,755 → 501,749**，planning **3,879,768 → 3,845,770**，terminal **28,413 → 28,413**。71 条 planning 均仍允许创建或修订 Plan；唯一 terminal 请求已有历史编码。因此，这批归档不能证明短动作输出上限策略产生了实际节省。

原响应的实际使用量仅作为基线：输入 **1,327,520**、输出 **444,541**，共 **1,772,061 token**；原输入缓存命中 **147,328**，未命中 **1,180,192**，原命中率约 **11.10%**。这些都是历史调用结果，不能按本次字节比例改写成优化后的 token、缓存率或费用。

## 验证状态、部署与限制

历史请求中已公开的来源引用没有保留新观察去重所需的完整来源身份，测量没有从后来的 Run 状态补造当时上下文。因此该次 90 条的 DATA 未缩减，也没有宣称它们验证了生产完整快照中的新增观察去重路径。该路径及纯控制输出预约由专门的离线反例验证，不能替代真实回答质量验证。

脚本在运行期间硬性拒绝 socket 操作、子进程启动及对历史目录的写入；只解析 JSON，不读取模型凭据，不调用传输接口，不在报告中复制原始请求或模型回答。历史来源原字节、优化前后大小、等价检查和有限失败字段均单独记录。

最终定向回归及源码 hash 见 [validation.json](validation.json)。498 项主批次为 **481 passed、17 failed**：16 项与下述历史基线相同；新增的一项是旧测试仍要求文件写入只有三个参数，已按新增 `base/edits` 合同适配，保留原执行权限断言，单项复测 **1 passed**。即时工具能写代码、执行程序或影响未知时保留完整代码规则，最终相关批次 **107 passed**。各批次有重叠，不相加。当前所选回归没有未解释的新增失败，仍保留 16 项历史失败，不能宣称全部通过。

所有改动 Python 文件 Ruff 通过，Git 差异检查通过；导出相关既有报告、终态恢复和 Web 的 73 项验证另列于上文。**本轮未运行全量回归。** 原始测试输出保留在忽略的 `data/model-context-efficiency`，验证记录保留各批次计数、失败诊断、来源 hash 和复测范围。

原 **16 项基线失败**继续保存在 [baseline.json](baseline.json) 与 [baseline.txt](baseline.txt)，其口径是 `HEAD:src/orca_agent/context.py` 配合当时工作树测试的定向对照，不是干净全量基线。保留原失败、断言和容量诊断；后续测试适配或修复不能改写该历史事实，也不能将未复测项推断为已修好。

新 Run 默认可冻结 `mechanical_progression=true`；历史 Run 配置缺该字段时按关闭解释，不自动启用或重置任何累计预算。SDK 源码已经变化，新 Linux 镜像及 manifest 经过离线构建和本地核验，仅更新忽略的 `config.local.toml` 中供新 Run 使用的镜像身份，其他配置、旧 Run 快照、旧镜像及卷保留。历史容器仍按保存的镜像和句柄对账；新 Attempt 的 SDK hash 必须匹配其冻结环境。要继续历史 Run，须使用其对应产品版本和配置，不能只换成新镜像。

尚未验证优化后的服务端实际 token、缓存命中、账单、端到端回答质量、任务成功率或科学能力；未使用不匹配的 tokenizer 或字符比例推测这些结果。机械调用减少是离线脚本化对照，局部 patch 是参数字节示例，0048 是当时的表示等价测量，三者不能合成为真实 token 降幅。SDK 纯 Python 烟测也没有验证新镜像的 ORCA/MPI、完整任务隔离或科学计算链。未实施步骤 6/7 的冻结规则或同版本跨任务正式验收。

## 离线复核命令

从仓库根目录执行：

```powershell
# 使用新的输出文件名，保留原 measurement.json 的历史源码快照。
.venv/Scripts/python.exe scripts/measure_context_efficiency.py --output docs/acceptance/model-context-efficiency/measurement-recheck-001.json
.venv/Scripts/python.exe -m pytest tests/unit/test_measure_context_efficiency.py -q --tb=short
.venv/Scripts/ruff.exe check scripts/measure_context_efficiency.py tests/unit/test_measure_context_efficiency.py
.venv/Scripts/python.exe -m pytest tests/unit/test_artifact_index.py tests/unit/test_mechanical_progression.py tests/unit/test_task_sdk_sources.py tests/unit/test_workspace_patch.py tests/unit/test_delivery_bundle.py -q
# 明确的新文件写入；E:\exports 预先存在，delivery.zip 尚不存在。
.venv/Scripts/orca-agent.exe --config config.local.toml export RUN_ID --output E:\exports\delivery.zip
```

测量命令只重算当前源码的本地表示指标；每次使用尚不存在的新记录名。运行上述离线测试或导出命令不授予模型或 ORCA 执行许可。SDK 镜像准备、显式配置和最小纯 Python 烟测命令见 [task-sdk.md](../../task-sdk.md)，完整构建日志保留在忽略的 `data/model-context-efficiency/sdk-runtime-build-001`。
