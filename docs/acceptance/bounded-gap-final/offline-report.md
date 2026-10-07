# 缺口闭合最终离线验证

日期：2026-10-08（Asia/Hong_Kong）。固定候选：`8a2dad5346e22875664fc756cdd3a88127e8d3bb`。本报告只覆盖默认离线验证，**等待用户验收**。真实模型、PubChem、OPI 生成、ORCA 及远程验证须各自记录，不能由本报告替代。

最终独立干净检出为 **2288 passed / 193 skipped / 0 failed / 0 errors**，共 2481 项；JUnit 耗时 673.175 秒，包含进程启动与收尾的耗时 675.437 秒。全库 Ruff 通过，耗时 0.610 秒。完整的精简记录、38 个模块的导入路径、逐源码文件 hash、跳过原因和历次日志 hash 见 [offline-validation.json](offline-validation.json)。

## 检出、依赖与证据

每轮均从本地 Git 仓库创建新的 `--local --no-hardlinks --no-checkout` clone，再 detached checkout 到指定完整提交。没有复制开发者 `data/`；工作目录固定为新 checkout，`PYTHONPATH` 精确指向其 `src`，复用既有依赖解释器。最终运行前逐一真实导入全部 38 个 tracked Python 源码模块，核对各自 `__file__` 精确等于该检出中的对应文件。结束时全部源码 hash 未变、Git 工作树干净，且无 `data/`。

| 项目 | 固定值 |
| --- | --- |
| Git tree | `fea3c6c29f3842f72620e3ea5a453327fc7ff95e` |
| 生产源码 manifest SHA-256 | `91d44f0ace428533b01da95c59b47dbde07d35e0875f7fe9df7864be39ee4b28` |
| `uv.lock` SHA-256 | `616f5e8155ca5522ca7cc488f91ef00a5c73db46e59c6b0497c85db8e56e885b` |
| 解释器 | `E:\BG6022-V4\.venv\Scripts\python.exe`，Python 3.11.4，64 位 |
| 平台 | `Windows-10-10.0.26200-SP0` |
| 依赖 | orca-pi 2.0.0 / RDKit 2025.9.6 / pytest 9.1.1 / Ruff 0.16.10 |

manifest 是 `git ls-files src/*` 中各文件 SHA-256 构成的字典，按键排序、UTF-8、无多余空白的 JSON 再计算 SHA-256；这与美化后的 manifest 文件本身的 hash 不同。最终候选与上一候选 `5085429` 的生产源码 manifest 完全一致，两提交之间仅修改 `tests/unit/test_phase_b_reliability.py`。

运行命令为 `python -m ruff check . --no-cache` 和 `python -u -m pytest -q --junitxml <log-root>/pytest-junit.xml`，没有 `maxfail` 或 live 开关；`PYTEST_ADDOPTS` 清空、`PYTHONDONTWRITEBYTECODE=1`。stdout/stderr 直接写文件，整套自然结束，不重跑单个失败用例。默认离线边界禁止外部网络与科学进程；带 backend 标记的受控 Python 子进程用于 Windows 内核边界测试，不是 ORCA。

最终日志根的精确 JSON 字符串为：

```json
"C:\\WINDOWS\uff3cTEMP\\orca-clean-8a2dad5-933fc4d90c924c4aa268149394a47024"
```

路径中 `WINDOWS` 与 `TEMP` 之间确为 U+FF3C 全角反斜线。原始 stdout、stderr、JUnit 保留在该本地临时目录；本次不复制巨型日志进仓库。临时路径不构成长期、跨机器可用的档案保证。

| 最终原始文件 | SHA-256 |
| --- | --- |
| `pytest-junit.xml` | `3cbd2b2c69ea03da783ede6c5d1e55bc432f70862a6311bd37c46b3df4641eec` |
| `pytest.stdout.log` | `0695dd4fd773ef8af226e622f718121a208e1da5ac9fcb66ed06f817833a37b8` |
| `pytest.stderr.log`（空） | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `ruff.stdout.log` | `82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18` |
| `verification-source.json` | `50f5538c5c09832dd2746fe556db589639ce82f6e66b7bd90cf9a0cc36303ac5` |

## 跳过与前次失败

193 项跳过分别为真实模型未启用 130 项、真实 ORCA 未启用 13 项、独立检出无本地历史档案 49 项、Windows 缺少创建测试符号链接权限 1 项。JSON 保留全部 12 条原始 reason 及计数。跳过不计入通过，也不伪造缺失的历史材料。

