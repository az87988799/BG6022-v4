用本题提供的 formaldehyde.xyz 作为起始结构，对气相中性单重态甲醛做一次真实的 ORCA PBE0/def2-SVP TightSCF 几何优化，并在该次收敛的几何上计算谐振频率和 IR 强度。用一个核，%maxcore 192 MB，不作经验频率缩放。可以使用一次 Opt Freq 联合作业，也可以关联两次真实作业的几何。新作业请放在当前工作目录中。

请判断它在此模型下是否是局部极小点，给出内部振动数与实质虚频数。对 C=O 伸缩，请报告 ORCA 原始模式编号、波数（cm⁻¹）、IR 表中 Int 列的强度（km/mol）。用该模式下 C 和 O 的相对位移沿键轴的分量支持归属，并说明它相对其他内部模式的大小。归一化投影采用绝对值 |(uO−uC)·êCO| / ||u||，其中 u 是该模式的全分子笛卡尔位移向量，êCO 取优化结构上的 C→O 单位向量。提供原始输出路径和可定位的行号或段落。

在工作目录根部写入 result.json，包含这些字段：opt_output、freq_output（相对工作目录的 ORCA 原始输出路径，联合计算填同一路径），local_minimum（布尔），internal_modes、imaginary_modes、mode_index（整数），wavenumber_cm1、intensity_km_mol、bond_projection（数值），assignment_evidence（含运动和比较的简短说明），evidence（用 optimization、frequencies、ir、displacement 四个键分别标明源文件和原始行或段名）。最后用中文回答。
