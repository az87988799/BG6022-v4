我想知道，给定这份甲醛几何，能量最低的单重垂直激发也是最容易吸收光的跃迁吗？请用本机 ORCA 做一次真实气相计算。使用 inputs/formaldehyde.xyz 的原始固定几何（Å），总电荷 0，基态多重度 1。采用 PBE0/def2-SVP、TightSCF、显式 TDA true、6 个单重激发根、1 个计算进程和每进程 192 MB MaxCore。保持几何不变。

按能量顺序报告六个根的垂直激发能（eV）与无量纲长度形式振子强度 fosc(D2)。比较最低能根和六根中最亮的根，指出至少一个近暗态（本题以 f ≤ 10⁻⁵ 为界），并用一句话解释能量与吸收强度的关系及比较范围。请保留实际使用的输入和 ORCA 原始文本输出，给出可追溯到原行的证据。

在当前工作目录写 result.json。请填写以下结构（示意中的零和省略项请替换为真实结果）；所有文件路径相对当前工作目录，行号从 1 起算。rows 每个根一项，line 指向 ORCA 原始输出电偶极长度表中的该跃迁行。raw_input 指实际提交的原生 .inp；raw_output 指完整的原始计算输出。
{
  "raw_input": "jobs/attempt-1/job.inp",
  "raw_output": "jobs/attempt-1/job.out",
  "rows": [{"root": 1, "energy_eV": 0, "fosc_length": 0, "line": 0}],
  "lowest_root": 0,
  "brightest_root": 0,
  "near_dark_root": 0,
  "interpretation": "……"
}