| 候选 | 实际结果 | 保留的问题与处理 |
| --- | --- | --- |
| `920eb7e` | 中断；已观察 1091 个通过、185 个 skip、1 个 F，尚无最终 JUnit | 协调者发现独立参考门槛缺陷并撤销候选，核验精确 PID 后中断。只读 collection 将 F 唯一映射到 `test_backend_memory_limit_applies_to_combined_process_tree`；没有 traceback，**具体失败断言未知**。原始失败没有被擦除或猜测改判。 |
| `094200d` | `--maxfail=1` 自然停止；1095 passed / 185 skipped / 1 failed | `test_science_raw_entry_has_frozen_prepared_input_and_no_repeat_execution` 依赖不存在的 `data/acceptance/reference-water_opt.json`。修复提交 `78ec909` 将离线 fixture 绑定到临时合成参考及其 hash。 |
| `5085429` | 完整运行；2286 passed / 193 skipped / 2 failed | 旧脚本未解码压缩上下文，查找目标 `c` 得到 KeyError；旧解释预算用例在最终回答预留规则下，于取证前停止，无法先产生 satisfied 目标。 |
| `8a2dad5` | 完整运行；2288 passed / 193 skipped / 0 failed | 测试脚本适配现有压缩格式及最终回答预算契约，生产源码与 `5085429` 一致。四个历次已定位失败用例在本次完整 JUnit 中均通过。 |

`920eb7e` 正式运行前还发生一次 PowerShell 的 `--junitxml` 参数拼接错误，exit 4、没有测试运行；该 harness 错误日志与纠正后的实际执行记录分别保留。只读 collection 用于定位已有 F，未执行失败测试。四轮均保留各自提交、源码 hash、日志和结果，不用后一轮结果覆盖前一轮。

`3847e4a` 将原有 `atomic_write` 原封提取为仅依赖标准库的模块，Store 保留导出，受控 worker 避免加载整个 Store 导入链；内存限制与断言未放宽。该改动与最终通过均有证据，但不能反推出中断的 `920eb7e` 具体断言。`5085429` 另修复挂起初始 Plan 后的修订计费，并补对应回归。

`8a2dad5` 的测试调整使用已有解码 helper，还检查原始 wire 确有共享字符串、解码后目标 ID 完整、两组纠正各自受限，连续两次恢复不增加发送或费用。解释耗尽用例为已经保留的最终轮次构造非法回复，再验证纠正额度耗尽及目标证据仍在；单调用预算在取证前停止的反例仍由单独测试覆盖。不是增加生产预算，也未削弱既有目标证据断言。

## 前置定向与范围

下列记录来自已提交的各批次报告，组间有重叠，不能相加为独立用例总数；最终完整 2481 项的结果以本报告为准。

| 来源 | 已记录定向结果 |
| --- | --- |
| [批次一](../bounded-gap-batch-1/report.md) | 当前证据 22、目标绑定 19、字段及作用域 16、只读交付 3 项通过；当时干净全量 2024 passed / 193 skipped。 |
| [批次二](../bounded-gap-batch-2/report.md) | 最终优化阶段 24 项通过，相关收集/读取/消费 122 passed / 1 skipped。 |
| [批次三](../bounded-gap-batch-3/report.md) | 协议/原子发布 26、诊断/LLM/V07 108、上下文/预算/输入链/报告 84 项通过；报告原有的 400/4 failed 和 54/1 failed 整合记录及后续修复亦保留。 |
| [批次四、五](../bounded-gap-batches-4-5/report.md) | 结构工具 65、输入绑定安全 13 项通过；文本入口和输入整链纳入最终全量。 |
| `8a2dad5` 提交前 | 协调者报告相关 137 项通过；单列为协调者提供的定向结果，不冒充本次独立全量或新真实证据。 |

上述程序边界、合成 HTTP、替代生成器、历史输出只读回放及脚本模型回复，均不证明真实模型质量、PubChem 实际取证、OPI 生成效果或 ORCA 科学精度。本离线验证增量为零真实 HTTP、零真实结构生成、零 ORCA 启动；正在另行实施的真实固定包及其成本、首次失败、重复结果应另行报告。用户尚未验收，不能将离线通过记为阶段 B 或整套项目验收通过。

