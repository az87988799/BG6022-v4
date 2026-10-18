"""Actual container attacks and broker refusals; no model or ORCA calculation."""

import base64
import hashlib
import json
import os
import secrets
import socket
from pathlib import Path

import pytest
from test_container_execution import configured, python_task, save_evidence

from orca_agent.agent import _science
from orca_agent.backends import container
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
# Capture the real host syscall before pytest installs its offline network shim.
# Only the explicit 127.0.0.1 canary positive control below uses this method.
HOST_CONNECT = socket.socket.connect
NATIVE_INPUT = """! RHF STO-3G TightSCF NoRI NoAutoStart
%maxcore 192
%pal nprocs 4 end
* xyz 0 1
H 0 0 0
H 0 0 .74
*
"""


def _read_fact(store, result, name="attack.json"):
    reference = result.source["files"]["task/" + name]
    return json.loads(store.artifact_path(reference["artifact_id"]).read_bytes())


def _assert_broker_receipt(store, run, facts):
    trusted = store.path(run.attempts[0].directory) / "responses/response.json"
    value = json.loads(trusted.read_bytes())
    # The container transport always supplies its (here empty) file mapping.
    assert "files" not in value and facts["receipt"] == {**value, "files": {}}


def _zero_starts(store, run, outcome):
    assert run.usage.orca_starts_actual == run.usage.orca_starts_reserved == 0
    assert run.usage.internal_work_units == run.usage.model_calls == 0
    assert run.internal_calls == []
    assert outcome["handle"]["backend"] == "linux-container"
    assert outcome["handle"]["workload_identity"]["uid"] == 10001
    assert store.environment_lease() is None
    assert container.reconcile(outcome["handle"])["state"] == "terminated"


def _request_task(tmp_path, code):
    """Reach the actual native broker with authority, but with zero launch budget."""
    config = configured(tmp_path)
    config.resources = config.resources.model_copy(update={"script_timeout_seconds": 45})
    store = Store(config.data_root, environment_root=tmp_path / "environment")
    source = tmp_path / "task.py"
    source.write_text(code, encoding="utf-8")
    artifact = store.import_artifact(source, "task_code")
    request = Request(
        goals=[
            Goal(
                id="raw",
                port="artifacts",
                quantity="actual broker refusal",
                minimum_check_version="generic-observation-1",
                evidence_contract_version="generic-observation-1",
                verification="reported_with_limitations",
            )
        ]
    )
    step = Step(
        id="dynamic",
        logical_id="dynamic",
        tool="python.execute",
        parameters={
            "code": {"artifact_id": artifact.id, "sha256": artifact.sha256, "language": "python"}
        },
    )
    plan = Plan(
        request_id=request.id,
        steps=[step],
        goal_map={"raw": OutputBinding(step_id=step.id, port="artifacts")},
    )
    permission = PermissionSnapshot(
        allowed_tools=["python.execute", "orca.run"],
        code_execution=True,
        scientific_execution=True,
        artifact_writes=True,
        artifact_ids=[artifact.id],
    )
    run = store.create_run(
        request,
        plan,
        permission,
        BudgetLimits(orca_starts=0, extra_orca_starts=0, internal_work_units=0, run_seconds=300),
        configuration=config.run_configuration(),
    )
    return config, store, run, step


REQUEST_PREAMBLE = """import hashlib, json, os, time
from pathlib import Path
requests = Path(os.environ["ORCA_TASK_REQUESTS"])
responses = Path(os.environ["ORCA_TASK_RESPONSES"])
def submit(value):
    pending = requests / "request.pending"
    pending.write_text(json.dumps(value))
    os.replace(pending, requests / "request.json")
def response():
    deadline = time.monotonic() + 25
    while not (responses / "response.json").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("actual broker response missing")
        time.sleep(.05)
    return json.loads((responses / "response.json").read_bytes())
"""


def _request():
    return {"version": 1, "request_key": "attack", "input": NATIVE_INPUT, "dependencies": []}


def _host_aliases(path):
    absolute = path.resolve()
    if os.name == "nt":
        drive = absolute.drive.rstrip(":").lower()
        tail = "/".join(absolute.parts[1:])
        return [f"/mnt/{drive}/{tail}", f"/run/desktop/mnt/host/{drive}/{tail}"]
    return [str(absolute)]


