import pytest
from pydantic import ValidationError

from llm_router.models import CompletionRequest


def test_estimate_rounds_prompt_up_and_reserves_max_tokens():
    assert CompletionRequest(prompt="abcde", max_tokens=10).estimated_tokens() == 2 + 10
    assert CompletionRequest(prompt="abcd", max_tokens=10).estimated_tokens() == 1 + 10


@pytest.mark.parametrize(
    "body",
    [
        '{"prompt": ""}',                          # empty prompt
        '{"prompt": "hi", "max_tokens": "100"}',   # string for int (strict)
        '{"prompt": "hi", "max_tokens": 0}',
        '{"prompt": "hi", "max_tokens": 32001}',
        '{"prompt": "hi", "model": "x"}',          # unknown field
        '{"max_tokens": 5}',                       # missing prompt
        '["not", "an", "object"]',
    ],
)
def test_rejects_bad_bodies(body):
    with pytest.raises(ValidationError):
        CompletionRequest.model_validate_json(body)


def test_accepts_minimal_body():
    assert CompletionRequest.model_validate_json('{"prompt": "hi"}').max_tokens == 256
