"""Configurable names keep one bounded transport and historical serialization."""

import json

import pytest

from orca_agent.llm import prepare_request
from orca_agent.model_usage import _cost
from orca_agent.models import ModelLimits


def test_legacy_model_limits_serialization_is_unchanged():
    assert "model_name" not in ModelLimits().model_dump()
    assert "model_name" not in json.loads(ModelLimits().model_dump_json())


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_selected_model_is_frozen_in_body_and_record_metadata(model):
    request = prepare_request([{"role": "user", "content": "Return JSON."}],
                              limits=ModelLimits(model_name=model, output_tokens=8192))
    body = json.loads(request.canonical_body)
    assert request.model == body["model"] == model and body["max_tokens"] == 8192
    assert request.limits.model_name == model


def test_unpriced_model_does_not_use_flash_price():
    assert _cost(100, 100, "deepseek-v4-pro") is None
    assert _cost(100, 100) == "0.00015"
