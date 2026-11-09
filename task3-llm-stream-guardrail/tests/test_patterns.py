import pytest

from guardrail.patterns import (
    CARD,
    CARD_MAX,
    EMAIL,
    EMAIL_MAX,
    MAX_MATCH,
    SSN,
    SSN_MAX,
    luhn_ok,
    redact,
)


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
    ("id123-45-6789", "123-45-6789"),  # letters before digits no longer block it
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
    "call 123-456-7890", "512-555-0199", "1123-45-6789", "123-45-67890",
])
def test_ssn_rejects(text):
    assert SSN.search(text) is None


@pytest.mark.parametrize("text, found", [
    ("card 4111 1111 1111 1111 ok", "4111 1111 1111 1111"),
    ("4111-1111-1111-1111", "4111-1111-1111-1111"),
    ("amex 3782 822463 10005", "3782 822463 10005"),  # 15 digits, 4-6-5 grouping
    ("4111111111111111", "4111111111111111"),
    ("Visa4111111111111111", "4111111111111111"),  # glued to a word
])
def test_card_shape_matches(text, found):
    assert CARD.search(text).group() == found


@pytest.mark.parametrize("text", [
    "411111111111",  # 12 digits: too short
    "41111111111111111111",  # 20 digits: too long, and no shorter run has a \b
    "4111  1111 1111 1111",  # double space: no 13-digit run left
    "123-45-6789", "512-555-0199",
])
def test_card_shape_rejects(text):
    assert CARD.search(text) is None


def test_card_length_cap():
    longest = " ".join(["1"] * 19)
    assert len(longest) == CARD_MAX and CARD.fullmatch(longest)


@pytest.mark.parametrize("number, ok", [
    ("4111 1111 1111 1111", True),  # Visa test number
    ("4111-1111-1111-1111", True),  # separators ignored
    ("3782 822463 10005", True),  # Amex test number, 15 digits
    ("5555555555554444", True),  # Mastercard test number
    ("4111 1111 1111 1112", False),  # last digit changed
    ("4111111111111121", False),  # sums to 0 mod 10 only if you skip doubling
    ("2024 2025 2026 2027", False),  # a list of years: shape matches, checksum does not
])
def test_luhn(number, ok):
    assert luhn_ok(number) is ok


@pytest.mark.parametrize("text, out", [
    (
        "mail a.b@example.com, ssn 123-45-6789, card 4111 1111 1111 1111, bad 4111 1111 1111 1112.",
        "mail [REDACTED], ssn [REDACTED], card [REDACTED], bad 4111 1111 1111 1112.",
    ),
    ("id 123-45-6789-0123 end", "id [REDACTED]-0123 end"),  # SSN alternative wins at its start
    ("ref 4111 123-45-6789 end", "ref 4111 [REDACTED] end"),  # failed card holds an SSN
    ("4111111111111111@example.com", "[REDACTED]"),  # email wins at the same start
    ("call 512-555-0199", "call 512-555-0199"),  # phone untouched
    ("john\u200b@exa\u00admple.com", "[REDACTED]"),  # invisible chars stripped first
    ("123\u2060-45-6789", "[REDACTED]"),
    ("family \U0001f468\u200d\U0001f469", "family \U0001f468\u200d\U0001f469"),  # ZWJ kept
])
def test_redact(text, out):
    assert redact(text) == out


def test_max_match_is_email():
    assert MAX_MATCH == EMAIL_MAX == 128
