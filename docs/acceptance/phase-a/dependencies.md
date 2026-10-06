# 阶段 A 依赖选型与官方依据

核对日期：2026-10-06（Asia/Hong_Kong）。本页记录选型依据；本机安装事实见同目录环境报告，Python 包的精确解析版本及分发 hash 以仓库 `uv.lock` 为准。版本满足最低要求，不代表真实 ORCA、并行或资源限制已验收。

## 核心依赖

| 项目 | 本次选型 / 约束 | 依据与许可证来源 |
| --- | --- | --- |
| Python | 本机工程采用 **CPython 3.11.4**；补丁版记录于 `.python-version` | OPI 2.0.0 声明 `requires-python >=3.11`；Python 为 PSF License v2，所含第三方组件另有条款。[OPI v2.0.0 元数据](https://github.com/faccts/opi/blob/v2.0.0/pyproject.toml)、[Python 许可证](https://docs.python.org/3/license.html) |
| OPI | **`orca-pi==2.0.0`**；导入名 `opi`；不跟随 nightly | 查得稳定发行日期 2026-02-18。公开发行使用 GPL-3.0；上游也提供商业许可。PyPI 的 Other/Proprietary 分类不能替代上游许可正文。[固定版本发行](https://pypi.org/project/orca-pi/2.0.0/)、[v2.0.0 LICENSE](https://github.com/faccts/opi/blob/v2.0.0/LICENSE)、[上游许可说明](https://github.com/faccts/opi#license) |
| ORCA | 首次验收目标 **6.1.1**；OPI 2.0 的最低要求是 6.1.1；发现其他版本不自动扩大支持范围 | ORCA 独立获取并适用其学术或商业许可，不包含在本项目包内；下载需要账户及接受相应协议。[OPI 兼容要求](https://github.com/faccts/opi)、[ORCA 6.1.1 发布](https://www.faccts.de/orca-6-1-1/)、[官方安装指南](https://www.faccts.de/docs/orca/6.1/tutorials/first_steps/install.html) |
| Windows MPI | 只选择 **MS-MPI**；本机发现 v10.1.2（文件版本 **10.1.12498.18**），保留此已装版本进入实测，不自动升级 | 微软当前发布页提供 v10.1.3，并明确列出前版 v10.1.2 的文件版本；源码 MIT，发行安装包遵循随包协议。公共 ORCA 手册并未证明每个 MS-MPI 补丁与 ORCA 6.1.1 的组合都可用。[ORCA 并行约束](https://www.faccts.de/docs/orca/6.1/manual/contents/essentialelements/parallel.html)、[Microsoft 下载与版本](https://www.microsoft.com/en-us/download/details.aspx?id=105289)、[源码许可证](https://github.com/microsoft/Microsoft-MPI/blob/master/LICENSE.txt) |

OPI v2.0.0 的直接依赖下限为 NumPy 2.2.6、platformdirs 4.3.8、Pydantic 2.11.5、RDKit 2025.3.2、semantic-version 2.10.0；各包仍有上限，完整范围见上表所链接的固定 tag 元数据。锁文件负责选择具体版本，不能只固定 OPI 后允许传递依赖漂移。

本仓库只引用第三方包，不复制 ORCA/MPI 安装包或注册凭据。工程及测试依赖的实际版本和许可应随依赖锁一并核对；本页不替本仓库另行选择开源许可证。

2026-10-06 对已同步虚拟环境发行元数据的只读核对如下。许可证名称来自各分发的 `METADATA` / `LICENSE`，表内 PyPI 链接定位相同发行；这份清单包含工程直接依赖及 OPI 传递依赖，不是项目整体许可推断。

| 包 | 已安装版本 | 发行许可记录 |
| --- | --- | --- |
| [Pydantic](https://pypi.org/project/pydantic/2.13.5/) / [pydantic-core](https://pypi.org/project/pydantic-core/2.46.5/) | 2.13.5 / 2.46.5 | MIT |
| [NumPy](https://pypi.org/project/numpy/2.4.6/) | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0（含分发组件） |
| [RDKit](https://pypi.org/project/rdkit/2025.9.6/) | 2025.9.6 | BSD-3-Clause |
| [platformdirs](https://pypi.org/project/platformdirs/4.12.3/) | 4.12.3 | MIT |
| [semantic-version](https://pypi.org/project/semantic-version/2.10.0/) | 2.10.0 | BSD（发行元数据；完整条款见随包 LICENSE） |
| [filelock](https://pypi.org/project/filelock/3.32.7/) | 3.32.7 | MIT |
| [psutil](https://pypi.org/project/psutil/7.2.2/) | 7.2.2 | BSD-3-Clause |
| [pytest](https://pypi.org/project/pytest/9.1.1/) | 9.1.1 | MIT |
| [Ruff](https://pypi.org/project/ruff/0.16.10/) | 0.16.10 | MIT |
| [Pillow](https://pypi.org/project/pillow/12.3.0/) | 12.3.0 | MIT-CMU |
| annotated-types / typing-inspection | 0.8.0 / 0.4.4 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| iniconfig / pluggy | 2.3.0 / 1.6.0 | MIT |
| Pygments | 2.21.0 | BSD-2-Clause |
| colorama | 0.4.6 | METADATA 无 License 值；须以分发内 `licenses/LICENSE.txt` 为准 |

## 本地执行环境的验收边界

候选环境是原生 Windows。官方手册要求 Windows 并行使用 MS-MPI，Linux/macOS 使用 OpenMPI；OPI README 中概括性的 OpenMPI 安装段不能覆盖 Windows 的平台规则。Windows ORCA 的 Typical 安装只包含串行组件，需核对 Custom/Full 安装的并行组件。[ORCA 并行手册](https://www.faccts.de/docs/orca/6.1/manual/contents/essentialelements/parallel.html)、[Windows 安装说明](https://www.faccts.de/docs/orca/6.1/manual/contents/quickstartguide/installation.html)

运行时必须通过受管后端以绝对路径直接启动 ORCA driver，由 driver 启动 MPI 模块。不能把 driver 包在 `mpirun` 中。首个真实计算前还须独立证明 4 核、1024 MB 进程组总内存、全环境最多 1 个计算任务、取消和协调者退出清理；`%maxcore 192` 只是 ORCA 每进程内存参数。MPI 在机器上存在不等于其派生进程已经全部被 Job Object 管理。[ORCA driver 调用规则](https://www.faccts.de/docs/orca/6.1/manual/contents/essentialelements/parallel.html)

本轮环境诊断不得提交计算、触发转换或为获得版本运行试算。无法从文件元数据或已有可信证据确定的版本明确记录为未知；缺环境允许继续离线开发，不能记为真实验收通过。

## OPI 只读接口核对

固定 tag 的 [`Output` 源码](https://github.com/faccts/opi/blob/v2.0.0/src/opi/output/core.py) 显示 `parse()` 默认可能创建缺失 JSON，`True` 还可重写文件。因此后续统一适配器的只读路径须显式使用：

```python
output = Output(basename, working_dir=directory, parse=False)
output.do_redump_jsons = False
output.parse(
    do_create_property_json=False,
    do_create_gbw_json=False,
    read_prop_json=property_json_exists,
    read_gbw_json=gbw_json_exists,
)
```

这是初始选型时核对的接口约束。现已在唯一生产适配器中落实，读取前后文件清单与 hash 有回归验证；property JSON 存在时优先使用并与文本核对，损坏或来源冲突保守拒绝。对本阶段明确支持的 RHF/STO-3G，缺失 JSON 时可以使用同一适配器中已验证的文本读取能力，版本仍须由实际 ORCA 输出确认，不能因为跳过 JSON 就假定版本正确。缺失产物及实际来源写入诊断。没有开启任何 JSON 转换 Tool，后处理预算为 0；后续如需转换，必须独立登记并具备预算、许可和来源记录。
