# Windows 本地执行：原子 Job 归属与进程树限制

日期：2026-10-06。适用范围：本地 Windows 后端；不增加长期领域对象。

## 问题与选择

阶段 A 必须在进程执行前落实受管树、CPU 范围和总内存限制，并承受协调者强杀。
`Popen → AssignProcessToJobObject` 即使先 suspended 也存在创建成功但尚未归属 Job
的故障窗口。只杀父 PID、`finally` 清理和 ORCA `%maxcore` 都不满足契约。

选择 Windows 10+/Server 2016+ 的 `PROC_THREAD_ATTRIBUTE_JOB_LIST`，通过
`STARTUPINFOEX` 在 `CreateProcessW` 内原子绑定 Job，同时 `CREATE_SUSPENDED`。
首次执行应用代码之前，`on_started` 回调须持久化 PID、精确创建时间、Job 名和启动身份。
API 不支持、嵌套 Job 不兼容或无法落实限制时直接拒绝启动，没有备用执行链。

每次尝试独立创建具名 Job；重用现有名称被拒绝。上层先生成名称并写入启动意图，
后端生成详细启动句柄。Job 设置 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`；
Job 句柄不可继承，显式继承清单只包含 stdin/stdout/stderr。不设置 breakaway 标志。
正常结束以整个 Job 的 `ActiveProcesses == 0` 为准，不能仅检查 driver 退出。

CPU 限制使用 `JOB_OBJECT_LIMIT_AFFINITY`，从协调者可用核中选取 1–4 核；
首次仅支持单处理器组机器，多处理器组环境拒绝启动。总内存通过
`JOB_OBJECT_LIMIT_JOB_MEMORY` 限制 Job 内全部进程的提交虚拟内存，记录
`PeakJobMemoryUsed`。此指标不是工作集，也不是系统总物理内存使用率。
本机拒绝内存提交时，原始 peak 计数可高于配置限值；保留该计数，不将其
截断到上限。限制验证另用直接 `VirtualAlloc` 超限请求的内核拒绝事实，
不能将 peak 计数简单等同于已经成功提交的当前内存。
关联 completion port 收到内存超限消息时终止整个 Job；操作系统也会直接拒绝
超限提交，即使未收到消息也不能突破上限。调用者仍须单独验证 ORCA 科学结果。

取消和超时调用 `TerminateJobObject`，在 10 秒内查询整个 Job 清空。
未确认清空则返回 `unknown`，要求上层保留额度。协调者消失后由内核关闭最后
Job 句柄，终止其受管树，不依赖 Python 的异常处理。结果保留 stdout/stderr。

`reconcile` 为只读对账：先比较 PID 与创建时间，必要时核对 Job 归属；从不发送
终止信号。PID 复用、访问拒绝、部分启动身份或无法确认的状态返回 `unknown`。
确认进程树消失只返回 `terminated`，不宣称科学成功或猜测丢失退出码。
即使观察者仍持有已退出进程对象的句柄，只要精确创建身份匹配、退出码确认已退出且
Job 已不存在，也可确认终止；PID 存在本身不等于进程仍在执行。
启动后句柄尚未保存而崩溃时，Job 仍能清理，但上层不能仅因缺句柄自动补跑。

每次约 50 毫秒通过 Job 本身的 PID 列表进行有界采样，再读取各进程的创建身份、
映像、实际 affinity 及 `IsProcessInJob` 归属；不靠 driver 的 PPID 后代推断 MPI 归属。
结果保留最多 512 个已观察身份、32 个采样诊断和截断标志。短寿命进程可能未被观察，
因此空样本或配置的核数不能证明四核 MPI 已受管。真实验收需直接观察实际 MPI ranks。
调用者可传入确定的 Windows Unicode 环境字典，例如已核验的 ORCA/MPI PATH 与
`OMP_NUM_THREADS=1`；环境内容不写入后端句柄或资源元数据，避免保存继承的凭据。

## 验收影响

`tests/integration/test_backend_windows.py` 使用本地 Python 测试进程验证
父/子/孙进程取消与超时、driver 提前退出、协调者强杀、创建后保存句柄前强制退出、
句柄保存失败、内存拒绝、实际 affinity、breakaway 无法逃出本 Job、PID 复用和保守清理状态。
在嵌套 Job 环境，带 breakaway 标志的创建可以成功，但子进程仍属于禁止
breakaway 的祖先 Job；测试直接核对本次具名 Job 的归属，不误把创建成功当成逃逸。
这些属于真实 Windows 内核证据，不属于真实 ORCA 或 MPI 证据。后者由独立真实用例验收；
未运行时不得声称支持四核 ORCA/MPI。

2026-10-06 本机验证：专项 `15 passed in 11.33s`，限定模块 Ruff 通过。
内存组合对照：96 MiB Job 限额下，单进程提交 64 MiB 成功；另一个受管父进程
同时保有 24 MiB 时，相同子进程的 64 MiB 提交被内核拒绝，尽管该子进程单独
的 private bytes 加请求量仍低于 96 MiB。此对照验证限制覆盖进程树合计量。
复现：`.venv\Scripts\python.exe -m pytest tests/integration/test_backend_windows.py -q`。

## 官方依据

- [扩展启动属性与 JOB_LIST](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)
- [CreateProcessW 与句柄继承](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw)
- [Job 限制、affinity、kill-on-close 与 breakaway](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_limit_information)
- [JobMemoryLimit 与内存计量](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information)
