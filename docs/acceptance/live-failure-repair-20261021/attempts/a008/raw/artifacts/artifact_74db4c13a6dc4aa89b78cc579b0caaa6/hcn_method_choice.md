HCN GIAO shielding calculation plan
- System: HCN, neutral, singlet, gas phase; fixed prepared geometry from the qualified structure-preparation step.
- Method/basis choice: PBE0/def2-TZVP. HCN has only 3 atoms, so the higher-quality triple-zeta basis is affordable; PBE0 is a reasonable hybrid DFT choice for GIAO shielding, while RHF/def2-SVP would be cheaper but lower quality.
- ORCA input will explicitly request RKS, PBE0, def2-TZVP, NoAutoStart, a fixed geometry, and NMR shielding output.
- The requested seed 1701 is recorded at task level; no stochastic search is used here.
- This note is written before the ORCA calculation; no numerical shielding result is asserted here.
