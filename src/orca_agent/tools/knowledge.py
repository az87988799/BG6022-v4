"""Bounded public-document retrieval and non-scientific answer receipts."""

import hashlib
import json
import re
from bisect import bisect_right
from collections import Counter
from html.parser import HTMLParser
from typing import Annotated, Literal
from urllib.parse import urldefrag, urljoin

import httpx
from pydantic import Field, model_validator

from orca_agent.models import Identifier, Record, utc_now

RULE = "knowledge-answer-1"
INDEX_VERSION = "knowledge-index-3"
MANUAL_ROOT = "https://www.faccts.de/docs/orca/6.1/manual/"
MANUAL_REFERENCE = r"^orca:(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.html$"
MAX_MANUAL_REFERENCE_LENGTH = 240
DOCUMENTS = {
    "orca_electric": ("ORCA 6.1 Electrical Properties", "https://www.faccts.de/docs/orca/6.1/manual/contents/spectroscopyproperties/electric.html"),
    "orca_optimization": ("ORCA 6.1 Geometry Optimization", "https://www.faccts.de/docs/orca/6.1/manual/contents/structurereactivity/optimizations.html"),
    "orca_scf": ("ORCA 6.1 SCF", "https://www.faccts.de/docs/orca/6.1/manual/contents/essentialelements/scf.html"),
    "orca_properties": ("ORCA 6.1 Property File", "https://www.faccts.de/docs/orca/6.1/manual/contents/utilitiesvisualization/property_file_list.html"),
    "opi": ("OPI 2.0 Introduction", "https://www.faccts.de/docs/opi/2.0/docs/contents/notebooks/how_to_opi.html"),
    "orca_input": ("ORCA 6.1 Input Structure", "https://www.faccts.de/docs/orca/6.1/manual/contents/essentialelements/input.html"),
    "orca_dft": ("ORCA 6.1 Density Functional Theory", "https://www.faccts.de/docs/orca/6.1/manual/contents/modelchemistries/DensityFunctionalTheory.html"),
    "orca_basis": ("ORCA 6.1 Basis Sets", "https://www.faccts.de/docs/orca/6.1/manual/contents/essentialelements/basisset.html"),
    "orca_frequencies": ("ORCA 6.1 Vibrational Frequencies", "https://www.faccts.de/docs/orca/6.1/manual/contents/structurereactivity/frequencies.html"),
    "rdkit": ("RDKit 2025.09.6 Getting Started", "https://raw.githubusercontent.com/rdkit/rdkit/Release_2025_09_6/Docs/Book/GettingStartedInPython.rst"),
    "guide_input": ("Task input guide 1", "package://orca_agent/tools/knowledge.py#guide_input-1"),
    "guide_method": ("Method selection guide 2", "package://orca_agent/tools/knowledge.py#guide_method-2"),
    "guide_diagnostics": ("Output diagnosis guide 1", "package://orca_agent/tools/knowledge.py#guide_diagnostics-1"),
}
GUIDES = {
    "guide_input": (
        "Input preparation: preserve molecular identity, stereochemistry, charge, multiplicity and coordinate units. "
        "Consult orca_input and the frozen OPI/RDKit versions for syntax and supported APIs. Write task files using "
        "workspace.write; use its returned Artifact ID/hash, never invent a host path. Declare every dependent "
        "file in the input manifest. Python may use the managed SDK within its declared budgets; importing a "
        "library does not authorize subprocesses, networking or arbitrary filesystem access. Preserve original "
        "geometry. A generated structure is an initial geometry, not evidence of optimization convergence."),
    "guide_method": (
        "Method selection: preserve all explicit user conditions. Select only fields the user authorized for "
        "automatic selection. Under simplified-1, delegated method/basis choices may use model reasoning about "
        "the requested quantity, element coverage and cost/accuracy tradeoffs. Record each value and reason with "
        "select_conditions before scientific execution; documentation retrieval is optional, and geometry preparation "
        "may precede selection. An uncited choice records model_reason, not literature support. Honor any user-required "
        "documentation and minimum evidence. When citing literature, use a completed current-Run knowledge.search "
        "Result; the program binds its document hash and returned excerpt when omitted. Any supplied hash or quote "
        "must match that Result; never invent a citation. Older frozen workflows retain their source requirements. "
        "Electron-count parity alone does not determine an electronic state. An unresolved charge, spin or "
        "identity that changes the scientific target requires clarification. A cited choice is not scientific validation."),
    "guide_diagnostics": (
        "Output diagnosis: inspect the original stdout/stderr and declared artifacts. Process exit, SCF convergence, "
        "geometry convergence and scientific qualification are separate facts. Bind observed properties to the "
        "actual geometry, conditions, unit and source file. For vibrational frequencies consult orca_frequencies; "
        "report imaginary modes and numerical limitations explicitly. Without a suitable checker, deliver observations "
        "with validation gaps and do not declare a minimum merely because execution ended. Preserve failure evidence; "
        "unknown execution requires reconciliation, and a repair cannot reset budgets or alter frozen inputs."),
}
DOCUMENT_METADATA = {name: {
    "document_version": "guide-2" if name == "guide_method" else "guide-1" if name in GUIDES else "2025.09.6" if name == "rdkit" else "2.0" if name == "opi" else "6.1",
    "kind": "project_guide" if name in GUIDES else "official_document",
    "format": "text" if name in GUIDES or name == "rdkit" else "html",
    **({"content_sha256": hashlib.sha256(GUIDES[name].encode()).hexdigest()} if name in GUIDES else {}),
} for name in DOCUMENTS}
INDEX_SHA256 = hashlib.sha256(json.dumps({
    "documents": {name: {"title": title, "url": url, **DOCUMENT_METADATA[name]}
        for name, (title, url) in DOCUMENTS.items()},
    "manual_root": MANUAL_ROOT, "manual_reference": MANUAL_REFERENCE,
    "max_manual_reference_length": MAX_MANUAL_REFERENCE_LENGTH,
}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class SearchParameters(Record):
    document: Literal[*DOCUMENTS] | Annotated[str, Field(
        pattern=MANUAL_REFERENCE, max_length=MAX_MANUAL_REFERENCE_LENGTH,
        description="Under managed-native-1, orca:index.html or orca:contents/...html reads the frozen official ORCA 6.1 manual.")]
    query: str = Field(min_length=1, max_length=120)
    paragraph: int | None = Field(default=None, ge=0, le=1048576,
        description="Optional zero-based paragraph from prior coverage; reads a continuous bounded window.")
    start_character: int | None = Field(default=None, ge=0, le=1048576,
        description="Optional zero-based character offset within paragraph; requires paragraph. Omit for query-centered selection.")

    @model_validator(mode="after")
    def window_scope(self):
        if self.start_character is not None and self.paragraph is None:
            raise ValueError("start_character requires a paragraph from document coverage")
        return self


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
        self.row_depth = 0
        self.row_labels = {}
        self.row_label = None
        self.row_cell_count = 0
        self.row_label_start = None
        self.literal_depth = 0
        self.literal_indices = set()
        self.pre_buffer = False
        self.code_paragraphs = set()

    def flush(self):
        pieces, plain = [], []
        for index, value in enumerate(self.buffer):
            if index in self.literal_indices:
                pieces.extend((re.sub(r"\s+", " ", "".join(plain)), value))
                plain = []
            else:
                plain.append(value)
        pieces.append(re.sub(r"\s+", " ", "".join(plain)))
        value = "".join(pieces)
        value = value.strip("\r\n") if self.pre_buffer else value.strip()
        if value:
            if self.literal_indices:
                self.code_paragraphs.add(len(self.parts))
            if self.row_label:
                self.row_labels[len(self.parts)] = self.row_label
            self.parts.append(value)
        self.buffer = []
        self.row_label = None
        self.literal_indices = set()
        self.pre_buffer = False

    def handle_starttag(self, tag, attrs):
        if tag == "article":
            self.in_article = True
        if tag in {"script", "style", "nav"}:
            self.skip += 1
        if self.in_article and not self.skip:
            if tag == "tr":
                if not self.row_depth:
                    self.flush()
                    self.row_cell_count = 0
                    self.row_label_start = None
                self.row_depth += 1
            if tag in {"p", "pre", "li", "h1", "h2", "h3", "h4", "table", "caption"}:
                if self.row_depth:
                    self.buffer.append(" ")
                else:
                    self.flush()
            if tag in {"td", "th", "br"}:
                if tag in {"td", "th"} and self.row_depth == 1:
                    if not self.row_cell_count and tag == "td":
                        self.row_label_start = len(self.buffer)
                    self.row_cell_count += 1
                if tag == "br" and self.literal_depth:
                    self.literal_indices.add(len(self.buffer))
                self.buffer.append("\n" if tag == "br" and self.literal_depth else " ")
            if tag in {"pre", "code"}:
                self.literal_depth += 1
                self.pre_buffer |= tag == "pre"

    def handle_endtag(self, tag):
        if self.in_article and not self.skip:
            if tag == "tr":
                self.row_depth = max(0, self.row_depth - 1)
                if not self.row_depth:
                    self.flush()
            if tag in {"p", "pre", "li", "h1", "h2", "h3", "h4", "table", "caption", "article"}:
                if self.row_depth and tag != "article":
                    self.buffer.append(" ")
                else:
                    self.flush()
            if tag in {"td", "th"}:
                if self.row_depth == 1 and self.row_label_start is not None:
                    self.row_label = re.sub(r"\s+", " ", "".join(self.buffer[self.row_label_start:])).strip()
                    self.row_label_start = None
                self.buffer.append(" ")
            if tag in {"pre", "code"}:
                self.literal_depth = max(0, self.literal_depth - 1)
        if tag == "article":
            self.in_article = False
        if tag in {"script", "style", "nav"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self.in_article and not self.skip:
            if self.literal_depth:
                self.literal_indices.add(len(self.buffer))
            self.buffer.append(data)


def _document_window(text, terms, start_character=None):
    """A continuous source window; no joined fragments or reconstructed code."""
    lines = text.splitlines(keepends=True) or [text]
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    def end_at(begin):
        limit = min(len(text), begin + 1800)
        boundary = offsets[bisect_right(offsets, limit) - 1]
        return boundary if boundary > begin else limit

    if start_character is not None:
        if not 0 <= start_character < len(text):
            raise ValueError("document start_character is outside the selected paragraph")
        begin = start_character
    elif len(text) <= 1800:
        begin = 0
    else:
        hits = [(match.start(), match.end(), match.group().lower())
                for match in re.finditer(r"[a-z0-9_-]+", text, re.I) if match.group().lower() in terms]
        candidates = set(offsets[:-1])
        for start, stop, _ in hits:
            line = bisect_right(offsets, start) - 1
            if offsets[line + 1] - offsets[line] > 1800:
                candidates.update((max(offsets[line], start - 200), max(offsets[line], stop - 1800)))
        best, begin, left, right, window = (-1, -1), 0, 0, 0, Counter()
        for candidate in sorted(candidates):
            end = end_at(candidate)
            while right < len(hits) and hits[right][1] <= end:
                window[hits[right][2]] += 1
                right += 1
            while left < right and hits[left][0] < candidate:
                window[hits[left][2]] -= 1
                if not window[hits[left][2]]:
                    del window[hits[left][2]]
                left += 1
            score = (len(window), sum(window.values()))
            if score > best:
                best, begin = score, candidate
    end = end_at(begin)
    excerpt = text[begin:end]
    return excerpt, {"start_character": begin, "end_character": end, "paragraph_characters": len(text),
        "start_line": text.count("\n", 0, begin) + 1,
        "end_line": text.count("\n", 0, max(begin, end - 1)) + 1,
        "truncated_before": begin > 0, "truncated_after": end < len(text),
        "partial_line": begin not in offsets or end not in offsets}


def matched_excerpt(parts, query, *, row_labels=None, paragraph=None, start_character=None, windows=None):
    """Prefer exact table identifiers, then matched phrases; retain source order."""
    terms = list(dict.fromkeys(re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", query.lower())))
    identifiers = set(re.findall(r"[a-z][a-z0-9_/-]*", query.lower()))
    if paragraph is not None and not 0 <= paragraph < len(parts):
        raise ValueError("document paragraph is outside the retrieved document")
    if paragraph is None and not terms and not row_labels:
        return "", []
    phrase = " ".join(terms)
    ranked = []
    for index, part in enumerate(parts):
        normalized = " ".join(re.findall(r"[a-z0-9_-]+", part.lower()))
        hits = sum(bool(re.search(r"\b" + re.escape(term) + r"\b", normalized)) for term in terms)
        label_tokens = re.findall(r"[a-z][a-z0-9_/-]*", (row_labels or {}).get(index, "").lower())
        label_hit = len(label_tokens) == 1 and label_tokens[0] in identifiers
        score = hits + 10 * bool(terms and hits == len(terms)) + 40 * bool(phrase and phrase in normalized)
        if hits or label_hit:
            ranked.append((label_hit, score, index))
    chosen = ([paragraph] if paragraph is not None else
              sorted(index for _, _, index in sorted(ranked, key=lambda item: (-item[0], -item[1], item[2]))[:3]))
    excerpts, remaining = [], 5400
    for index in chosen:
        excerpt, coverage = _document_window(parts[index], set(terms), start_character)
        allowed = remaining - (2 if excerpts else 0)
        if len(excerpt) > allowed:
            excerpt = excerpt[:allowed]
            coverage.update(end_character=coverage["start_character"] + len(excerpt),
                            truncated_after=True, partial_line=True)
            coverage["end_line"] = parts[index].count("\n", 0, max(coverage["start_character"], coverage["end_character"] - 1)) + 1
        excerpts.append(excerpt)
        remaining = allowed - len(excerpt)
        if windows is not None:
            windows.append({"paragraph": index, **coverage})
    return "\n\n".join(excerpts), chosen


class _ManualLinks(HTMLParser):
    """Return only bounded follow-up references inside the frozen manual."""

    def __init__(self, url, query=""):
        super().__init__()
        self.url, self.links = url, []
        self.terms = set(re.findall(r"[a-z][a-z0-9_-]{2,}", query.lower()))

    def score(self, reference):
        return len(self.terms & set(re.findall(r"[a-z][a-z0-9_-]{2,}", reference.lower())))

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href", "")
        target = urldefrag(urljoin(self.url, href))[0]
        if target.startswith(MANUAL_ROOT):
            reference = "orca:" + target[len(MANUAL_ROOT):]
            if (len(reference) <= MAX_MANUAL_REFERENCE_LENGTH
                    and re.fullmatch(MANUAL_REFERENCE, reference) and reference not in self.links):
                if len(self.links) < 128:
                    self.links.append(reference)
                else:
                    lowest = min(range(len(self.links)), key=lambda index: self.score(self.links[index]))
                    if self.score(reference) > self.score(self.links[lowest]):
                        self.links[lowest] = reference


def _retrieve(store, run, call):
    identifier = call.parameters["document"]
    if identifier in DOCUMENTS:
        title, url = DOCUMENTS[identifier]
        metadata = DOCUMENT_METADATA[identifier]
    else:
        SearchParameters.model_validate(call.parameters)
        policy = store.run_configuration(run).execution.native_input_policy
        if policy is None or policy.profile != "managed-native-1":
            raise ValueError("manual section retrieval requires the explicit managed-native-1 policy")
        title, url = "ORCA 6.1 Manual: " + identifier[5:], MANUAL_ROOT + identifier[5:]
        metadata = {"document_version": "6.1", "kind": "official_document", "format": "html"}
    # Exactly one GET; no redirects, cookies, credentials, search engine, or retries.
    raw = bytearray()
    if identifier in GUIDES:
        raw.extend(GUIDES[identifier].encode("utf-8"))
    else:
        with httpx.Client(timeout=12, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", url, headers={"User-Agent": "ORCA-Agent/0.1 documentation reader"}) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > 1024 * 1024:
                        raise ValueError("document exceeds 1 MiB retrieval bound")
                    raw.extend(chunk)
    text = raw.decode("utf-8", errors="replace")
    row_labels = {}
    if metadata["format"] == "html":
        parser = _Text()
        parser.feed(text)
        parser.flush()
        parts = parser.parts
        row_labels = parser.row_labels
    else:
        parts = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    windows = []
    excerpt, indices = matched_excerpt(parts, call.parameters["query"], row_labels=row_labels,
        paragraph=call.parameters.get("paragraph"), start_character=call.parameters.get("start_character"), windows=windows)
    result = {"status": "found" if excerpt else "missing", "title": title, "url": url,
            "document_id": identifier, "index_version": INDEX_VERSION, "index_sha256": INDEX_SHA256,
            "document_version": metadata["document_version"], "kind": metadata["kind"],
            "rendering_version": "html-readable-3" if metadata["format"] == "html" else "text-1",
            "excerpt_version": "query-window-1",
            "published_at": None, "accessed_at": utc_now().isoformat(), "query": call.parameters["query"],
            "document_sha256": hashlib.sha256(raw).hexdigest(), "excerpt": excerpt,
            "excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
            "coverage": {"complete": False, "scope": "bounded matched document paragraphs and table rows",
                         "paragraph_indices": indices, "paragraph_count": len(parts),
                         "view": "HTML text with pre/code line breaks; ordinary prose whitespace normalized"
                            if metadata["format"] == "html" else "decoded text paragraphs",
                         "windows": windows,
                         "continuation": "Use the same document with paragraph and zero-based start_character from windows; "
                            "each query archives its actual document hash. Windows are partial, not complete executable blocks.",
                         "table_row_labels": {str(index): row_labels[index] for index in indices if index in row_labels}},
            "scientific_qualification": False,
            "retrieval": "versioned packaged guide; no network" if identifier in GUIDES else "versioned official-document index; one HTTPS GET"}
    if identifier.startswith("orca:"):
        links = _ManualLinks(url, call.parameters["query"])
        links.feed(text)
        result["manual_references"] = links.links
        if url.rsplit("/", 1)[-1] == "index.html":
            result["coverage"]["document_scope"] = "navigation"
            result["coverage"]["limitation"] = (
                "Index headings and links locate documentation; they do not establish "
                "input syntax or output format. Explicitly retrieve the relevant manual "
                "section before relying on its syntax. Linked pages were not fetched."
            )
    return result, bytes(raw), metadata["format"]


def retrieve(store, run, call):
    """Archive the bytes from this one retrieval, without another request or conversion."""
    from orca_agent.store import BudgetExceeded, atomic_write

    result, raw, format_name = _retrieve(store, run, call)
    filename = "document.html" if format_name == "html" else "document.txt"
    path = store.path(f"runs/{run.id}/calls/{call.id}/{filename}")
    atomic_write(path, raw, immutable=True)
    source = {"run_id": run.id, "call_id": call.id, "tool": call.tool,
              "url": result["url"], "document_id": result["document_id"],
              "document_version": result["document_version"], "accessed_at": result["accessed_at"],
              "byte_semantics": "HTTP content-decoded body before UTF-8 text decoding; packaged guides use UTF-8 bytes",
              "scientific_qualification": False}
    try:
        artifact = store.import_artifact(path, "knowledge_document", run_id=run.id,
                                         source=source, expected_sha256=result["document_sha256"])
    except BudgetExceeded:
        return {**result, "status": "archive_failed", "operation_status": "failed",
                "document_archive_complete": False, "artifact_ids": [],
                "diagnostics": [{"category": "document_archive_failed", "reason": "artifact_budget_exhausted",
                                 "raw_call_file_retained": filename}],
                "source": source}
    return {**result, "artifact_ids": [artifact.id], "document_artifact_id": artifact.id,
            "document_archive_complete": True, "document_bytes": len(raw),
            "source": {**source, "files": {filename: {"artifact_id": artifact.id, "sha256": artifact.sha256}}}}


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
