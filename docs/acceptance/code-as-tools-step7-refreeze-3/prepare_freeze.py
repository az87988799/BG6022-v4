"""Freeze the repaired candidate and the same task texts, without running a task."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT))
HERE = Path(__file__).resolve().parent
PRIOR = PROJECT / "docs/acceptance/code-as-tools-step7-refreeze-2/freeze"
FREEZE = HERE / "freeze"


def read(path):
    return json.loads(path.read_bytes())


def write(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def user_directed_gate(folder, offline_label):
    validation_path = folder / f"{offline_label}-validation.json"
    interruption_path = folder / f"{offline_label}-interruption.json"
    record, interruption = read(validation_path), read(interruption_path)
    instruction = "停止离线测试，开始真实LLM测试"
    if (interruption.get("validation") != offline_label
            or interruption.get("user_instruction") != instruction
            or interruption.get("result") != "user interrupted; not a complete passing offline validation"
            or interruption.get("real_model_started") is not False
            or interruption.get("real_orca_started") is not False
            or record.get("exit_code") != 4294967295
            or record.get("real_model") is not False
            or record.get("real_orca") is not False
            or record.get("external_network") is not False
            or record.get("source_unchanged") is not True
            or record.get("source_before") != record.get("source_after")
            or (folder / f"{offline_label}-junit.xml").exists()
            or any(key in record for key in ("collected", "passed", "failures", "skipped"))):
        raise RuntimeError("User-directed freeze requires the retained interrupted, incomplete validation")
    started = datetime.fromisoformat(record["started_at"])
    interrupted = datetime.fromisoformat(interruption["interrupted_at"].replace("Z", "+00:00"))
    if started.tzinfo is None or interrupted.tzinfo is None or interrupted < started:
        raise RuntimeError("User interruption must follow validation start with explicit timestamps")
    current = {p.relative_to(PROJECT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
               for root in (PROJECT / "src/orca_agent", PROJECT / "tests")
               for p in sorted(root.rglob("*.py"))}
    observed = record["source_after"]
    if {key: value for key, value in observed.items()
            if key.startswith(("src/", "tests/"))} != current:
        raise RuntimeError("User-directed freeze is for different product sources or tests")
    for relative, expected in observed.items():
        path = PROJECT / relative
        if (not path.resolve().is_relative_to(PROJECT.resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
            raise RuntimeError(f"Interrupted validation source or test changed: {relative}")
    if (any((folder / f"orca-{suffix}").exists()
            for suffix in ("plan.json", "pytest.log", "validation.json", "junit.xml"))
            or (PROJECT / "data/workflow-simplification-2-orca").exists()):
        raise RuntimeError("Developer ORCA evidence exists; cannot claim probes were not run")
    return {
        "mode": "userdirected", "user_instruction": instruction,
        "integration": {"path": validation_path.relative_to(PROJECT).as_posix(),
                        "sha256": hashlib.sha256(validation_path.read_bytes()).hexdigest(),
                        "status": "user_interrupted_not_passed", "exit_code": record["exit_code"],
                        "source_unchanged": True, "junit_present": False},
        "authorization": {"path": interruption_path.relative_to(PROJECT).as_posix(),
                          "sha256": hashlib.sha256(interruption_path.read_bytes()).hexdigest(),
                          "interrupted_at": interruption["interrupted_at"]},
        "orca": {"status": "not_run", "reason": "User directed immediate real LLM testing"},
        "next_step": "Same seven formal tasks once; no developer ORCA probe or offline pass claim",
    }


def validation_gates(offline_label, orca_label):
    folder = PROJECT / "docs/acceptance/workflow-simplification-2"
    if not offline_label.isalnum() or not orca_label.isalnum():
        raise ValueError("Validation labels must be alphanumeric")
    if orca_label == "userdirected":
        return user_directed_gate(folder, offline_label)
    labels = {"integration": offline_label, "orca": orca_label}
    records = {name: read(folder / f"{label}-validation.json") for name, label in labels.items()}
    current = {p.relative_to(PROJECT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted((PROJECT / "src/orca_agent").rglob("*.py"))}
    for name, record in records.items():
        if (record["exit_code"] != 0 or record["failures"]
                or record["source_unchanged"] is not True
                or record["source_before"] != record["source_after"]
                or record["collected"] <= 0 or record["real_model"] is not False):
            raise RuntimeError(f"{name} validation did not pass with stable sources")
        observed = record["source_after"]
        if {key: value for key, value in observed.items() if key.startswith("src/")} != current:
            raise RuntimeError(f"{name} validation is for different product sources")
        for relative, expected in observed.items():
            path = PROJECT / relative
            if (not path.resolve().is_relative_to(PROJECT.resolve())
                    or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
                raise RuntimeError(f"{name} validation source or test changed: {relative}")
    offline, probe = records["integration"], records["orca"]
    if (offline["real_orca"] is not False or offline["external_network"] is not False
            or probe["real_orca_enabled"] is not True or probe["skipped"] != 0
            or probe["collected"] != 3 or probe["offline_gate"] != offline_label
            or len(probe["probes"]) != 3
            or {item["kind"] for item in probe["probes"]} != {"direct", "sdk", "sdk_timeout"}
            or any(item["model_calls"] != 0 for item in probe["probes"])):
        raise RuntimeError("Offline regression and all three bounded ORCA probes are required")
    return {name: {"path": (folder / f"{labels[name]}-validation.json").relative_to(PROJECT).as_posix(),
                   "sha256": hashlib.sha256((folder / f"{labels[name]}-validation.json").read_bytes()).hexdigest(),
                   "collected": value["collected"], "skipped": value["skipped"]}
            for name, value in records.items()}


def main():
    if len(sys.argv) not in (2, 4):
        raise ValueError("Usage: prepare_freeze.py COMMIT [OFFLINE_LABEL ORCA_LABEL|userdirected]")
    commit = sys.argv[1]
    gates = validation_gates(*(sys.argv[2:] or ["integration", "orca"]))
    # All batches share the original production environment lease root.
    previous_event = read(PRIOR / "product-freeze-event.json")
    shared = Path(previous_event["environment_root"])
    if not shared.is_dir():
        raise RuntimeError("Original shared environment root is unavailable")
    os.environ.update(LOCALAPPDATA=str(shared.parent.parent),
                      PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    from orca_agent.config import load_config
    from orca_agent.store import default_environment_root
    from tests.helpers.code_tools_acceptance import digest
    from tests.helpers.code_tools_batch import canonical_sha256, snapshot
    if default_environment_root().resolve() != shared.resolve():
        raise RuntimeError("Shared environment root differs")
    FREEZE.mkdir(exist_ok=False)
    write(FREEZE / "validation-gates.json", gates)
    for name in ("public-tasks.json", "controls.json", "scoring.md", "knowledge-access.md"):
        (FREEZE / name).write_bytes((PRIOR / name).read_bytes())
    tasks = read(FREEZE / "public-tasks.json")
    tasks["common_requirements"] = [
        text.replace("计算前说明版本化资料依据、适用性和成本",
                     "计算前说明选择理由、实际来源、适用性和成本；按0056，非用户必需的官方资料检索不作为门槛")
        for text in tasks["common_requirements"]]
    tasks["workflow_revision"] = "0056-simplified-1; task original_text and scientific requirements unchanged"
    (FREEZE / "public-tasks.json").write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    (FREEZE / "prior-holdout-selection.json").write_bytes((PRIOR / "holdout-selection.json").read_bytes())
    runtime = (PRIOR / "runtime.toml").read_text(encoding="utf-8").replace(
        "code-tools-refreeze-2", "code-tools-refreeze-3")
    runtime = "workflow_policy = 'simplified-1'\n" + runtime
    (FREEZE / "runtime.toml").write_text(runtime, encoding="utf-8", newline="\n")
    user_directed = gates.get("mode") == "userdirected"
    authorization = (gates["user_instruction"] if user_directed else
                     "对于流程全部进行简化，然后修复后进行离线测试，测试通过再开始真实LLM测试")
    driver = (PRIOR / "run_first_repetition.py").read_text(encoding="utf-8").replace(
        "按这个方向简化，并修复刚才第二轮离线测试产生的问题，修复后开始第二轮真实llm全流程测试",
        authorization)
    prior_stop = 'if unsettled or run.state == "unknown" or error or knowledge_issues or overruns:'
    if driver.count(prior_stop) != 1:
        raise RuntimeError("Prior driver stop contract differs; review before freezing")
    driver = driver.replace(prior_stop,
        'if unknown or unsettled or run.state == "unknown" or error or knowledge_issues or overruns:')
    (FREEZE / "run_first_repetition.py").write_text(driver, encoding="utf-8", newline="\n")
    validation_note = (
        f"用户最新指令：{authorization}。离线验证由用户中止，未完成且不算通过；"
        "本轮三个开发 ORCA 探针未运行。当前源码及测试仍逐项核对中止记录的哈希，"
        "按用户授权直接进入真实 LLM 七题复测。\n\n" if user_directed else
        "冻结门槛为稳定源码离线验证通过及三个有界开发 ORCA 探针通过。\n\n")
    (FREEZE / "runbook.md").write_text(
        "# 简化后重新冻结复测\n\n" + validation_note
        + "本候选沿用原 T1—T5、H1、H2 原文及种子策略，各执行一次 repeat=1。"
        "H1/H2 已暴露，不是新保留任务；此前三批结果与累计成本保留。"
        "冻结的 60 槽总矩阵只声明上限，本次驱动仅允许七个 formal 槽，"
        "不启动重复 2/3、参考或控制任务。\n\n"
        "产品提交、配置、执行镜像和评估资产均在执行前冻结，每项前后 verify。"
        "新 Run 使用 workflow_policy=simplified-1、file_writes=32、analysis_executions=4；"
        "其余资源、模型和执行预算沿用上批。共享原环境根目录及并发租约。\n\n"
        "prepare_freeze.py 已执行 init-batch 并保存零槽回执；随后运行本目录 run_first_repetition.py。"
        "驱动只创建原文请求、执行产品主循环、"
        "采证与结算。任何未对账执行、冻结漂移、知识正文漂移或超额使后续槽停止。"
        "不得注入解法、替换脚本、开发者补跑失败任务或覆盖原证据。\n\n"
        "模型凭据只由进程环境提供，不能进入冻结资产或产物。"
        "操作完成、科学通过和用户目标完成分别报告，保留 partial 和失败事实。\n",
        encoding="utf-8", newline="\n")

    def cli(action, *args):
        command = [sys.executable, "scripts/code_tools_acceptance.py", action,
                   "--inspect-runtime", "--directory", FREEZE.relative_to(PROJECT).as_posix(), *args]
        result = subprocess.run(command, cwd=PROJECT, capture_output=True, text=True, encoding="utf-8")
        write(HERE / f"{action}.json", {"command": command, "returncode": result.returncode,
                                       "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            raise RuntimeError(f"{action} failed; inspect receipt")
        return json.loads(result.stdout)

    cli("freeze-product", "--code-commit", commit, "--batch-root", "data/code-tools-refreeze-3-batch")
    event = read(FREEZE / "product-freeze-event.json")
    holdouts = read(PRIOR / "holdout-selection.json")
    holdouts.update(selected_at_utc=datetime.now(timezone.utc).isoformat(),
                    product_commit=event["product_commit"],
                    product_manifest_sha256=event["product_manifest_sha256"],
                    source_selection_sha256=digest(FREEZE / "prior-holdout-selection.json"))
    write(FREEZE / "holdout-selection.json", holdouts)
    config = load_config(FREEZE / "runtime.toml")
    policy = read(PRIOR / "batch-policy.json")
    policy.update(batch_id="code-tools-refreeze-3",
                  configuration_sha256=canonical_sha256(config.model_dump(mode="json")))
    for slot in policy["slots"]:
        if slot["category"] == "formal":
            slot["run_budget"] = config.text.budget.model_dump(mode="json")
        else:
            slot["run_budget"]["file_writes"] = min(32, slot["run_budget"]["artifact_count"])
    write(FREEZE / "batch-policy.json", policy)
    write(FREEZE / "prior-batch.json", {
        "source_batch_root": previous_event["batch_root"],
        "snapshot": snapshot(previous_event["batch_root"]),
        "earlier_batch_record": (PRIOR / "prior-batch.json").relative_to(PROJECT).as_posix(),
        "earlier_batch_record_sha256": digest(PRIOR / "prior-batch.json")})
    cli("freeze-evaluation")
    cli("verify")
    cli("init-batch")
    print(json.dumps({"product_commit": event["product_commit"],
                      "freeze": str(FREEZE), "workloads_started": 0}))


if __name__ == "__main__":
    main()
