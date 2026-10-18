"""Explicit real Linux evidence; developer authored, with no model calls."""

import json
import os
from pathlib import Path

import pytest

from orca_agent.agent import _science
from orca_agent.config import load_config
from orca_agent.models import (
    BudgetLimits,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
)
from orca_agent.store import Store

pytestmark = pytest.mark.container


def configured(tmp_path):
    path = os.environ.get("ORCA_AGENT_CONTAINER_CONFIG")
    if not path:
        pytest.skip("fixed container configuration missing; unverified")
    config = load_config(Path(path))
    assert config.execution.backend == "linux-container"
    config.data_root = tmp_path / "data"
    return config


def python_task(tmp_path, code, *, scientific=False, resources=None):
    config = configured(tmp_path)
    if resources:
        config.resources = config.resources.model_copy(update=resources)
    store = Store(config.data_root, environment_root=tmp_path / "environment")
    source = tmp_path / "task.py"
    source.write_text(code, encoding="utf-8")
    artifact = store.import_artifact(source, "task_code")
    request = Request(goals=[Goal(id="raw", port="artifacts", quantity="execution evidence",
        minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
        verification="reported_with_limitations")])
    step = Step(id="dynamic", logical_id="dynamic", tool="python.execute", parameters={
        "code": {"artifact_id": artifact.id, "sha256": artifact.sha256, "language": "python"}})
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"raw": OutputBinding(step_id=step.id, port="artifacts")})
    permission = PermissionSnapshot(
        allowed_tools=["python.execute", "orca.run"] if scientific else ["python.execute"],
        code_execution=True, scientific_execution=scientific,
        artifact_writes=True, artifact_ids=[artifact.id])
    run = store.create_run(request, plan, permission,
        BudgetLimits(orca_starts=1 if scientific else 0, extra_orca_starts=0,
                     internal_work_units=1 if scientific else 0, run_seconds=600),
        configuration=config.run_configuration())
    return config, store, run, step


