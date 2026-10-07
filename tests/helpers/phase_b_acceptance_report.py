"""Read-only B-11 aggregation; no model, scientific execution or history rewrite.

Offline receipt JSON binds category, freeze_label, freeze_sha256, repetition,
invocation_id, junit_path and junit_sha256. Three repetitions require three
distinct invocations. Missing evidence never becomes a passed slot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from orca_agent.store import Store, atomic_write, sha256_file
from tests.helpers.phase_b_budget import AcceptanceBudget
from tests.helpers.phase_b_freeze import freeze_hash
from tests.helpers.phase_b_grading import classify_grade

PROJECT = Path(__file__).resolve().parents[2]
AXES = ("quantity", "unit", "conditions", "source", "limits", "next_action")


def _read(path):
    path = Path(path)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("acceptance evidence exceeds 16 MiB")
    return json.loads(path.read_text(encoding="utf-8"))


def _store(root):
    store = object.__new__(Store)
    store.root = Path(root).resolve()
    return store


def _freeze(path, project):
    if not path.is_file():
        return {"verified": False, "reason": "formal freeze missing", "freeze_label": None}
    record = _read(path)
    result = {"verified": False, "freeze_label": record.get("freeze_label"),
              "code_commit": record.get("code_commit"), "freeze_sha256": sha256_file(path)}
    try:
        if not record.get("code_commit") or not record.get("files"):
            raise ValueError("freeze lacks commit/file manifest")
        for name, digest in record["files"].items():
            target = (project / name).resolve()
            if (not target.is_relative_to(project) or not target.is_file()
                    or freeze_hash(target, source_text=name in record.get("source_lf_normalization", [])) != digest):
                raise ValueError(f"frozen file differs: {name}")
        result["verified"] = True
    except (ValueError, OSError) as exc:
        result["reason"] = str(exc)
    return result


def _receipt(path):
    return {"path": str(path), "sha256": sha256_file(path)}


def _trajectory(store, run):
    """Bind actual local HTTP receipts, including rejected/failed requests."""
    if not run.model_records:
        return False, False
    corrected = any(d.get("action") == "rejected" for d in run.decisions)
    for record in run.model_records:
        root = f"runs/{run.id}/model/{record['id']}"
        if (sha256_file(store.path(root + ".request.json")) != record["request_hash"]
                or sha256_file(store.path(root + ".response.json")) != record.get("response_record_sha256")):
            raise ValueError("model request/response receipt changed")
        corrected |= bool(record.get("error_category") or record.get("http_status", 0) != 200)
    return True, corrected


def _axes(review, text):
    states = []
    for name in AXES:
        value = review.get("explanation", {}).get(name, {})
        if type(value.get("passed")) is not bool:
            states.append("unverified")
        elif (not value.get("quote") or value["quote"] not in text or not value.get("rationale")):
            raise ValueError("six-axis review does not cite persisted model text")
        else:
            states.append("passed" if value["passed"] else "failed")
    return "failed" if "failed" in states else "unverified" if "unverified" in states else "passed"


def _model_record(metadata_path, model_store):
    from tests.helpers.phase_b_model_cases import evaluate_response
    metadata = _read(metadata_path)
    directory = metadata_path.parent
    row = {k: metadata.get(k) for k in ("variant_id", "repetition", "category", "freeze_label", "freeze_sha256", "run_id")}
    row.update(evidence_type="real_model_with_frozen_evidence", status="unverified", first_success=False,
               correction_or_transport_failure=None, evidence=[_receipt(metadata_path)])
    known_failure = False
    try:
        ready = _read(directory / "ready.json")
        if ready.get("metadata_sha256") != sha256_file(metadata_path) or ready.get("run_id") != row["run_id"]:
            raise ValueError("model slot metadata binding changed")
        grade_path, review_path = directory / "grade.json", directory / "review.json"
        if grade_path.is_file():
            recorded = _read(grade_path)
            identity = ("run_id", "variant_id", "repetition", "spec_sha256")
            if all(recorded.get(key) == metadata.get(key) for key in identity):
                known_failure = classify_grade(recorded) == "failed"
        run = model_store.load_run(row["run_id"])
        if run.batch_category != row["category"]:
            raise ValueError("model Run category differs")
        present, corrected = _trajectory(model_store, run)
        row.update(executed=present, correction_or_transport_failure=corrected)
        if not present:
            row["status"] = "not_run"
        if not grade_path.is_file():
            row["reason"] = "model grade missing"
            return row
        grade = _read(grade_path)
        review = _read(review_path) if review_path.exists() else None
        if review and any(review.get(k) != metadata.get(k) for k in ("variant_id", "repetition", "run_id", "spec_sha256")):
            raise ValueError("model review identity differs")
        actual = evaluate_response(model_store, run, metadata, review=review)
        if any(grade.get(k) != actual.get(k) for k in ("run_id", "variant_id", "repetition", "spec_sha256", "model_text_sha256")):
            raise ValueError("stored model grade differs from current evidence")
        row["evidence"].append(_receipt(grade_path))
        if review_path.is_file():
            row["evidence"].append(_receipt(review_path))
        row["proposal_review"] = actual.get("proposal_review", {"status": "not_verified"})
        row["status"] = classify_grade(actual, executed=present)
        if row["status"] == "passed" and (grade.get("status") != "passed" or not review):
            row["status"] = "unverified"
        row["first_success"] = row["status"] == "passed" and not corrected
        row["six_axes_passed"] = all(a["status"] == "passed" for a in actual["explanation"].values())
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        row.update(status="failed" if known_failure else "unverified", reason=str(exc))
    return row


def _joint_record(metadata_path):
    from tests.helpers.phase_b_grade_joint import grade_joint
    metadata = _read(metadata_path)
    identity = metadata_path.stem
    frozen = metadata.get("freeze") or {}
    label = frozen.get("freeze_label")
    suffix = identity.removeprefix(f"{label}-{metadata['case']}-") if label else ""
    row = {"identity": identity, "joint_case": metadata["case"], "run_id": metadata["run_id"],
           "category": metadata["category"], "freeze_label": label,
           "freeze_sha256": frozen.get("freeze_sha256"), "repetition": int(suffix) if suffix in {"1", "2", "3"} else None,
           "evidence_type": "joint_real_model_orca", "status": "unverified", "first_success": False,
           "all_proposal_facts_passed": None, "semantic_review_passed": None,
           "proposal_review_status": "unverified",
           "correction_or_transport_failure": None, "evidence": [_receipt(metadata_path)]}
    try:
        store = _store(metadata["data_root"])
        run = store.load_run(row["run_id"])
        if run.batch_category != row["category"]:
            raise ValueError("joint Run category differs")
        present, corrected = _trajectory(store, run)
        row.update(executed=present or bool(run.attempts), correction_or_transport_failure=corrected)
        path = metadata_path.with_name(identity + ".grade.json")
        if not path.exists() and row["category"] == "development":
            path = metadata_path.with_name(identity + ".independent-grade.json")
        if not path.exists():
            row.update(status="unverified" if row["executed"] else "not_run", reason="joint grade missing")
            return row
        grade = _read(path)
        actual = grade_joint(store, run, row["joint_case"], metadata=metadata)
        if grade.get("run_id") != run.id or grade.get("case") != row["joint_case"]:
            raise ValueError("joint grade identity differs")
        row["evidence"].append(_receipt(path))
        row["mechanical_passed"] = grade.get("passed") is True and actual["passed"] is True
        review_path = metadata_path.with_name(identity + ".explanation-review.json")
        axes = "unverified"
        if review_path.exists():
            review = _read(review_path)
            if review.get("run_id") != run.id or review.get("category") != row["category"]:
                raise ValueError("joint explanation review identity differs")
            stops = [d for d in run.decisions if d.get("action") == "stop" and d["id"] == review.get("model_record_id")]
            if len(stops) != 1 or stops[0].get("reason") != review.get("exact_model_reason"):
                raise ValueError("joint explanation is not an accepted model stop")
            reason = stops[0]["reason"]
            response = store.path(f"runs/{run.id}/model/{stops[0]['id']}.response.json")
            if (sha256_file(response) != review.get("model_response_record_sha256")
                    or hashlib.sha256(reason.encode()).hexdigest() != review.get("model_reason_sha256")):
                raise ValueError("joint explanation hash differs")
            axes = _axes(review, reason)
            for key in ("all_proposal_facts_passed", "semantic_review_passed"):
                row[key] = review.get(key) if type(review.get(key)) is bool else None
            facts = (row["all_proposal_facts_passed"], row["semantic_review_passed"])
            row["proposal_review_status"] = ("failed" if False in facts else "passed"
                                             if facts == (True, True) else "unverified")
            row["evidence"].append(_receipt(review_path))
        row["six_axes_passed"] = axes == "passed"
        reviews = (axes, row["proposal_review_status"])
        row["status"] = ("failed" if not row["mechanical_passed"] or "failed" in reviews
                         else "passed" if reviews == ("passed", "passed") else "unverified")
        if row["proposal_review_status"] != "passed":
            row["reason"] = "joint proposal facts/semantic review " + row["proposal_review_status"]
        row["first_success"] = row["status"] == "passed" and not corrected
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        row.update(status="unverified", reason=str(exc))
    return row


def _offline(paths, freeze):
    rows, used_invocations, used_xml = [], set(), set()
    for path in paths:
        row = {"status": "unverified", "evidence": []}
        try:
            value = _read(path)
            row.update(repetition=value.get("repetition"), evidence=[_receipt(path)])
            xml_path = Path(value["junit_path"])
            if not xml_path.is_absolute():
                xml_path = Path(path).parent / xml_path
            if (value.get("category") != "formal_offline_fault_injection"
                    or value.get("freeze_label") != freeze.get("freeze_label")
                    or value.get("freeze_sha256") != freeze.get("freeze_sha256")
                    or not freeze["verified"] or row["repetition"] not in (1, 2, 3)):
                raise ValueError("offline receipt is not bound to the active freeze/repetition")
            identity = value.get("invocation_id")
            if not identity or identity in used_invocations or str(xml_path.resolve()) in used_xml:
                raise ValueError("offline repetition reused an invocation")
            used_invocations.add(identity)
            used_xml.add(str(xml_path.resolve()))
            if sha256_file(xml_path) != value["junit_sha256"]:
                raise ValueError("JUnit receipt hash differs")
            if xml_path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("JUnit receipt exceeds 16 MiB")
            nodes = {}
            for test in ET.parse(xml_path).getroot().iter("testcase"):
                node = test.attrib.get("classname", "").replace(".", "/") + ".py::" + test.attrib["name"]
                explicit = test.find("./properties/property[@name='nodeid']")
                if explicit is not None:
                    node = explicit.attrib["value"]
                if node in nodes:
                    raise ValueError("duplicate JUnit node cannot establish independent success")
                nodes[node] = ("failed" if test.find("failure") is not None or test.find("error") is not None
                               else "unverified" if test.find("skipped") is not None else "passed")
            row.update(status="verified", node_status=nodes)
            row["evidence"].append(_receipt(xml_path))
        except (ValueError, KeyError, TypeError, OSError, ET.ParseError) as exc:
            row["reason"] = str(exc)
        rows.append(row)
    return rows


def _rates(rows):
    counts = Counter(row["status"] for row in rows)
    attempted = len(rows) - counts["not_run"]
    first = sum(row.get("first_success", False) for row in rows)
    corrected = [r for r in rows if r.get("correction_or_transport_failure") is True]
    corrected_passed = sum(r["status"] == "passed" for r in corrected)
    return {"required_slots": len(rows), "attempted_slots": attempted,
            "status_counts": {k: counts[k] for k in ("passed", "failed", "unverified", "not_run")},
            "first_successes": first, "final_successes": counts["passed"],
            "rate_denominator": attempted, "first_success_rate": first / attempted if attempted else None,
            "success_after_allowed_correction_rate": counts["passed"] / attempted if attempted else None,
            "corrected_or_transport_failed_slots": len(corrected), "corrected_final_successes": corrected_passed,
            "corrected_subset_success_rate": corrected_passed / len(corrected) if corrected else None}


def _cost(book):
    try:
        before = sha256_file(book.ledger.path)
        ledger = book.snapshot()  # Mandatory immutable receipt/hash validation.
        if sha256_file(book.ledger.path) != before:
            raise ValueError("acceptance ledger changed during the read-only snapshot")
        science = [*ledger["entries"].values(), *ledger.get("agent_science", {}).values()]
        categories = {}
        for category in ("reference", "formal", "development"):
            selected = [e for e in science if e["category"] == category]
            categories[category] = {"reserved": len(selected),
                "known_actual": sum(e.get("orca_starts_actual") or 0 for e in selected),
                "unknown_reservations": sum(e.get("execution_uncertain", True)
                                            or e.get("orca_starts_actual") is None for e in selected)}
        return {"verified": True, "ledger": _receipt(book.ledger.path), "limits": ledger["limits"],
                "model": book._model_totals(ledger), "science": categories,
                "model_by_category": {category: book._model_totals({"model_records": {
                    key: entry for key, entry in ledger.get("model_records", {}).items()
                    if entry["category"] == category}}) for category in ("formal", "development")},
                "science_reserved_total": len(science),
                "money_basis": "frozen maximum uncached price upper bound; provider charges not queried",
                "unknown_policy": "unknown token/cost/science reservations remain occupied, never zeroed"}
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        return {"verified": False, "reason": str(exc), "totals": None}


def build_report(*, coverage_path=None, freeze_path=None, model_root=None, joint_root=None,
                 model_store_root=None, offline_receipts=(), budget=None, project=PROJECT):
    project = Path(project).resolve()
    coverage_path = Path(coverage_path or project / "docs/acceptance/phase-b/coverage.json")
    freeze_path = Path(freeze_path or project / "docs/acceptance/phase-b/formal-freeze.json")
    model_root = Path(model_root or project / "data/phase-b/model-evaluations")
    joint_root = Path(joint_root or project / "data/phase-b/evaluations")
    model_store = _store(model_store_root or project / "data/phase-b/reference")
    coverage, freeze = _read(coverage_path), _freeze(freeze_path, project)
    if freeze["verified"]:
        frozen_files = _read(freeze_path)["files"]
        relative = coverage_path.resolve().relative_to(project).as_posix()
        if relative not in frozen_files:
            freeze.update(verified=False, reason="coverage mapping is not part of the formal freeze")
    variant_ids = [e["variant_id"] for e in coverage["entries"]]
    if (len(variant_ids) != len(set(variant_ids)) or any(
            sorted(s["repetition"] for s in e["formal_slots"]) != [1, 2, 3] for e in coverage["entries"])):
        raise ValueError("coverage must contain unique variants and exactly three slots each")
    sources = ([coverage["frozen_cases"]] if coverage.get("frozen_cases") else []) + coverage.get("additional_cases", [])
    expected = {}
    for source in sources:
        path = (project / source["path"]).resolve()
        if not path.is_relative_to(project) or sha256_file(path) != source["sha256"]:
            raise ValueError("coverage frozen case source differs")
        cases = _read(path)
        additions = {f"{case['id']}/{variant['id']}": variant["evidence_requirement"]
                     for case in cases["cases"] for variant in case["variants"]}
        if expected.keys() & additions.keys():
            raise ValueError("additional coverage cannot replace a frozen variant")
        expected.update(additions)
    if sources:
        observed = {e["variant_id"]: e["evidence_requirement"] for e in coverage["entries"]}
        if observed != expected:
            raise ValueError("coverage omits or relabels a frozen variant")
    records = [_model_record(p, model_store) for p in sorted(model_root.glob("*/*/*/*/metadata.json"))]
    for path in sorted(joint_root.glob("*.json")):
        metadata = _read(path)
        if metadata.get("evidence_type") == "joint_real_model_orca" and metadata.get("category") in {"formal", "development"}:
            records.append(_joint_record(path))
    offline = _offline(offline_receipts, freeze)
    variants = []
    for entry in coverage["entries"]:
        slots = []
        for spec in entry["formal_slots"]:
            slot = {"variant_id": entry["variant_id"], "repetition": spec["repetition"],
                    "evidence_type": entry["evidence_requirement"], "status": "not_run", "first_success": False}
            if slot["evidence_type"] == "offline_fault_injection":
                matches = [r for r in offline if r.get("repetition") == slot["repetition"]]
                if len(matches) == 1 and matches[0]["status"] == "verified":
                    record = matches[0]
                    states = [record["node_status"].get(n, "unverified") for n in spec["pytest_nodeids"]]
                    slot.update(status="failed" if "failed" in states else "passed" if states and all(s == "passed" for s in states) else "unverified",
                                evidence=record["evidence"])
                    slot["first_success"] = slot["status"] == "passed"
                elif matches:
                    slot.update(status="unverified", reason="offline invocation missing, duplicated or unverified")
            else:
                matches = [r for r in records if r["category"] == "formal"
                           and r["freeze_label"] == freeze.get("freeze_label") and r["repetition"] == slot["repetition"]
                           and (r.get("variant_id") == entry["variant_id"] if "joint_case" not in spec
                                else r.get("joint_case") == spec["joint_case"])]
                if len(matches) == 1:
                    slot.update(matches[0])
                    slot["variant_id"] = entry["variant_id"]
                    if not freeze["verified"] or slot.get("freeze_sha256") != freeze.get("freeze_sha256"):
                        slot.update(status="unverified", first_success=False, reason="formal freeze binding not verified")
                elif matches:
                    slot.update(status="unverified", reason="multiple Runs occupy the same formal slot")
            slots.append(slot)
        variants.append({"variant_id": entry["variant_id"], "slots": slots, **_rates(slots)})
    slots = [s for v in variants for s in v["slots"]]
    run_slots = {}
    for slot in slots:
        if slot.get("run_id"):
            run_slots.setdefault((slot["evidence_type"], slot["run_id"]), []).append(slot)
    for repeated in run_slots.values():
        identities = {(s.get("joint_case", s["variant_id"]), s["repetition"]) for s in repeated}
        if len(identities) > 1:
            for slot in repeated:
                slot.update(status="unverified", first_success=False,
                            reason="one Run cannot count as independent repetitions or distinct trajectories")
    for variant in variants:
        variant.update(_rates(variant["slots"]))
    cost = _cost(budget or AcceptanceBudget(model_store))
    return {"schema_version": 1, "kind": "read_only_acceptance_aggregation", "freeze": freeze,
            "coverage": _receipt(coverage_path), "variants": variants, "formal": _rates(slots),
            "by_evidence_type": {kind: _rates([s for s in slots if s["evidence_type"] == kind])
                                 for kind in sorted({s["evidence_type"] for s in slots})},
            "development_records": [r for r in records if r["category"] == "development"],
            "development_by_evidence_type": {kind: _rates([r for r in records
                if r["category"] == "development" and r["evidence_type"] == kind])
                for kind in sorted({r["evidence_type"] for r in records if r["category"] == "development"})},
            "other_formal_batches": [r for r in records if r["category"] == "formal" and r["freeze_label"] != freeze.get("freeze_label")],
            "offline_receipts": offline, "cost": cost,
            "unverified_or_unrun_slots": [{k: s[k] for k in ("variant_id", "repetition", "status")}
                                         for s in slots if s["status"] in {"not_run", "unverified"}],
            "passed": bool(slots) and freeze["verified"] and cost["verified"] and all(s["status"] == "passed" for s in slots),
            "limits": ["Development and earlier freezes never fill current formal slots.",
                       "Mechanical grading never replaces independent six-axis explanation review.",
                       "Joint success also requires explicit proposal-fact and semantic review passes; missing fields remain unverified.",
                       "Rates include every attempted failed/unverified slot; not_run slots are reported separately.",
                       "Joint water-SP and repair variants may share one trajectory; ledger cost is counted once."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--offline-receipt", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(freeze_path=args.freeze, offline_receipts=args.offline_receipt)
    atomic_write(args.output, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode(), immutable=True)


if __name__ == "__main__":
    main()
