# Python 运行器文件与任务输入分别保存

日期：2026-10-21（Asia/Hong_Kong）。

a025 的第二个 Attempt 是 Python 分析。它合法地将前一 ORCA 的已注册、同 Run、hash 已核对 stdout 映射为任务相对路径 `stdout.out`。Linux helper 随后在同一目录用 `xb` 创建自己的 stdout，发生 `FileExistsError`，尚未进入任务脚本。Windows 后端具有相同命名冲突；Python 的 `.launch.json` 管理文件也与合法任务名称共用目录。这是运行器布局问题，不是输入权限、科学数据或模型必须改名的问题。

新 Run 可在既有执行配置中冻结 `python_runtime_layout="isolated-1"`。缺省 `None` 序列化省略，旧 Run 保持原布局和原文件键。Linux 镜像须在已核 hash 的 manifest 中声明支持该布局；新 helper 仅收到这个固定参数时启用。恢复和收集使用原 Attempt handle 内冻结的执行配置，不读取任务 metadata 决定布局、不升级旧镜像。

采用新布局时，Python 任务 cwd、输入 target_path、代码字节及相对路径均不改变；运行器 stdout、stderr 和启动身份文件单独位于 `/work/python-runtime`。Linux 的任务目录与运行器日志通过一次受限 export 导出，统一执行原有文件数、单文件/总字节和期限限制。宿主分别归档为 `task/...`、`runtime-logs/stdout.out` 和 `runtime-logs/stderr.txt`，保留独立 owner、文件键和 hash。合法 `task/.launch.json` 输入不再被管理文件过滤器跳过；真实运行器启动身份仍由管理 handle 记录。Windows 用相同冻结配置选择 Attempt 内独立日志目录，通过原有继承句柄输出，任务 ACL 不扩到该目录。

两后端均保持 `xb`，不截断、不覆盖既有文件，不自动重写任务代码或重命名输入。ORCA 原生和 SDK 内部 ORCA 的工作目录、日志、启动证明及科学检查保持原契约。新 Python Result 明确记录程序采用的布局，失败摘要、已读日志保留及文件索引选择相应日志文件，不能把同名上游输入当本次执行日志。运行器日志仍是不可信执行数据，不获得科学资格；隔离、累计预算和未知执行对账保持。

验证限于真实 a025 输入的离线复制与 helper 启动路径回放、Linux 固定导出及恢复参数、Windows 日志句柄与不可覆盖检查、同名输入和运行器日志的独立归档及反馈。离线测试不创建进程或容器，不调用模型或 ORCA。新镜像、实际容器探针与后续科学任务应独立记录，不能沿用旧镜像验收结论。

本轮两个直接测试文件共 14 项通过，涉及源码 Ruff 和差异空白检查通过。原 a025 输入 `stdout.out`（SHA-256 `9b63400fa42ad820dcd7249331035399a1492cf38ffb8e08ed75fb4ebc9ecee4`）在临时目录经过生产输入复制和新 helper 路径回放后保持原字节；运行器单独创建日志，所有读取原件前后 hash 相同。回放替换 gate、dup2、execve 为不启动进程的桩，只证明路径与证据保留修复，不宣称任务脚本、隔离或科学结果成功。

随后离线重建镜像 `sha256:364491981f50643cd767e58ce637d79a5eab154cb76fa49dae1c6f78f3dbfdb7`，manifest `72033340b8679b2b85f02eab972e6308dd9fe054db2f4d17163b14a789f093fd`。实际容器探针 p002 通过：一个 Python Attempt 读取三个合法同名输入 `stdout.out`、`stderr.txt`、`.launch.json` 并核对 hash，两个运行日志独立归档，三份原始及任务副本字节不变、租约释放、无未知执行。用量为 0 模型、0 ORCA、0 内部计算工作量及 10.844 秒，纳入同一累计账本；原镜像选择字节与 hash 保留。探针只验证 Python 布局和收集，未重跑原生 ORCA 科学或模型任务，不能把 p001 的原镜像证明改记为新镜像结果。
