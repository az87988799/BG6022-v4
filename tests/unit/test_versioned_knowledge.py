"""Versioned retrieval and local guides; every HTTP request is synthetic."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from orca_agent.tools import knowledge


def call(document, query):
    return SimpleNamespace(parameters=knowledge.SearchParameters(document=document, query=query).model_dump())


@pytest.mark.parametrize("document,version", [
    ("guide_input", "guide-1"), ("guide_method", "guide-2"), ("guide_diagnostics", "guide-1"),
])
def test_guides_are_versioned_bounded_sources_without_network(monkeypatch, document, version):
    monkeypatch.setattr(knowledge.httpx, "Client", lambda **kwargs: pytest.fail("packaged guide opened network"))
    original = dict(knowledge.GUIDES)
    result, raw, _ = knowledge._retrieve(None, None, call(document, "input" if document == "guide_input" else "method" if document == "guide_method" else "output"))
    assert raw == knowledge.GUIDES[document].encode()
    assert result["status"] == "found" and result["kind"] == "project_guide"
    assert result["document_id"] == document and result["document_version"] == version
    assert result["rendering_version"] == "text-1"
    assert result["index_version"] == knowledge.INDEX_VERSION and result["index_sha256"] == knowledge.INDEX_SHA256
    assert result["document_sha256"] == hashlib.sha256(knowledge.GUIDES[document].encode()).hexdigest()
    assert result["excerpt_sha256"] == hashlib.sha256(result["excerpt"].encode()).hexdigest()
    assert len(result["excerpt"]) <= 5400 and result["scientific_qualification"] is False
    assert result["url"].startswith("package://") and knowledge.GUIDES == original
    assert not any(name in result["excerpt"].casefold() for name in ("ethanol", "cco", "乙醇"))


def mock_server(monkeypatch, response):
    requests = []
    client = httpx.Client
    def handler(request):
        requests.append(request)
        return response(request)
    def create(**kwargs):
        assert kwargs == {"timeout": 12, "follow_redirects": False, "trust_env": False}
        return client(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(knowledge.httpx, "Client", create)
    return requests


@pytest.mark.parametrize("document", ["orca_input", "orca_dft", "orca_basis", "orca_frequencies", "rdkit"])
def test_official_index_records_actual_bytes_excerpt_and_version(monkeypatch, document):
    paragraph = "A versioned synthetic frequency and basis explanation."
    raw = paragraph.encode() if document == "rdkit" else f"<article><p>{paragraph}</p></article>".encode()
    requests = mock_server(monkeypatch, lambda request: httpx.Response(200, content=raw))
    result, retrieved, _ = knowledge._retrieve(None, None, call(document, "frequency basis"))
    assert retrieved == raw
    assert len(requests) == 1 and str(requests[0].url) == knowledge.DOCUMENTS[document][1]
    assert result["status"] == "found" and result["kind"] == "official_document"
    assert result["document_version"] == ("2025.09.6" if document == "rdkit" else "6.1")
    assert result["rendering_version"] == ("text-1" if document == "rdkit" else "html-readable-2")
    assert result["document_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["excerpt"] == paragraph and result["excerpt_sha256"] == hashlib.sha256(paragraph.encode()).hexdigest()
    assert result["scientific_qualification"] is False and result["coverage"]["complete"] is False


@pytest.mark.parametrize("status,body", [(302, b""), (200, b"x" * (1024 * 1024 + 1))], ids=["redirect", "oversized"])
def test_retrieval_does_not_follow_redirects_retry_or_accept_oversized_documents(monkeypatch, status, body):
    requests = mock_server(monkeypatch, lambda request: httpx.Response(status, content=body,
        headers={"Location": "https://unregistered.invalid/private"}))
    with pytest.raises((httpx.HTTPStatusError, ValueError)):
        knowledge._retrieve(None, None, call("orca_input", "input"))
    assert len(requests) == 1


def test_search_schema_comes_from_same_fixed_index_and_cannot_select_arbitrary_url():
    assert set(knowledge.SearchParameters.model_json_schema()["properties"]["document"]["anyOf"][0]["enum"]) == set(knowledge.DOCUMENTS)
    with pytest.raises(ValidationError):
        call("https://unregistered.invalid", "input")
    with pytest.raises(ValidationError):
        knowledge.SearchParameters(document="orca_input", query="input", url="https://unregistered.invalid")


def test_html_rendering_normalizes_source_whitespace_without_splitting_inline_words(monkeypatch):
    raw = b'<article><p>A\n  short\t paragraph with <em>inline</em> <b>text</b> and Word<span>Part</span>.\r\nNext&nbsp;sentence.</p></article>'
    requests = mock_server(monkeypatch, lambda request: httpx.Response(200, content=raw))
    result, _, _ = knowledge._retrieve(None, None, call("orca_dft", "short paragraph"))
    assert result["excerpt"] == "A short paragraph with inline text and WordPart. Next sentence."
    assert result["document_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["excerpt_sha256"] == hashlib.sha256(result["excerpt"].encode()).hexdigest()
    assert len(requests) == 1


def test_table_rows_keep_cell_paragraphs_headers_and_labels_together():
    parser = knowledge._Text()
    parser.feed('<nav>irrelevant outside navigation</nav><article><h2>Available choices</h2>'
        '<table><thead><tr><th><p>Keyword</p></th><th><p>Description</p></th></tr></thead>'
        '<tbody><tr><td><p>Sample-A</p></td><td><p>A compact\n choice.</p><p>Second explanation.</p></td></tr>'
        '<tr><td><p>Sample-B</p></td><td><p>A diffuse choice.</p></td></tr></tbody></table>'
        '<p>After table.</p><nav><p>irrelevant inner navigation</p></nav>'
        '<script>hidden()</script><style>hidden{}</style></article>')
    parser.flush()
    assert parser.parts == ["Available choices", "Keyword Description",
        "Sample-A A compact choice. Second explanation.", "Sample-B A diffuse choice.", "After table."]
    excerpt, indices = knowledge.matched_excerpt(parser.parts, "Sample-A")
    assert excerpt == "Sample-A A compact choice. Second explanation." and indices == [2]
    assert parser.row_labels == {2: "Sample-A", 3: "Sample-B"}


def test_actual_official_html_fragments_keep_row_identity_and_normalized_quote(monkeypatch):
    directory = Path(__file__).parents[1] / "fixtures/knowledge"
    raw = (directory / "orca-readable-fragments.html").read_bytes()
    manifest = json.loads((directory / "orca-readable-fragments.json").read_bytes())
    assert hashlib.sha256(raw).hexdigest() == manifest["fixture_sha256"]
    requests = mock_server(monkeypatch, lambda request: httpx.Response(200, content=raw))
    result, _, _ = knowledge._retrieve(None, None, call("orca_basis", "def2-SVP"))
    assert result["document_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["coverage"]["table_row_labels"] == {"2": "def2-SVP", "3": "def2-SVPD"}
    assert "def2-SVP H\u2013Rn def2-ECP (Rb\u2013Rn) Polarized valence double-" in result["excerpt"]
    assert "def2-SVPD H\u2013Rn def2-ECP (Rb\u2013Rn) Diffuse def2-SVP" in result["excerpt"]
    parser = knowledge._Text()
    parser.feed(raw.decode())
    assert parser.parts[1] == "Basis Set Elem. ECP Comment"
    assert "types. A simplified" in parser.parts[0] and "\n" not in parser.parts[0]
    assert result["excerpt_sha256"] == hashlib.sha256(result["excerpt"].encode()).hexdigest()
    assert len(requests) == 1


@pytest.mark.parametrize("label", ["Alpha", "Alpha [17]"])
def test_exact_table_identifier_outranks_description_matches_without_prefix_suffix_or_header_aliases(label):
    parser = knowledge._Text()
    parser.feed('<article><table><thead><tr><th>Alpha</th><th>Meaning</th></tr></thead><tbody>' +
        ''.join(f'<tr><td><p>{name}</p></td><td><p>Alpha geometry frequency performance.</p></td></tr>'
            for name in ("Alpha-plus", "pre-Alpha", "Alpha/G", "Group Alpha")) +
        f'<tr><td><p>{label}</p></td><td><p>Base entry.</p></td></tr></tbody></table></article>')
    excerpt, indices = knowledge.matched_excerpt(parser.parts, "Alpha geometry frequency performance",
        row_labels=parser.row_labels)
    assert indices == [1, 2, 5]  # Exact label wins even against three higher phrase scores.
    assert f"{label} Base entry." in excerpt
    assert 0 not in parser.row_labels and parser.row_labels[3] == "Alpha/G"


def test_short_exact_label_is_searchable_and_unknown_query_cannot_fabricate_a_row():
    parts = ["A A short identifier.", "B A different identifier."]
    labels = {0: "A", 1: "B"}
    assert knowledge.matched_excerpt(parts, "B", row_labels=labels) == (parts[1], [1])
    assert knowledge.matched_excerpt(parts, "C", row_labels=labels) == ("", [])


@pytest.mark.parametrize("reference", ["orca:../private.html", "orca:contents/%2e%2e/private.html",
    "orca://evil.invalid/index.html", "orca:index.html?secret=1", "orca:index.html#anchor",
    "orca:contents/../../6.0/manual/index.html", "orca:C:/secret.html"])
def test_manual_references_cannot_escape_versioned_official_root(reference):
    with pytest.raises(ValidationError):
        call(reference, "input")


def test_general_manual_sections_keep_single_get_hash_and_scoped_discovery(monkeypatch):
    raw = b'<article><p>TDDFT response roots.</p><a href="../essentialelements/solvation.html">solvent</a>' \
          b'<a href="https://evil.invalid/private.html">bad</a><a href="../../../6.0/manual/index.html">old</a></article>'
    requests = mock_server(monkeypatch, lambda request: httpx.Response(200, content=raw))
    policy = SimpleNamespace(profile="managed-native-1")
    store = SimpleNamespace(run_configuration=lambda run: SimpleNamespace(execution=SimpleNamespace(native_input_policy=policy)))
    result, _, _ = knowledge._retrieve(store, None, call("orca:contents/spectroscopyproperties/tddft.html", "response"))
    assert len(requests) == 1
    assert str(requests[0].url) == knowledge.MANUAL_ROOT + "contents/spectroscopyproperties/tddft.html"
    assert result["document_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["manual_references"] == ["orca:contents/essentialelements/solvation.html"]
    assert result["scientific_qualification"] is False
    policy.profile = "bounded-analytic-1"
    with pytest.raises(ValueError, match="explicit managed-native"):
        knowledge._retrieve(store, None, call("orca:index.html", "input"))
    assert len(requests) == 1


def test_discovered_manual_references_share_call_schema_length_bound():
    limit = knowledge.MAX_MANUAL_REFERENCE_LENGTH
    accepted = "orca:" + "a" * (limit - len("orca:.html")) + ".html"
    rejected = accepted.replace(".html", "a.html")
    parser = knowledge._ManualLinks(knowledge.MANUAL_ROOT + "index.html")
    parser.feed(f'<a href="{rejected[5:]}">too long</a><a href="{accepted[5:]}">bounded</a>')
    assert parser.links == [accepted]
    assert knowledge.SearchParameters(document=accepted, query="input").document == accepted
    with pytest.raises(ValidationError):
        knowledge.SearchParameters(document=rejected, query="input")


def archived_run(tmp_path, **budget):
    from orca_agent.models import BudgetLimits, Goal, PermissionSnapshot, Request
    from orca_agent.store import Store
    store = Store(tmp_path, environment_root=tmp_path / "environment")
    request = Request(goals=[Goal(id="question", port="knowledge_answer", minimum_check_version="knowledge-answer-1")])
    # Archiving retrieved evidence is part of this read operation, not permission
    # to generate task files or run arbitrary code.
    run = store.create_run(request, None, PermissionSnapshot(allowed_tools=["knowledge.search"]),
                           BudgetLimits(knowledge_queries=3, **budget))
    return store, run


@pytest.mark.parametrize("query,status", [("frequency", "found"), ("unrelated", "missing")])
def test_real_dispatch_archives_exact_decompressed_body_and_counts_quota(tmp_path, monkeypatch, query, status):
    import gzip
    from orca_agent.tools.dispatch import execute_call
    raw = b"<article><p>Frequency explanation \xff is not transcoded in the archive.</p></article>"
    requests = mock_server(monkeypatch, lambda request: httpx.Response(
        200, content=gzip.compress(raw), headers={"Content-Encoding": "gzip"}))
    store, run = archived_run(tmp_path)
    result = execute_call(store, run, "knowledge.search", {"document": "orca_frequencies", "query": query})
    excerpt = result.observations["document_excerpt"]
    assert result.operation_status == "completed" and excerpt["status"] == status
    assert len(requests) == len(result.artifact_ids) == 1
    artifact = store.load_artifact(excerpt["document_artifact_id"])
    assert store.artifact_path(artifact.id).read_bytes() == raw
    assert artifact.sha256 == excerpt["document_sha256"] == hashlib.sha256(raw).hexdigest()
    assert excerpt["document_archive_complete"] is True and excerpt["document_bytes"] == len(raw)
    assert result.source["files"]["document.html"] == {"artifact_id": artifact.id, "sha256": artifact.sha256}
    assert artifact.source["call_id"] == result.call_id and artifact.role == "knowledge_document"
    assert store.artifact_budget_usage(run)["reserved_bytes"] == len(raw)
    assert not result.qualified_outputs and not run.attempts and not run.permission.artifact_writes
    assert run.usage.knowledge_queries == 1 and run.usage.orca_starts_actual == 0


@pytest.mark.parametrize("budget", [{"artifact_count": 0}, {"artifact_bytes": 1}, {"artifact_total_bytes": 1}])
def test_archive_quota_failure_retains_raw_call_file_but_never_claims_complete_document(tmp_path, monkeypatch, budget):
    from orca_agent.tools.dispatch import execute_call
    raw = b"<article><p>Input explanation.</p></article>"
    requests = mock_server(monkeypatch, lambda request: httpx.Response(200, content=raw))
    store, run = archived_run(tmp_path, **budget)
    result = execute_call(store, run, "knowledge.search", {"document": "orca_input", "query": "input"})
    excerpt = result.observations["document_excerpt"]
    assert result.operation_status == "failed" and not result.artifact_ids and not result.qualified_outputs
    assert excerpt["status"] == "archive_failed" and excerpt["document_archive_complete"] is False
    assert all(check.status == "unverified" for checks in result.checks.values() for check in checks)
    assert result.diagnostics[0]["reason"] == "artifact_budget_exhausted"
    assert store.path(f"runs/{run.id}/calls/{result.call_id}/document.html").read_bytes() == raw
    assert len(requests) == 1 and run.usage.knowledge_queries == 1
    with pytest.raises(ValueError, match="successful document retrieval"):
        knowledge.answer(store, run, SimpleNamespace(parameters={"goal_id": "question",
            "answer": "Cannot cite an incompletely archived retrieval.", "source_result_ids": [result.id],
            "limitations": []}))
    from orca_agent.condition_selection import _source
    with pytest.raises(ValueError, match="completed knowledge.search"):
        _source(store, run, SimpleNamespace(result_id=result.id, document_sha256=excerpt["document_sha256"],
                quote=excerpt["excerpt"]), path=["choices", "method", "sources", 0])


def test_packaged_guide_dispatch_archives_frozen_bytes_without_network(tmp_path, monkeypatch):
    from orca_agent.tools.dispatch import execute_call
    monkeypatch.setattr(knowledge.httpx, "Client", lambda **kwargs: pytest.fail("guide opened network"))
    store, run = archived_run(tmp_path)
    result = execute_call(store, run, "knowledge.search", {"document": "guide_input", "query": "input"})
    excerpt = result.observations["document_excerpt"]
    assert store.artifact_path(excerpt["document_artifact_id"]).read_bytes() == knowledge.GUIDES["guide_input"].encode()
    assert excerpt["document_archive_complete"] is True and run.usage.knowledge_queries == 1
