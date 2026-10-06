# 优化判据及原生 JSON 范围

日期：2026-10-06。阶段 A 的方法、初始几何、五项 TightOpt 阈值及预算不变。

## 严格优化

真实水优化的早期输出包含 `THE OPTIMIZATION HAS CONVERGED`，但 ORCA 采用步长超额满足时的替代判据，能量变化和梯度尚未全部低于冻结阈值。项目检查因此正确拒绝发布优化结构，仅保留合格的局部电子能；该历史结果没有被覆盖。

为实现预先声明的全部五项判据，OPI 构造的 `%geom` 显式设置 `EnforceStrictConvergence true`。该开关见 [ORCA 6.1 官方说明](https://www.faccts.de/docs/orca/6.1/manual/contents/structurereactivity/goat.html)，只采用其中描述的优化器选项，不启用 GOAT。独立标准输入同步为 v2，原 v1 输入与早期真实输出保留。没有放宽数值门槛或增加迭代/时间预算。

## JSON 输出

OPI 默认 JSON 配置同时要求 GBW JSON 与 property JSON。首个开发 SP 的真实 stdout 和 `job.2jsonout` 显示内部调用了 ORCA_2JSON，超出阶段 A 后处理预算 0；这个开发运行不算完整执行契约的通过证据。

现在使用 OPI 的 `json_via_input=False` 及显式 `BlockOutput(jsonpropfile=True, jsongbwfile=False)`，只由受管 ORCA 写入需要的原生 property JSON。只读 `Output.parse` 禁止创建或重写任何 JSON。运行收集检测到转换记录时记录实际成本与预算违规并阻止合格输出；进程采样不是完整系统审计，不能单凭没有采到转换进程证明绝无转换。

首个开发运行的实际后处理次数已记录为 1，原预算仍为 0，输入、产物及最初 Result 全部保留。后续通过项单独引用禁 GBW JSON 的运行证据。
