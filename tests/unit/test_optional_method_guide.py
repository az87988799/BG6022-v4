"""The packaged selection guide matches the delegated simplified workflow."""

from types import SimpleNamespace

import pytest

from orca_agent.tools import knowledge


def test_method_guide_preserves_optional_retrieval_and_honest_citations(monkeypatch):
    monkeypatch.setattr(knowledge.httpx, "Client", lambda **kwargs: pytest.fail("guide used HTTP"))
    call = SimpleNamespace(parameters={"document": "guide_method", "query": "method"})
    result, raw, _ = knowledge._retrieve(None, None, call)
    text = result["excerpt"]
    assert text == raw.decode()  # No omitted tail can hide the limits on this short guide.
    assert result["title"] == "Method selection guide 2"
    assert result["url"].endswith("#guide_method-2")
    assert "Under simplified-1" in text and "model reasoning" in text
    assert "cost/accuracy tradeoffs" in text and "documentation retrieval is optional" in text
    assert "model_reason, not literature support" in text
    assert "Honor any user-required documentation and minimum evidence" in text
    assert "completed current-Run knowledge.search Result" in text
    assert "Any supplied hash or quote must match that Result" in text
    assert "Older frozen workflows retain their source requirements" in text
    assert "A cited choice is not scientific validation" in text
    assert "Read versioned official method and basis documentation" not in text
    assert "retrieved Result and exact supporting quote with select_conditions" not in text
    assert result["scientific_qualification"] is False
