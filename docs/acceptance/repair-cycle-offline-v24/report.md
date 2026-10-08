# v24 clean offline evidence verification

The clean checkout for `84f42aca55378bbe5c24be3d0e753ff262c07b46` / `agent-json-v24` completed **2919 passed, 202 skipped, 0 failures, 0 errors**, with Ruff exit **0**. This archive independently read and hashed existing evidence; it did not rerun tests or execute a model, ORCA, OPI, Git command, or budget operation.

The original [receipt](receipt.json) is copied byte for byte: SHA256 `549a19c51cebddb37349a59eb7e37f07b2900510a660acb027847591a35206fb`. Its JUnit and all four recorded log hashes match. All **2028 tracked paths** have identical before/after manifests; rereading the retained clean checkout also matches every recorded final hash.

| Timing source | Seconds |
| --- | ---: |
| JUnit suite | 1295.029 |
| pytest final line | 1295.43 |
| pytest subprocess receipt | 1297.438 |

These are separate measurements of the same run and must not be added. Large logs, JUnit and tracked-byte manifests remain at their original paths; [manifest.json](manifest.json) records exact paths, byte sizes and SHA256 values.

The 202 skipped cases comprise **139 real-model**, **13 real-ORCA**, **49 local historical-archive**, and **1 Windows symlink-privilege** case. Exact JUnit reason counts:

| Reason | Cases |
| --- | ---: |
| creating test symlinks requires Windows developer mode/privilege | 1 |
| local real reference archive unavailable; source-backed context unverified | 3 |
| local real-development archive is absent; no replacement evidence is fabricated | 14 |
| phase-A original archive unavailable; immutable restoration unverified | 1 |
| real ORCA not enabled; unverified | 13 |
| real model not enabled; unverified | 139 |
| retained V06 development archive absent; real-request rebuild unverified | 3 |
| retained development receipt unavailable; no live evidence fabricated | 1 |
| retained real N06 receipt absent; real-input replay unverified | 1 |
| retained v15 N06 archive absent; real-input replay unverified | 1 |
| retained v5 real request absent; real-input replay unverified | 1 |
| retained v8 development record absent; real-request replay unverified | 24 |

Python is **3.11.4** in both environments. The original [runtime comparison](runtime-version-comparison.json) is preserved byte for byte. All nine recorded package versions match one another and the clean checkout's `uv.lock`:

| Package | Version |
| --- | --- |
| filelock | 3.32.7 |
| httpx | 0.28.1 |
| openai | 2.28.0 |
| orca-pi | 2.0.0 |
| psutil | 7.2.2 |
| pydantic | 2.13.5 |
| pytest | 9.1.1 |
| rdkit | 2025.9.6 |
| ruff | 0.16.10 |

The sync receipt records `uv sync --offline --locked --group dev` with exit 0; uv is `uv 0.12.23 (46b84fd0b 2026-10-03 x86_64-pc-windows-msvc)`. The copied lock, pyproject and Python version file use the clean checkout's exact bytes and match its tracked manifest. Their working-tree bytes differ only by CRLF/LF line endings; this is recorded explicitly rather than claiming byte identity across checkouts.

The earlier clean run `d31dd4afcf15402b` on `7cd1aafb827c65e7deff7ba29c481b662f3355b0` **failed and was stopped early**. Its independent reproduction identifies the stale `STRING_ENCODING` wording assertion in `test_two_row_compaction_preserves_literal_null_missing_and_trust_paths`. The failed receipt, operator-stop note and reproduction log are copied unchanged. No complete JUnit or full counts exist for that run; the receipt's missing `tests.xml` exception followed the early stop and is not reported as a second product defect. The later focused file run recorded 27 passes. Comparing both clean tracked manifests shows exactly one test-file change and six documentation changes, with **no production-source change**. The final complete run above is separate evidence.

Earlier targeted runs overlap and are not added to 2919. In particular, the last 76-test result was observed only in tool output; no dedicated log/JUnit exists, and none was manufactured here. Detailed prior iteration failures remain in `docs/acceptance/repair-cycle-v24-targeted/`.

This verifies the committed **offline** candidate only. Skipped live and archive cases, real model interpretation, ORCA/remote availability, D3 N06 and later live gates remain outside this report. Scripted single-request capacity does not prove an entire real trajectory fits the cumulative 48000-token budget. This archive grants no execution authority or budget and does not claim overall completion or user acceptance.
