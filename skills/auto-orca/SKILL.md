---
name: auto-orca
description: 在 Windows 上辅导 DFT 初学者使用本机 ORCA。适用于比较固定几何能量、判断优化结构是否为极小点、分析振动与 IR、估算构象比例、解读低激发态，或定位已有 ORCA 输出的证据与失败原因。
---

# Auto ORCA

已有输出时先读证据。学生已给几何、方法和所求物理量时，按下方入口直接提交；在交付时解释实际条件与选择理由。需要自己选方法时，开算前简述依据。分子身份、荷电态、自旋、介质或温度的歧义会改变答案时再追问。改变学生指定的科学条件前让学生决定。

## 本机入口

入口是本文件同级的 `scripts/orca_job.py`。在 PowerShell 中将安装目录赋给 `$skillDir`；在仅有 Python 的环境，用当前 `sys.executable` 加脚本路径作为子进程参数。宿主提供 `SKILL_DIR` 时直接取该目录。ORCA 依次从 `--orca`、`ORCA_EXE`、`PATH` 定位。XYZ 单位为 Å，坐标和原文件留在本机。

```powershell
$job = Join-Path $skillDir 'scripts/orca_job.py'
py -3 $job read --output './jobs/previous/job.out' --task optfreq
py -3 $job run --xyz './geometry.xyz' --task sp --method PBE0 --basis def2-SVP --charge 0 --mult 1 --tightscf --cores 1 --maxcore-mb 512 --out-dir './jobs/single-point'
py -3 $job run --input './vertical.inp' --out-dir './jobs/vertical'
```

示例中的条件和资源换成本题指定值。下列入口已经给全常用参数，可在读入几何的同一次工具调用中提交；遇到未覆盖选项时再查 `run --help`。

- **XYZ 作业**。同一命令支持 `--task sp`、`opt`、`freq`、`optfreq`。要求 TightSCF 时加 `--tightscf`，首次生成的 `job.inp` 就会显式列出该关键字。复合方法 `r2SCAN-3c` 自带基组，省略 `--basis`。`--maxcore-mb` 是每个 ORCA 进程的内存。多个固定几何可在一次工具调用中按顺序提交，每个作业使用独立新目录。
- **闭壳层单重态 TD-DFT**。用 `--input` 提交原生输入，方法、基组、SCF 关键字写在 `!` 行，资源写入原生文件，坐标内嵌于 `* xyz 电荷 1` 块。单重态是默认设置。TDA 计算写 `%tddft nroots N tda true end`，将 N 替换为要求的根数；全 TD-DFT 使用 `tda false`。需要显式关闭三重态可在该块加入 `triplets false`。ORCA 6.1.1 拒绝 `singlets true`，旧文件经本入口提交时会自动省去它，原件保存在 `source.inp`。

每次提交后直接核对新目录的 `job.inp`、`job.out` 和 `result.json`；非零退出时也读 `checks`、`evidence`、`missing`。对于已有作业，若要求的是收敛精度，按原输出逐项核对实际 SCF 阈值及收敛记录；证据已满足要求时直接复用。长作业用原会话句柄跟踪，中断后核对进程与原目录再决定是否补算。

## 按证据选择下一步

- **固定几何比较**。对两个结构沿用相同组成、电荷、自旋和模型。分别核对退出状态、SCF、终止标记及能量行。按原始十进制数求 `E(B) − E(A)` 后再舍入，用符号解释方向。
- **极小点、谱峰和优化失败**。用 `read --task optfreq` 查优化和频率各自的证据。只有优化时，从**该次收敛的最终坐标**按相同条件补 `--task freq`。核对元素顺序与最终坐标，分两次作业时分别引用两个输出。做键伸缩归属时，用 `scripts/bond_modes.py --hess <本次作业的 job.hess> --atoms i j`；i、j 是 Hessian 的 `$atoms` 表中**从 1 起计**的原子序号。比较各模式的键轴相对位移，再按模式号到同一作业 IR 表取波数和强度。
- **公开名称、SMILES、构象**。已有 XYZ 直接作为输入。缺几何时用带 RDKit 的 Python 运行 `scripts/prepare_structure.py`。选择 `--name acetone` 查询公开名称，或 `--smiles 'CC(=O)C'` 本地建模，配 `--out-dir` 保存结构、身份及种子。只在构象问题上增加 `--conformers`。多个身份选项影响答案时让学生选定。
- **热力学和光谱**。需要查自由能、偶极、简并度或光谱的判读细节时，可参阅[化学判据与命令](references/chemistry.md)。激发态按要求的吸收表和跃迁编号关联能量与振子强度。超出当前解析范围时，读原始表头、单位和实际条件，指出仍需要的证据。

交付先回答原问题，随后列关键数值、单位、实际条件、原始文件和行号或段名；区分学生指定、继承与假定的条件。遇到失败，引用具体诊断和已有文件。只有新尝试能补足缺口时才另开作业。
