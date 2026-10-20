"""Freeze the repaired candidate and the same task texts, without running a task."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT))
HERE = Path(__file__).resolve().parent
PRIOR = PROJECT / "docs/acceptance/code-as-tools-step7-refreeze-1/freeze"
FREEZE = HERE / "freeze"


def read(path):
    return json.loads(path.read_bytes())


def write(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    commit = sys.argv[1]
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
    for name in ("public-tasks.json", "controls.json", "scoring.md", "knowledge-access.md"):
        (FREEZE / name).write_bytes((PRIOR / name).read_bytes())
    (FREEZE / "prior-holdout-selection.json").write_bytes((PRIOR / "holdout-selection.json").read_bytes())
    runtime = (PRIOR / "runtime.toml").read_text(encoding="utf-8").replace(
        "code-tools-refreeze-1", "code-tools-refreeze-2").replace(
        "analysis_executions = 4", "analysis_executions = 4\nfile_writes = 32")
    (FREEZE / "runtime.toml").write_text(runtime, encoding="utf-8", newline="\n")
    driver = (PRIOR / "run_first_repetition.py").read_text(encoding="utf-8").replace(
        "修复这轮暴露的问题，重新冻结后再把全部任务各跑一轮真实计算。",
        "按这个方向简化，并修复刚才第二轮离线测试产生的问题，修复后开始第二轮真实llm全流程测试")
    (FREEZE / "run_first_repetition.py").write_text(driver, encoding="utf-8", newline="\n")
    (FREEZE / "runbook.md").write_text(
        "# 简化后重新冻结复测\n\n"
        "本候选沿用原 T1—T5、H1、H2 原文及种子策略，各执行一次 repeat=1。"
        "H1/H2 已暴露，不是新保留任务；此前两批结果与累计成本保留。"
        "冻结的 60 槽总矩阵只声明上限，本次驱动仅允许七个 formal 槽，"
        "不启动重复 2/3、参考或控制任务。\n\n"
        "产品提交、配置、执行镜像和评估资产均在执行前冻结，每项前后 verify。"
        "新 Run 使用 file_writes=32，analysis_executions=4；"
        "其余资源、模型和执行预算沿用上批。共享原环境根目录及并发租约。\n\n"
        "直接运行本目录 run_first_repetition.py；驱动只创建原文请求、执行产品主循环、"
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

    cli("freeze-product", "--code-commit", commit, "--batch-root", "data/code-tools-refreeze-2-batch")
    event = read(FREEZE / "product-freeze-event.json")
    holdouts = read(PRIOR / "holdout-selection.json")
    holdouts.update(selected_at_utc=datetime.now(timezone.utc).isoformat(),
                    product_commit=event["product_commit"],
                    product_manifest_sha256=event["product_manifest_sha256"],
                    source_selection_sha256=digest(FREEZE / "prior-holdout-selection.json"))
    write(FREEZE / "holdout-selection.json", holdouts)
    config = load_config(FREEZE / "runtime.toml")
    policy = read(PRIOR / "batch-policy.json")
    policy.update(batch_id="code-tools-refreeze-2",
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
    print(json.dumps({"product_commit": event["product_commit"],
                      "freeze": str(FREEZE), "workloads_started": 0}))


if __name__ == "__main__":
    main()
