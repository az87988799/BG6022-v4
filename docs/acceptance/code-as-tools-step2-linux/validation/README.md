# Validation commands and interpretation

Run from the repository root. Use a fresh evidence directory for every batch;
pytest `--basetemp` may remove an existing directory. This package records actual
results, including failed development runs and an interrupted broad unit run.

```powershell
# Fixed image 705838... and manifest 535a7187..., model disabled.
$env:ORCA_AGENT_CONTAINER_CONFIG='E:\BG6022-V4\data\container-proof-20261018\runtime-config-002.toml'
.venv\Scripts\python.exe -m pytest tests/live/test_container_execution.py --live-container --live-orca --basetemp=data/container-proof-20261018/live-final-001 --junitxml=data/container-proof-20261018/live-final-001.xml -q
.venv\Scripts\python.exe -m pytest tests/live/test_container_lifecycle.py tests/live/test_container_attacks.py --live-container --basetemp=data/container-proof-20261018/live-security-001 --junitxml=data/container-proof-20261018/live-security-001.xml -q
.venv\Scripts\python.exe -m pytest tests/live/test_container_attacks.py tests/live/test_container_orca_crash.py --live-container --live-orca --basetemp=data/container-proof-20261018/live-security-002 --junitxml=data/container-proof-20261018/live-security-002.xml -q

.venv\Scripts\python.exe -m pytest tests/unit/test_managed_attempt_store.py tests/unit/test_managed_execution.py tests/unit/test_task_sdk.py tests/unit/test_native_orca.py tests/unit/test_runtime_files.py tests/unit/test_code_contract_planning.py tests/unit/test_run_configuration.py tests/unit/test_code_contracts.py tests/unit/test_store.py tests/unit/test_revision_store.py tests/unit/test_orca.py tests/unit/test_optimization_final_stage.py tests/unit/test_execution_environment.py tests/unit/test_container_start_recovery.py tests/test_container_backend.py -q --junitxml=data/container-proof-20261018/scoped-unit-final.xml
.venv\Scripts\python.exe -m pytest tests/integration/test_runner.py tests/integration/test_message_launch_gate.py tests/integration/test_recovery_edges.py tests/integration/test_backend_windows.py tests/integration/test_isolation_windows.py -q --junitxml=data/container-proof-20261018/integration-final.xml
.venv\Scripts\python.exe -m ruff check src tests scripts
.venv\Scripts\orca-agent.exe --config config.local.toml doctor
git diff --check
```

`live-security-001` has six failed test assertions about an empty transported
`files` mapping, preserved in XML and logs. The underlying task refusal was
observed, but this batch is not described as wholly passing. Its four lifecycle
tests passed; all nine attack tests subsequently passed in `live-security-002`.
The latter also includes the actual ORCA crash test, which intentionally returns
a failed/unverified product Result after recovery while the test passes.

`unit-final-001` was interrupted after historical failures; its shell's later
log-display exit code is not a pytest success. It has no completed JUnit result.
`baseline-verification.json` records an independent 205-case HEAD comparison,
not the whole historical suite. The final 131-case comparison regression overlaps
the scoped unit group. Do not add these counts as independent test cases.

The compact execution-catalog edit was made after the three final scientific
successes. It changes the backend availability metadata and the Windows refusal
lookup; the Linux runtime image, launcher and scientific checks were unchanged.
The 131-case final regression covers the catalog and its Windows refusal branch.
No extra scientific runs were added solely for this metadata edit.
