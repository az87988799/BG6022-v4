"""Bounded public-document retrieval and non-scientific answer receipts."""

import hashlib
import re
from html.parser import HTMLParser
from typing import Literal

import httpx
from pydantic import Field

from orca_agent.models import Identifier, Record, utc_now

RULE = "knowledge-answer-1"
DOCUMENTS = {
    "orca_electric": ("ORCA 6.1 Electrical Properties", "https://www.faccts.de/docs/orca/6.1/manual/contents/spectroscopyproperties/electric.html"),
    "orca_optimization": ("ORCA 6.1 Geometry Optimization", "https://www.faccts.de/docs/orca/6.1/manual/contents/structurereactivity/optimizations.html"),
    "orca_scf": ("ORCA 6.1 SCF", "https://www.faccts.de/docs/orca/6.1/manual/contents/essentialelements/scf.html"),
    "orca_properties": ("ORCA 6.1 Property File", "https://www.faccts.de/docs/orca/6.1/manual/contents/utilitiesvisualization/property_file_list.html"),
    "opi": ("OPI 2.0 Introduction", "https://www.faccts.de/docs/opi/2.0/docs/contents/notebooks/how_to_opi.html"),
}


class SearchParameters(Record):
    document: Literal["orca_electric", "orca_optimization", "orca_scf", "orca_properties", "opi"]
    query: str = Field(min_length=1, max_length=120)


