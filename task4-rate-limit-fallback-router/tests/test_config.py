import pytest
from pydantic import ValidationError

from llm_router.config import Settings


def test_defaults_match_brief():
    s = Settings()
    assert (s.limit_tokens, s.window_ms, s.upstream_timeout_s) == (50_000, 60_000, 3.0)


@pytest.mark.parametrize("field", ["limit_tokens", "window_ms", "busy_timeout_ms"])
def test_rejects_non_positive(field):
    with pytest.raises(ValidationError):
        Settings(**{field: 0})


def test_rejects_unknown_knob():
    with pytest.raises(ValidationError):
        Settings(limt_tokens=1)  # typo must fail, not silently keep 50k
