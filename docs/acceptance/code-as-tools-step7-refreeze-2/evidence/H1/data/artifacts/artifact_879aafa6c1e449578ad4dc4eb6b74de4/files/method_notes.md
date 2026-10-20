# HCN GIAO shielding plan

Requested system: neutral, singlet HCN in the gas phase, atoms ordered H-C-N.

Method choice: PBE0 (RKS for the closed-shell singlet). PBE0 is an allowed one-parameter hybrid functional (25% HF exchange) and is inexpensive for three atoms while including exact-exchange admixture relevant to shielding. Basis choice: def2-TZVP, an allowed polarized valence triple-zeta basis; the larger flexible basis is preferable to def2-SVP for anisotropic GIAO tensors while remaining modest for HCN under the four-core / 192 MB-per-process limits.

Planned ORCA controls: gas phase (no solvent), charge 0, multiplicity 1, RKS, PBE0/def2-TZVP, GIAO NMR shielding for all nuclei. Resource settings will be recorded in the actual input as required by the native input contract.

Random seed policy: any task-local stochastic algorithm will use seed 1701 and be recorded. If a library/model cannot accept a fixed seed, that will be recorded as not fixable rather than fabricated as deterministic. No random-number method is currently required for the geometry-preparation stage.
