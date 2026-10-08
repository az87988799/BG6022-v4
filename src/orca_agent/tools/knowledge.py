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

    def handle_starttag(self, tag, attrs):
        if tag == "article":
            self.in_article = True
        if tag in {"script", "style", "nav"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag == "article":
            self.in_article = False
        if tag in {"script", "style", "nav"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self.in_article and not self.skip and data.strip():
            self.parts.append(data.strip())


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
    terms = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", call.parameters["query"].lower())
    scores = [(sum((5 if len(term) >= 6 else 1) for term in terms if term in line.lower()), i)
              for i, line in enumerate(parser.parts)]
    matches = [i for score, i in sorted(scores, key=lambda v: (-v[0], v[1])) if score][:4]
    indices = sorted({j for i in matches for j in range(max(0, i - 2), min(len(parser.parts), i + 24))})
    excerpt = "\n".join(parser.parts[i] for i in indices)[:6000]
    return {"status": "found" if excerpt else "missing", "title": title, "url": url,
            "published_at": None, "accessed_at": utc_now().isoformat(), "query": call.parameters["query"],
            "document_sha256": hashlib.sha256(raw).hexdigest(), "excerpt": excerpt,
            "coverage": {"complete": False, "scope": "bounded matched document excerpts"},
            "scientific_qualification": False, "retrieval": "versioned official-document index; one HTTPS GET"}


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
    for identifier in identifiers:
        if identifier not in run.result_ids:
            raise ValueError("source must be an actually retrieved Result in this Run")
        result = store.load_result(run.id, identifier)
        document = result.observations.get("document_excerpt")
        if not document or document.get("status") != "found" or result.operation_status != "completed":
            raise ValueError("source is not a successful document retrieval")
        sources.append({key: document[key] for key in ("url", "title", "published_at", "accessed_at", "document_sha256")})
    if goal.conditions.get("requires_sources") and not sources:
        raise ValueError("this knowledge goal requires actual retrieved sources")
    source_facts = None
    if source_run := request.conditions.get("read_only_source_run"):
        from orca_agent.report import build_report
        source_facts = build_report(store, source_run)["goal_facts"]
        if source_facts != request.conditions.get("available_evidence"):
            raise ValueError("source facts changed since question intake; refresh explicitly")
    return {"status": "answered", "goal_id": goal.id, "answer": call.parameters["answer"],
            "sources": sources, "limitations": call.parameters["limitations"],
            "source_run_id": request.conditions.get("read_only_source_run"), "source_facts": source_facts,
            "scientific_qualification": False,
            "classification": "model_knowledge_explanation; delivery checked, scientific truth not certified"}
