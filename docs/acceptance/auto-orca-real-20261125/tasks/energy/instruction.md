我有两个固定的水分子几何 A 和 B，输入文件是 `inputs/A.xyz` 和 `inputs/B.xyz`（Å）。请用本机 ORCA 分别新算一次气相单点电子能，采用 PBE0/def2-SVP、TightSCF，中性单重态。保持输入几何不变，其他量化设置沿用程序默认值。请告诉我哪个几何电子能更低，以及 B 相对 A 高或低多少，给出带符号的 E(B) − E(A)，单位用 Hartree 和 kJ/mol。

每个几何请保留实际送入 ORCA 的输入文件和完整的原始标准输出，核查 SCF 收敛和正常终止。在答案中标出能量与收敛证据所在的输出段或行号。在当前工作目录写 `result.json`，使用以下字段名。文件路径相对于当前工作目录。

```json
{
  "A": {"input": "<实际输入路径>", "output": "<原始输出路径>", "energy_hartree": 0.0},
  "B": {"input": "<实际输入路径>", "output": "<原始输出路径>", "energy_hartree": 0.0},
  "delta_E_B_minus_A_hartree": 0.0,
  "delta_E_B_minus_A_kJ_mol": 0.0,
  "lower_energy_geometry": "<A 或 B>"
}
```

数值请替换为实算值。Hartree 至少保留小数点后八位，kJ/mol 至少保留三位。
