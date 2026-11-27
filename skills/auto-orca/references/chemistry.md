# 教学计算的选法与读数

## 固定几何与偶极

固定除一条 O–H 键长之外的条件，分别作单点计算，才能比较这个几何变化引起的能量差。PBE0 混合泛函和较小的 def2-SVP 基组适合演示这一趋势。水分子示例用气相、中性、单重态。加强基组或改变介质可能改变差值；两套几何的能量排序不足以推出室温布居。偶极问题另取同一态、同一几何的 `DIPOLE MOMENT` 最终大小与 Debye 单位，讨论分量时声明坐标系。带电分子的偶极与原点有关。[ORCA 单点计算教程](https://www.faccts.de/docs/orca/6.1/tutorials/prop/single_point.html)

## 几何、极小点与红外

优化要有明确的收敛标志及对应的最终几何。正常终止可以与达到优化迭代上限同时发生。频率计算在给定几何上检查局部曲率。自由分子通常列出 3N 个笛卡尔模式；线性分子扣除 5 项，非线性分子扣除 6 项。单原子没有内部振动。保留负频的数值和模式编号。微小负值可能源自柔软坐标或数值噪声，先看位移和优化精度；残缺频率表保留已读行及缺口。完整正曲率仅支持所用模型下的局部极小点。[ORCA 优化收敛说明](https://www.faccts.de/docs/orca/6.1/manual/contents/structurereactivity/optimizations.html)

气相丙酮可用 r2SCAN-3c 连做 Opt 和 Freq。这个复合方法内置配套基组和修正项。取得几何与完整频率后，以 `$atoms` 中从 1 起计的羰基 C、O 编号运行下式。原子顺序取自本次作业。查公开名称生成的丙酮示例通常为 2 和 3，上传的 XYZ 可能不同。

```powershell
py -3 (Join-Path $skillDir 'scripts/bond_modes.py') --hess './jobs/acetone-ir/job.hess' --atoms 2 3
```

读同一 Hessian 的几何、频率表和分块的模式矩阵。脚本把两原子的相对位移投影到键轴，并按整条模式向量的范数归一化。模式整体正负号任意。检查排名靠前模式中 C、O 的轴向运动，比较是否有其它混合模式；在同一作业的 IR 表核对模式、波数和强度。报告计算缩放因子以及是否另作经验缩放。丙酮有 10 个原子和 24 个内部振动。没有位移证据时，只给候选峰。[ORCA 红外教程](https://www.faccts.de/docs/orca/6.1/tutorials/spec/IR.html)

## 构象与热力学

先说明室温的取值和介质。用 ETKDGv3 探索构象，力场或可选 xTB 粗排节省 DFT 作业。保存抽样数、种子、去重阈值及所有送入 DFT 的代表。对保留的结构在同一方法、介质、电荷、自旋、温度、标准态及热化学模型下优化并计算频率。只有单点电子能时，应解释缺少热校正和极小点证据，补做相应计算。

正丁烷示例选 298.15 K 气相，可用 `--method r2SCAN-3c --task optfreq --temperature 298.15`。核对优化后的 C–C–C–C 二面角，合并落入同一势阱的重复结构。以最低自由能为零点，权重为 `g × exp[−(G−Gmin)/(RT)]`；若 G 以 Hartree 计，每摩尔换算为 2625.49964 kJ，R 用 0.008314462618 kJ mol⁻¹ K⁻¹。逐个计算两个旁式时各计一次，若只用一个代表则说明镜像简并度 2 的依据。采样命中数不能代替简并度。低频扭转的热校正、隐式溶剂和候选覆盖均可能影响预测比例。[ORCA 热化学教程](https://www.faccts.de/docs/orca/6.1/tutorials/prop/thermo.html)

下面的命令从已有输出重读自由能，核对完整内振动、最终几何、输入模型、温压和热化学参数。它用最终结构识别三个势阱，并报告两个旁式之和。

```powershell
py -3 (Join-Path $skillDir 'scripts/conformer_populations.py') --butane --temperature 298.15 --state anti 1 './jobs/anti/job.out' --state plus 1 './jobs/plus/job.out' --state minus 1 './jobs/minus/job.out'
```

只保留一个镜像代表时，改为两个 `--state`，其中旁式的简并度为 2。对于其它分子，要先根据结构核对每个构象的化学身份、等价性和采样覆盖，再使用通用权重入口。

## 低激发态与常见失败

对于甲醛，指定基态几何，写单作业、内嵌 XYZ 的单重态 TD-DFT 输入。明确泛函、基组、根数及是否用 TDA。小基组的示范预测尤其不能精确代表弥散 Rydberg 态。在 `ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS` 表对齐跃迁标识、能量和无量纲的 `fosc(D2)`。输出还可能列出速度形式，同一跃迁的两列振子强度可以有差异，引用时写出表头。按能量找最低态，按振子强度找所算根内最易吸收的跃迁，给出近零强度和近简并的限制。当前原生输入支持的选项见 `orca_job.py run --help`。遇到多作业、额外依赖或其他未支持语法时保留目标并说明缺口。[ORCA UV/Vis 教程](https://www.faccts.de/docs/orca/6.1/tutorials/spec/UVVis.html)

SCF 失败先查电荷、自旋、几何和实际 SCF 终止行。优化达到迭代上限时查看最后梯度和位移趋势，最后一步可用作续算起点，需明确标为未收敛。遇到相同诊断且没有新修复依据时，交付当前文件、失败事实与哪项问题仍未解决。