def save_evidence(tmp_path, run, result, outcome):
    evidence = {"run": run.model_dump(mode="json"), "result": result.model_dump(mode="json"),
                "outcome": outcome}
    (tmp_path / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    return evidence


def test_real_task_libraries_and_kernel_denials(tmp_path):
    code = '''import ctypes, errno, json, os, socket, subprocess
from pathlib import Path
import numpy as np
import opi
from rdkit import Chem
assert np.dot(np.array([1., 2.]), np.array([3., 4.])) == 11
assert Chem.MolFromSmiles("CCO").GetNumAtoms() == 3
facts = {"uid": os.getuid(), "status": Path("/proc/self/status").read_text(),
         "cgroup": Path("/proc/self/cgroup").read_text(),
         "mountinfo": Path("/proc/self/mountinfo").read_text(), "denied": {}}
def deny(name, call):
    try:
        value = call()
    except OSError as exc:
        facts["denied"][name] = {"errno": exc.errno, "type": type(exc).__name__}
    else:
        raise AssertionError("unexpected access: " + name + ": " + str(value))
deny("orca_read", lambda: Path("/opt/orca/orca").read_bytes())
deny("orca_exec", lambda: subprocess.run(["/opt/orca/orca", "--version"]))
deny("shell_exec", lambda: subprocess.run(["/bin/sh", "-c", "true"]))
deny("fork", os.fork)
deny("socket_external", lambda: socket.create_connection(("1.1.1.1", 443), timeout=.2))
deny("socket_loopback", lambda: socket.create_connection(("127.0.0.1", 445), timeout=.2))
deny("root_write", lambda: Path("/usr/forbidden").write_text("x"))
deny("control_write", lambda: Path("/work/control/heartbeat").write_text("x"))
deny("cgroup_write", lambda: Path("/sys/fs/cgroup/memory.max").write_text("max"))
for name in ("/var/run/docker.sock", "/run/guest-services", "/mnt/c", "/mnt/e", "/mnt/wsl"):
    assert not Path(name).exists(), name
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
ctypes.set_errno(0)
result = libc.syscall(57)
assert result == -1 and ctypes.get_errno() == errno.EPERM
facts["raw_fork_errno"] = ctypes.get_errno()
Path("boundary.json").write_text(json.dumps(facts, indent=2))
'''
    config, store, run, step = python_task(tmp_path, code)
    result, outcome = _science(store, config, run, step, {}, None, None)
    evidence = save_evidence(tmp_path, run, result, outcome)
    assert outcome["state"] == "completed", evidence
    assert result.observations["evidence_collection_complete"], evidence
    artifact = result.source["files"]["task/boundary.json"]["artifact_id"]
    facts = json.loads(store.artifact_path(artifact).read_bytes())
    assert facts["uid"] == 10001
    assert len(facts["denied"]) == 9
    assert "CapEff:\t0000000000000000" in facts["status"]
    assert "NoNewPrivs:\t1" in facts["status"]
    assert store.environment_lease() is None
    assert run.usage.orca_starts_actual == 0


@pytest.mark.parametrize("case", ["timeout", "memory", "cancel"])
def test_real_task_failure_cleans_the_container(tmp_path, case):
    import threading

    if case == "memory":
        code = "import mmap\nm=mmap.mmap(-1, 2*1024**3)\nfor i in range(0, 2*1024**3, 4096): m[i]=1\n"
    else:
        code = "import time\nfrom pathlib import Path\nPath('before-stop.txt').write_text('retain')\nwhile True: time.sleep(.05)\n"
    config, store, run, step = python_task(
        tmp_path, code, resources={"script_timeout_seconds": 10 if case == "timeout" else 45})
    timer = None

    def hook(point):
        nonlocal timer
        if case == "cancel" and point == "after_resumed":
            timer = threading.Timer(.5, lambda: store.signal(run.id, "cancel"))
            timer.start()

    try:
        result, outcome = _science(store, config, run, step, {}, None, hook)
    finally:
        if timer:
            timer.join(timeout=5)
    evidence = save_evidence(tmp_path, run, result, outcome)
    expected = {"timeout": ("timed_out", "deadline_exceeded"),
                "memory": ("failed", "container_memory_limit_exceeded"),
                "cancel": ("cancelled", "cancel_requested")}
    assert (outcome["state"], outcome["reason"]) == expected[case], evidence
    assert outcome["not_started"] is False and outcome["handle"]["workload_identity"], evidence
    assert outcome["resource_usage"]["active_processes"] == 0, evidence
    if case == "memory":
        assert outcome["resource_usage"]["oom_killed"], evidence
    assert store.environment_lease() is None, evidence
    assert not result.qualified_outputs
    if case != "memory":
        assert "task/before-stop.txt" in result.source["files"], evidence


@pytest.mark.live
def test_dynamic_sdk_four_rank_orca(tmp_path):
    code = '''from pathlib import Path
import json
from orca_task_sdk import run_orca
from rdkit import Chem
assert Chem.MolFromSmiles("O").GetNumAtoms() == 1
Path("water.inp").write_text("""! RHF STO-3G TightSCF NoRI NoAutoStart
%maxcore 192
%pal nprocs 4 end
%scf ConvForced 1 MaxIter 100 end
* xyz 0 1
O 0 0 0
H .757160 0 .586260
H -.757160 0 .586260
*
""")
receipt = run_orca("water.inp", request_key="water_sp")
Path("receipt.json").write_text(json.dumps(receipt, indent=2))
assert receipt["operation_status"] == "completed", receipt
text = Path(receipt["files"]["stdout.out"]).read_text()
assert "ORCA TERMINATED NORMALLY" in text
assert "4 parallel MPI-processes" in text
assert "-74.962991615317" in text
Path("sdk-result.json").write_text(json.dumps({"raw_evidence_read": True,
    "scientific_validation": "unverified"}))
'''
    config, store, run, step = python_task(tmp_path, code, scientific=True)
    result, outcome = _science(store, config, run, step, {}, None, None)
    evidence = save_evidence(tmp_path, run, result, outcome)
    assert outcome["state"] == "completed", evidence
    assert len(run.internal_calls) == 1 and run.internal_calls[0].state == "completed", evidence
    assert run.usage.orca_starts_reserved == run.usage.orca_starts_actual == 1
    assert run.usage.internal_work_units == 1 and run.usage.model_calls == 0
    assert not result.qualified_outputs
    assert "task/sdk-result.json" in result.source["files"]
    assert not result.diagnostics, evidence
    outer = outcome["handle"]
    inner = run.internal_calls[0].execution_handle
    assert outer["container_id"] == inner["container_id"]
    assert store.environment_lease() is None


@pytest.mark.live
@pytest.mark.parametrize("case", ["water_sp", "water_opt"])
def test_opi_four_core_uses_same_native_chain(tmp_path, case):
    from orca_agent.runner import execute, initialize
    from orca_agent.store import sha256_file

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "phase_a" / case
    specification = json.loads((fixture / "request.json").read_text("utf-8"))
    assert specification["steps"][0]["parameters"]["cores"] == 4
    (tmp_path / "geometry.xyz").write_bytes((fixture / "geometry.xyz").read_bytes())
    source = tmp_path / "request.json"
    config = configured(tmp_path)
    # The bounded live test explicitly selects its timeout before freezing the Run.
    for item in specification["steps"]:
        item["parameters"]["timeout_seconds"] = config.resources.job_timeout_seconds
    source.write_text(json.dumps(specification), encoding="utf-8")
    store = Store(config.data_root, environment_root=tmp_path / "environment")
    before = sha256_file(fixture / "geometry.xyz")
    run = initialize(store, config, source)
    run = execute(store, config, run.id)
    assert run.attempts, run.diagnostics
    result = store.load_result(run.id, run.attempts[0].result_id)
    evidence = save_evidence(tmp_path, run, result, result.source["execution"])
    assert run.state == "completed", evidence
    assert run.usage.orca_starts_actual == 1
    assert "energy" in result.qualified_outputs, evidence
    if case == "water_opt":
        assert "optimized_geometry" in result.qualified_outputs, evidence
    assert not (store.path(run.attempts[0].directory) / "task" / "job.2jsonout").exists()
    assert sha256_file(fixture / "geometry.xyz") == before
    assert store.environment_lease() is None