class AnswerParameters(Record):
    goal_id: Identifier
    answer: str = Field(min_length=1, max_length=2500)
    source_result_ids: list[Identifier] = Field(default_factory=list, max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=5)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0
        self.in_article = False
        self.buffer = []

    def flush(self):
        value = " ".join(self.buffer).strip()
        if value:
            self.parts.append(value)
        self.buffer = []

    def handle_starttag(self, tag, attrs):
        if tag == "article":
            self.in_article = True
        if tag in {"script", "style", "nav"}:
            self.skip += 1
        if self.in_article and not self.skip and tag in {"p", "pre", "li", "h1", "h2", "h3", "h4"}:
            self.flush()

    def handle_endtag(self, tag):
        if self.in_article and not self.skip and tag in {"p", "pre", "li", "h1", "h2", "h3", "h4", "article"}:
            self.flush()
        if tag == "article":
            self.in_article = False
        if tag in {"script", "style", "nav"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self.in_article and not self.skip and data.strip():
            self.buffer.append(data.strip())


def matched_excerpt(parts, query):
    """Keep paragraphs together and prefer the requested phrase over nearby terms."""
    terms = list(dict.fromkeys(re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", query.lower())))
    if not terms:
        return "", []
    phrase = " ".join(terms)
    ranked = []
    for index, paragraph in enumerate(parts):
        normalized = " ".join(re.findall(r"[a-z0-9_-]+", paragraph.lower()))
        hits = sum(bool(re.search(r"\b" + re.escape(term) + r"\b", normalized)) for term in terms)
        score = hits + 10 * (hits == len(terms)) + 40 * (phrase in normalized)
        if hits:
            ranked.append((score, index))
    chosen = sorted(index for _, index in sorted(ranked, key=lambda pair: (-pair[0], pair[1]))[:3])
    return "\n\n".join(parts[index][:1800] for index in chosen)[:5400], chosen


def retrieve(store, run, call):
    title, url = DOCUMENTS[call.parameters["document"]]
    # Exactly one GET; no redirects, cookies, credentials, search engine, or retries.
    raw = bytearray()
    with httpx.Client(timeout=12, follow_redirects=False, trust_env=False) as client:
        with client.stream("GET", url, headers={"User-Agent": "ORCA-Agent/0.1 documentation reader"}) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 1024 * 1024:
                    raise ValueError("document exceeds 1 MiB retrieval bound")
    parser = _Text()
    parser.feed(raw.decode("utf-8", errors="replace"))
    parser.flush()
    excerpt, indices = matched_excerpt(parser.parts, call.parameters["query"])
    return {"status": "found" if excerpt else "missing", "title": title, "url": url,
            "published_at": None, "accessed_at": utc_now().isoformat(), "query": call.parameters["query"],
            "document_sha256": hashlib.sha256(raw).hexdigest(), "excerpt": excerpt,
            "coverage": {"complete": False, "scope": "bounded matched document paragraphs",
                         "paragraph_indices": indices, "paragraph_count": len(parser.parts)},
            "scientific_qualification": False, "retrieval": "versioned official-document index; one HTTPS GET"}


def question_source_facts(store, source_run):
    """Exact bounded report fields for explanations, without duplicate inventories."""
    from orca_agent.report import build_report
    fields = {"goal_id", "port", "current_conditions", "source_conditions", "geometry_relation",
              "source_geometry_artifact_id", "recorded_status", "current_evidence_status",
              "goal_complete", "answer", "gaps", "minimum_check_version"}
    rows = []
    for fact in build_report(store, source_run)["goal_facts"]:
        row = {key: value for key, value in fact.items() if key in fields}
        row["source"] = {key: value for key, value in (fact.get("source") or {}).items()
                         if key in {"run_id", "result_id", "attempt_id", "call_id"}}
        rows.append(row)
    return rows


def answer(store, run, call):
    request = store.load_request(run)
    goal = next((g for g in request.goals if g.id == call.parameters["goal_id"]), None)
    if goal is None or goal.port != "knowledge_answer":
        raise ValueError("answer must bind the current non-scientific goal")
    sources = []
    identifiers = call.parameters["source_result_ids"]
    if not identifiers and goal.conditions.get("requires_sources"):
        identifiers = [identifier for identifier in run.result_ids if
            store.load_result(run.id, identifier).observations.get("document_excerpt", {}).get("status") == "found"]
    elif not identifiers:
        identifiers = [identifier for identifier in run.result_ids if
            (source := store.load_result(run.id, identifier)).operation_status == "completed"
            and source.source.get("tool", "").startswith("evidence.")]
    for identifier in identifiers:
        if identifier not in run.result_ids:
            raise ValueError("source must be an actually retrieved Result in this Run")
        result = store.load_result(run.id, identifier)
        document = result.observations.get("document_excerpt")
        if not document and result.source.get("tool", "").startswith("evidence."):
            if result.operation_status != "completed":
                raise ValueError("file source is not a completed evidence read")
            for artifact_id in result.artifact_ids:
                store.artifact_path(artifact_id)
            sources.append({"result_id": result.id, "kind": "raw_evidence_observation",
                            "observations": result.observations,
                            "scientific_qualification": False})
            continue
        if not document or document.get("status") != "found" or result.operation_status != "completed":
            raise ValueError("source is not a successful document retrieval")
        sources.append({key: document[key] for key in ("url", "title", "published_at", "accessed_at", "document_sha256")})
    if goal.conditions.get("requires_sources") and not any(source.get("url") for source in sources):
        raise ValueError("this knowledge goal requires actual retrieved sources")
    source_facts = None
    if source_run := request.conditions.get("read_only_source_run"):
        from orca_agent.report import build_report
        source_facts = (question_source_facts(store, source_run)
            if request.conditions.get("source_fact_view") == "query-facts-1"
            else build_report(store, source_run)["goal_facts"])
        if source_facts != request.conditions.get("available_evidence"):
            raise ValueError("source facts changed since question intake; refresh explicitly")
    return {"status": "answered", "goal_id": goal.id, "answer": call.parameters["answer"],
            "sources": sources, "limitations": call.parameters["limitations"],
            "source_run_id": request.conditions.get("read_only_source_run"), "source_facts": source_facts,
            "scientific_qualification": False,
            "classification": "model_knowledge_explanation; delivery checked, scientific truth not certified"}
