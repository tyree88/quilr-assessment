import re

# local part ≤ 64 (RFC 5321 §4.5.3.1.1) + "@" + domain ≤ 48 + "." + TLD ≤ 14 = 128
EMAIL_MAX = 128
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,48}\.[A-Za-z]{2,14}")

SSN_MAX = 11
# \d, not [0-9]: fullwidth digits match too; over-matching is the safe direction.
# Digit lookarounds, not \b: "id123-45-6789" is caught, a longer digit run is not.
SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")

# 13–19 digits, one optional space or dash between digits: 19 + 18 separators = 37
CARD_MAX = 37
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def luhn_ok(number: str) -> bool:
    """Luhn checksum over the decimal digits of a CARD match; separators are skipped.

    Only CARD matches (13+ digits) reach this, so the empty-input case never occurs.
    """
    total = 0
    for i, ch in enumerate(reversed([c for c in number if c.isdecimal()])):
        d = int(ch) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0
