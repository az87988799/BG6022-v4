# Linux Open MPI 开发探针

本目录只提供步骤 2 的环境兼容性探针，未接入生产 Tool、SDK 或后端，不包含 ORCA、产品源码和凭据。它检查真实 `MPI_Init`、4 rank 的 `MPI_Allreduce`、环形点到点 `MPI_Sendrecv` 及 `MPI_Finalize`。仅创建文件或构建成功都不表示验证通过。首次真实执行的日志、镜像身份和容器状态须另行归档。

## 固定输入

- Open MPI **4.1.8**：与官方 ORCA 6.1.1 Linux 下载包 `orca_6_1_1_linux_x86-64_shared_openmpi418.tar.xz` 标明的链接版本一致。
- 官方源码：<https://download.open-mpi.org/release/open-mpi/v4.1/openmpi-4.1.8.tar.gz>。
- SHA256：`fb41086bbed9300baa2f3d7572491facfe5257412fa524ec5a396aa9101d5c62`，见 [Open MPI 官方发布清单](https://www.open-mpi.org/software/ompi/v4.1/)。Dockerfile 不接受覆盖该 URL、版本或校验值。
- 基础镜像必须显式传入官方 `debian:bookworm-slim@sha256:<64 位 digest>`。本轮从 Docker 官方 Hub API 取得的 `linux/amd64` digest 为 `sha256:a4672c0cb26fbdde88e38fa2dfb6c681942306680e41e4378b28770b6e79ee91`；原始元数据保存在本地 `data/container-proof-20261018/debian-tag.json`。仍须记录实际构建所得 image ID。

libevent、hwloc、PMIx 使用所固定源码内的副本，按 [Open MPI 4.1.8 构建说明](https://github.com/open-mpi/ompi/blob/v4.1.8/README) 配置。Debian apt 包（包括供独立边界探针使用的 `python3-minimal`）仍取自官方仓库的构建时版本，尚未固定 snapshot，因此不宣称整个镜像可逐字节重现，也不将这个 Python 选型视为生产依赖已锁定。构建及运行包版本、配置、许可证和 MPI 探针源码/二进制 hash 保存在镜像 `/opt/probe-provenance/`。

## 构建

以下是供开发者执行的 PowerShell 示例。不要将仓库根目录用作 build context，也不要加入 ORCA 安装包、登录 cookie、SSH、secret 或宿主目录挂载。只复制本目录的开发探针 `mpi_probe.c` 和 `boundary_probe.py`；后者由独立边界验证使用，不会在默认 MPI 命令中执行。

```powershell
$probeBase = 'debian:bookworm-slim@sha256:a4672c0cb26fbdde88e38fa2dfb6c681942306680e41e4378b28770b6e79ee91'
docker build --platform linux/amd64 --build-arg "DEBIAN_IMAGE=$probeBase" --tag orca-agent-mpi-probe:4.1.8 scripts/container_probe
docker image inspect orca-agent-mpi-probe:4.1.8
```

构建阶段需要联网下载官方基础镜像、Debian 包与校验过的 Open MPI 源码；它不会启动 MPI 或 ORCA。先保存构建日志，再把实际镜像 ID 用于后续运行。网络只在运行命令中关闭，不能将构建联网与任务环境联网混为一谈。

## 一次有界执行

同一环境只运行一个探针。使用唯一容器名，避免覆盖旧证据。下面示例中的 `$probeImage` 应替换为刚构建且已记录的 `sha256:...` 镜像 ID；运行参数缺失不能视为等价验证。

```powershell
$probeImage = 'sha256:<built-image-id>'
$probeName = 'orca-agent-mpi-probe-001'
docker create --name $probeName --platform linux/amd64 --init --network none --read-only --cap-drop ALL --security-opt no-new-privileges=true --user 10001:10001 --cpus 4 --cpuset-cpus 0-3 --memory 1g --memory-swap 1g --pids-limit 128 --tmpfs /tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777 --shm-size 128m --ulimit core=0 --log-driver json-file --log-opt compress=false --log-opt max-size=1m --log-opt max-file=1 $probeImage
docker start --attach $probeName
$probeAttachExit = $LASTEXITCODE
docker inspect $probeName
docker logs $probeName
```

不要使用 `--privileged`、host network/PID/IPC、额外 capabilities、`seccomp=unconfined` 或 Docker socket 挂载。未指定自定义 seccomp 时保留 Docker 默认过滤。只提供 `/tmp` 和容器私有 `/dev/shm` 的临时写空间，其他根文件系统只读；未绑定宿主工作区。4 CPU 是容器进程树的 CPU 时间配额，1 GiB 是总内存上限，`--memory-swap 1g` 与内存值相同用于禁止额外 swap。应用请求 4 个 rank，不能把配额数当作实际 CPU 使用量。

容器内使用非 root UID/GID 10001。单机启动显式设置 `OMPI_MCA_plm=isolated`，避免默认 `rsh` 组件在没有 SSH 客户端时提前失败；不安装 SSH 或开放宿主通信。MPI 数据通信指定 `ob1` 与 `self,vader`；`btl_vader_single_copy_mechanism=none` 禁用 CMA/KNEM/XPMEM 等单拷贝路径，避免为此增加跨进程权限。该值见 [4.1.8 vader 实现](https://github.com/open-mpi/ompi/blob/v4.1.8/opal/mca/btl/vader/btl_vader_component.c)。MPI 控制通信仍可使用私有回环接口；[Docker `none` 网络](https://docs.docker.com/engine/network/drivers/none/) 保留容器内 loopback，不提供外部接口。探针通过也不能据此宣称任意网络访问已做对抗性验证。

`mpirun --timeout 15` 提供 MPI 的期限，外层 `timeout` 在 18 秒发 TERM、再过 2 秒发 KILL。若 Docker 控制面失联或超过该界限仍显示运行中，停止后续启动，由开发者对账并明确取消该容器；不得无限重试。此开发探针尚未建立生产取消、恢复和预算责任链。

## 证据与通过条件

每个 rank 输出 JSON 行：初始化前 PID/PPID/UID、初始化后的 rank/size/Open MPI 版本、归约与接收值，以及 `MPI_Finalize` 返回值和进程退出码。日志跨进程到达顺序可能不同，按 rank 检查，不能仅搜索一个“成功”字符串。

通过必须同时满足：

1. 容器实际使用指定镜像、上述限制和无宿主挂载；退出后 `State.Running=false`、`State.ExitCode=0`、`State.OOMKilled=false`、`State.Error` 为空，保留客户端 attach 返回码。
2. 恰有 rank 0–3 各一条初始化记录，size=4、UID=10001、运行时版本为 Open MPI 4.1.8，各 rank PID 不同。
3. 每个 rank 的 `allreduce_sum=10`，环形接收值等于前一个 rank，`local_failure=0`、`failing_ranks=0`。
4. 恰有 4 条 finalize 记录，`finalize_code=0`、`exit_code=0`，无遗漏或超时。

保留完整 stdout/stderr、容器 inspect JSON、镜像 inspect JSON、构建日志和 `/opt/probe-provenance/`。可用可信宿主 `docker cp <容器名>:/opt/probe-provenance <本地证据目录>` 采集小文本；不要将下载的 ORCA 包或镜像层提交到 Git。采证后再删除该唯一容器，避免 `--rm` 提前丢失退出事实。

此探针不证明 Python/ORCA 兼容、科学计算正确、文件/凭据访问隔离、资源超限行为、取消/恢复或 SDK 统一责任链。CPU/内存上限的配置读回也不能代替其真实强制效果测试。这些均需后续独立证据；本目录不改变当前生产执行许可。
