import random

import pytest

from guardrail.patterns import MAX_MATCH, redact
from guardrail.redactor import StreamRedactor

SAMPLES = [
    "contact john@example.com now",
    "mail a.b+tag@sub.example.co.uk, then id123-45-6789 and 4111 1111 1111 1111.",
    "bad card 4111 1111 1111 1112 stays, phone 512-555-0199 stays",
    "ref 4111 123-45-6789 end",
    "Visa4111111111111111 and 3782 822463 10005 and 5555555555554444",
    "split john\u200b@exa\u00admple.com with invisible chars",
    "no pii here at all, just a long sentence that goes on " * 6,
    "x" * 300 + "joe@example.org" + "y" * 300,
    "4111111111111111@example.com",
    "\uff11\uff12\uff13-\uff14\uff15-\uff16\uff17\uff18\uff19 fullwidth",
]


def run(chunks: list[str]) -> str:
    r = StreamRedactor()
    return "".join(r.feed(c) for c in chunks) + r.flush()


def splits(s: str, rng: random.Random) -> list[list[str]]:
    words = s.split(" ")
    rand, i = [], 0
    while i < len(s):
        j = i + rng.randint(1, 12)
        rand.append(s[i:j])
        i = j
    return [[s], list(s), [w + " " for w in words[:-1]] + [words[-1]], rand]


@pytest.mark.parametrize("s", SAMPLES)
def test_chunking_invariance(s):
    rng = random.Random(s)
    for chunks in splits(s, rng):
        assert "".join(chunks) == s
        assert run(chunks) == redact(s)


@pytest.mark.parametrize("pii", [
    "john@example.com", "a.b+tag@sub.example.co.uk", "123-45-6789",
    "4111 1111 1111 1111", "4111-1111-1111-1111", "3782 822463 10005",
])
def test_pii_split_at_every_index_is_redacted(pii):
    s = f"before {pii} after"
    for i in range(len(s) + 1):
        for j in range(i, len(s) + 1, 3):
            out = run([s[:i], s[i:j], s[j:]])
            assert out == "before [REDACTED] after", (i, j)


def test_buffer_stays_bounded():
    r = StreamRedactor()
    rng = random.Random(1)
    for _ in range(20_000):
        r.feed(rng.choice(["word ", "joe@example.com ", "4111 1111 ", "1111 1111 ", "x"]))
        assert r.held <= MAX_MATCH + MAX_MATCH


def test_emits_before_end_of_stream():
    r = StreamRedactor()
    assert r.feed("a" * (MAX_MATCH + 10)) == "a" * 10


def test_pii_longer_than_k_leaks_its_head_as_documented():
    local = "a" * 70  # local part over the 64-char cap
    assert redact(f"{local}@example.com") == "a" * 6 + "[REDACTED]"
    assert run(list(f"{local}@example.com")) == "a" * 6 + "[REDACTED]"


def test_k_below_max_match_is_refused():
    with pytest.raises(ValueError):
        StreamRedactor(k=MAX_MATCH - 1)


def test_context_char_keeps_digit_lookbehind_across_the_hold_point():
    # A long digit run: no SSN or card inside it may match, wherever the hold point lands.
    # No match covers it, so the hold point can land inside the run; the char before it
    # must stay visible or "123-45-6789" looks like a fresh SSN.
    s = "n" * 200 + " " + "0" * 40 + "123-45-6789 " + "m" * 200
    assert redact(s) == s
    for i in range(len(s) + 1):
        assert run([s[:i], s[i:]]) == redact(s), i
