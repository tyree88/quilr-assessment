import pytest

from guardrail.patterns import EMAIL, EMAIL_MAX, SSN, SSN_MAX


@pytest.mark.parametrize("text, found", [
    ("mail john@example.com now", "john@example.com"),
    ("a.b+tag@sub.example.co.uk", "a.b+tag@sub.example.co.uk"),
    ("end john@example.com.", "john@example.com"),  # trailing dot is not part of it
])
def test_email_matches(text, found):
    assert EMAIL.search(text).group() == found


@pytest.mark.parametrize("text", ["a@b", "john@", "@example.com", "john@example.c"])
def test_email_rejects(text):
    assert EMAIL.search(text) is None


def test_email_length_caps():
    longest = "a" * 64 + "@" + "b" * 48 + "." + "c" * 14
    assert len(longest) == EMAIL_MAX and EMAIL.fullmatch(longest)
    assert not EMAIL.fullmatch("a" * 65 + "@example.com")
    assert not EMAIL.fullmatch("a" * 64 + "@" + "b" * 49 + ".com")


@pytest.mark.parametrize("text, found", [
    ("ssn 123-45-6789.", "123-45-6789"),
    ("000-12-3456", "000-12-3456"),  # Q4b: no area-number rules, over-redact
    (
        "\uff11\uff12\uff13-\uff14\uff15-\uff16\uff17\uff18\uff19",
        "\uff11\uff12\uff13-\uff14\uff15-\uff16\uff17\uff18\uff19",
    ),
])
def test_ssn_matches(text, found):
    assert EMAIL.search(text) is None and SSN.search(text).group() == found
    assert len(found) == SSN_MAX


@pytest.mark.parametrize("text", [
    "call 123-456-7890", "512-555-0199", "1123-45-6789", "123-45-67890", "abc123-45-6789",
])
def test_ssn_rejects(text):
    assert SSN.search(text) is None