def test_real_host_source_and_credential_canaries_and_host_socket_denied(tmp_path, monkeypatch):
    host_only = tmp_path / "host-only"
    host_only.mkdir()
    source_canary = host_only / "source-canary.py"
    credential_canary = host_only / "credentials-canary.txt"
    source_canary.write_text("# " + secrets.token_hex(32) + "\n", encoding="ascii")
    credential_canary.write_text("FAKE_TEST_KEY=" + secrets.token_hex(32), encoding="ascii")
    repository_source = Path(__file__).resolve().parents[2] / "src/orca_agent/task_sdk.py"
    paths = [source_canary, credential_canary, repository_source]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    # This is a synthetic token, never a real API key or user credential.
    monkeypatch.setenv("ORCA_ATTACK_CREDENTIAL_CANARY", secrets.token_hex(32))
    aliases = [candidate for path in paths for candidate in _host_aliases(path)]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        listener.settimeout(1)
        address = listener.getsockname()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as positive:
            positive.settimeout(1)
            HOST_CONNECT(positive, address)
            accepted, peer = listener.accept()
            with accepted:
                accepted.sendall(b"host-canary-ready")
                assert positive.recv(64) == b"host-canary-ready"
        code = """import errno, json, os, socket
from pathlib import Path
facts = {"paths": [], "network": [], "environment_canary_absent":
    "ORCA_ATTACK_CREDENTIAL_CANARY" not in os.environ}
for name in HOST_PATHS:
    for action, flags in (("read", os.O_RDONLY), ("write", os.O_WRONLY)):
        try:
            descriptor = os.open(name, flags | os.O_NOFOLLOW)
        except OSError as exc:
            facts["paths"].append({"path": name, "action": action, "errno": exc.errno})
        else:
            os.close(descriptor)
            raise AssertionError("host path unexpectedly accessible: " + name)
for host in ("127.0.0.1", "host.docker.internal", "gateway.docker.internal"):
    try:
        channel = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        facts["network"].append({"host": host, "port": HOST_PORT,
                                 "phase": "socket", "errno": exc.errno})
        assert exc.errno == errno.EPERM
    else:
        channel.close()
        raise AssertionError("task created a network socket")
assert facts["environment_canary_absent"]
Path("attack.json").write_text(json.dumps(facts, indent=2))
"""
        code = f"HOST_PATHS={aliases!r}\nHOST_PORT={address[1]}\n" + code
        config, store, run, step = python_task(tmp_path, code)
        result, outcome = _science(store, config, run, step, {}, None, None)
        evidence = save_evidence(tmp_path, run, result, outcome)
        assert outcome["state"] == "completed", evidence
        assert result.observations["evidence_collection_complete"], evidence
        facts = _read_fact(store, result)
        assert len(facts["paths"]) == len(aliases) * 2 and len(facts["network"]) == 3
        listener.settimeout(0.2)
        with pytest.raises(TimeoutError):
            listener.accept()
    assert {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths} == before
    _zero_starts(store, run, outcome)
    actual = json.loads(
        container._command(
            config.execution, ["container", "inspect", outcome["handle"]["container_id"]]
        )[1]
    )[0]
    assert all(mount["Type"] == "volume" for mount in actual["Mounts"])
    assert actual["HostConfig"]["NetworkMode"] == "none"
    (tmp_path / "host-canaries.json").write_text(
        json.dumps(
            {
                "file_hashes_before_and_after": before,
                "listener": address,
                "positive_control_peer": peer,
                "positive_control_passed": True,
                "additional_connections": 0,
                "mounts": actual["Mounts"],
                "task_evidence": facts,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("case", ["forged_authority", "boolean_version", "dependency_traversal"])
def test_actual_broker_rejects_malicious_schema_before_reservation(tmp_path, case):
    value = _request()
    expected = "invalid bounded SDK request"
    if case == "forged_authority":
        value["permission"] = {"scientific_execution": True, "shell_execution": True}
        value["command"] = "/bin/sh"
    elif case == "boolean_version":
        value["version"] = True
    else:
        value["dependencies"] = [
            {
                "target_path": "../../escape.txt",
                "content_base64": base64.b64encode(b"escape-canary").decode(),
            }
        ]
        expected = "unambiguous relative task path"
    code = REQUEST_PREAMBLE + f"value={value!r}\nsubmit(value)\nreceipt=response()\n"
    code += 'Path("attack.json").write_text(json.dumps({"request": value, "receipt": receipt}))\n'
    code += f'assert {expected!r} in receipt.get("error", ""), receipt\n'
    config, store, run, step = _request_task(tmp_path, code)
    result, outcome = _science(store, config, run, step, {}, None, None)
    evidence = save_evidence(tmp_path, run, result, outcome)
    assert outcome["state"] == "completed", evidence
    assert result.observations["evidence_collection_complete"], evidence
    facts = _read_fact(store, result)
    _assert_broker_receipt(store, run, facts)
    assert expected in facts["receipt"]["error"]
    assert "budget" not in facts["receipt"]["error"].lower()
    _zero_starts(store, run, outcome)


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_actual_broker_denies_linked_request_file(tmp_path, kind):
    code = REQUEST_PREAMBLE + f"value={_request()!r}\nkind={kind!r}\n"
    code += """payload = requests / "payload.json"
payload.write_text(json.dumps(value))
target = requests / "request.json"
if kind == "symlink":
    target.symlink_to(payload)
else:
    os.link(payload, target)
observed = {"is_symlink": target.is_symlink(), "nlink": target.stat().st_nlink}
receipt = response()
Path("attack.json").write_text(json.dumps({"kind": kind, "observed_link": observed,
    "receipt": receipt}))
assert "mailbox read rejected" in receipt.get("error", ""), receipt
"""
    config, store, run, step = _request_task(tmp_path, code)
    result, outcome = _science(store, config, run, step, {}, None, None)
    evidence = save_evidence(tmp_path, run, result, outcome)
    assert outcome["state"] == "completed", evidence
    assert result.observations["evidence_collection_complete"], evidence
    facts = _read_fact(store, result)
    _assert_broker_receipt(store, run, facts)
    assert facts["observed_link"]["is_symlink"] is (kind == "symlink")
    if kind == "hardlink":
        assert facts["observed_link"]["nlink"] == 2
        assert "regular single-link file" in facts["receipt"]["error"]
    else:
        assert "Too many levels of symbolic links" in facts["receipt"]["error"]
    _zero_starts(store, run, outcome)


def test_task_cannot_replace_receipt_or_replay_broker_request(tmp_path):
    first = {**_request(), "version": False}
    second = {**_request(), "request_key": "replay"}
    code = REQUEST_PREAMBLE + f"first={first!r}\nsecond={second!r}\n"
    code += """submit(first)
receipt = response()
assert "invalid bounded SDK request" in receipt.get("error", ""), receipt
original = (responses / "response.json").read_bytes()
denied = []
for action in ("overwrite", "unlink"):
    try:
        if action == "overwrite":
            (responses / "response.json").write_text('{"operation_status":"completed"}')
        else:
            (responses / "response.json").unlink()
    except PermissionError as exc:
        denied.append({"action": action, "errno": exc.errno})
    else:
        raise AssertionError("task changed trusted response")
submit(second)
time.sleep(2.5)
after = (responses / "response.json").read_bytes()
Path("attack.json").write_text(json.dumps({"first_request": first, "replay_request": second,
    "receipt": receipt, "receipt_mutations_denied": denied,
    "before_sha256": hashlib.sha256(original).hexdigest(),
    "after_sha256": hashlib.sha256(after).hexdigest()}))
assert original == after and len(denied) == 2
"""
    config, store, run, step = _request_task(tmp_path, code)
    result, outcome = _science(store, config, run, step, {}, None, None)
    evidence = save_evidence(tmp_path, run, result, outcome)
    assert outcome["state"] == "completed", evidence
    assert result.observations["evidence_collection_complete"], evidence
    facts = _read_fact(store, result)
    _assert_broker_receipt(store, run, facts)
    assert facts["before_sha256"] == facts["after_sha256"]
    _zero_starts(store, run, outcome)


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_real_output_link_is_retained_as_failure_not_followed(tmp_path, kind):
    code = (
        f"kind={kind!r}\n"
        + """import json, os
from pathlib import Path
Path("ordinary.txt").write_text("ordinary task data")
if kind == "symlink":
    Path("redirect").symlink_to("/opt/orca/orca")
else:
    os.link("ordinary.txt", "redirect")
Path("attack.json").write_text(json.dumps({"kind": kind,
    "is_symlink": Path("redirect").is_symlink(),
    "nlink": Path("redirect").lstat().st_nlink}))
print("output link created", flush=True)
"""
    )
    config, store, run, step = python_task(tmp_path, code)
    result, outcome = _science(store, config, run, step, {}, None, None)
    evidence = save_evidence(tmp_path, run, result, outcome)
    assert outcome["exit_code"] == 0, evidence  # Task reached its real filesystem attack.
    assert outcome["state"] == "failed" and outcome.get("collection_error"), evidence
    expected = "linked output entry" if kind == "symlink" else "regular single-link file"
    assert expected in outcome["collection_error"], evidence
    assert not result.observations["evidence_collection_complete"], evidence
    assert not result.qualified_outputs
    _zero_starts(store, run, outcome)
    handle = outcome["handle"]
    container._command(
        config.execution,
        [
            "cp",
            f"{handle['container_id']}:/work/task/attack.json",
            str(tmp_path / "raw-attack.json"),
        ],
    )
    facts = json.loads((tmp_path / "raw-attack.json").read_bytes())
    assert facts["is_symlink"] is (kind == "symlink")
    if kind == "hardlink":
        assert facts["nlink"] == 2
    root = store.path(run.attempts[0].directory)
    receipts = [json.loads(path.read_bytes()) for path in root.glob("container-export-*.json")]
    assert receipts and all(value["complete"] is False for value in receipts)
    assert all(value["volume_name"] == handle["volume_name"] for value in receipts)
    assert all((root / value["raw_transport"]).is_file() for value in receipts)
