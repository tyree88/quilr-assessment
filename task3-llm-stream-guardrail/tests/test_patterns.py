import pytest

from guardrail.patterns import EMAIL, EMAIL_MAX


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
