# Final optimization stage replay inputs

`real_methane_opt` contains exact copies of three small input files from the historical independent methane optimization reference. `provenance.json` identifies original locations and SHA-256 values. The larger output and final XYZ are already tracked under `tests/fixtures/phase_b/independent/methane-opt-reference` and are referenced without duplication.

The regression copies these five files into an isolated temporary collection directory, verifies their hashes, runs the production read/check/collection path, and creates a new local Result containing the replay provenance. It does not launch ORCA or a converter, modify historical files or Results, or claim a new real optimization. The normal historical output includes four optimization cycles and a final energy evaluation at the stationary point.

All failure variants in `test_optimization_final_stage.py` are explicitly marked synthetic and are built in temporary directories. They establish parser and downstream-consumption behavior, not real ORCA failure evidence.
