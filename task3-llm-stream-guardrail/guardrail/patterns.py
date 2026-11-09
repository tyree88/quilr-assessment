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


REDACTED = "[REDACTED]"
MAX_MATCH = max(EMAIL_MAX, SSN_MAX, CARD_MAX)  # 128: the stream hold length K comes from this

# One pass, one alternation. Leftmost match wins; at the same start, earlier names win.
PII_RE = re.compile("|".join(
    f"(?P<{name}>{p.pattern})" for name, p in (("email", EMAIL), ("ssn", SSN), ("card", CARD))
))

# Invisible format chars a model (or a prompt injection) can wedge inside PII to split it.
# Not all of Unicode category Cf: U+200D (zero-width joiner) and the tag chars build emoji.
_INVISIBLE = dict.fromkeys(
    [0x00AD, 0x180E, 0x200B, 0x200C, 0x200E, 0x200F, 0xFEFF,
     *range(0x202A, 0x202F), *range(0x2060, 0x2065), *range(0x2066, 0x206A)]
)


def strip_invisible(text: str) -> str:
    """Per-character, so it gives the same result however the text is chunked."""
    return text.translate(_INVISIBLE)


def _redact_card_windows(span: str) -> str:
    """A card-shaped span that fails Luhn as a whole: CARD is greedy, so a valid card can sit
    inside it next to another number ("4111 1111 1111 1111 12/27", "qty 2 4111 ...").
    Redact every 13-19 digit window that starts and ends on a digit-group edge and passes
    Luhn (longest first, left to right). Windows only on group edges keep the Luhn control:
    an unseparated 16-digit number has one window, itself. SSNs in what is left still go.
    """
    starts = [i for i, c in enumerate(span) if c.isdecimal() and (i == 0 or not span[i - 1].isdecimal())]
    ends = [i + 1 for i, c in enumerate(span)
            if c.isdecimal() and (i + 1 == len(span) or not span[i + 1].isdecimal())]
    out, last = "", 0
    for a in starts:
        if a < last:
            continue
        for b in reversed(ends):
            if b <= a:
                break
            if 13 <= sum(c.isdecimal() for c in span[a:b]) <= 19 and luhn_ok(span[a:b]):
                out += span[last:a] + REDACTED  # <= 6 digits before a window: no SSN fits
                last = b
                break
    return out + SSN.sub(REDACTED, span[last:])


def replacement(m: re.Match[str]) -> str:
    """What a committed PII_RE match becomes. Shared by redact() and StreamRedactor."""
    if m.lastgroup == "card" and not luhn_ok(m.group()):
        return _redact_card_windows(m.group())
    return REDACTED


def redact(text: str) -> str:
    """Whole-string reference: the answer the streaming redactor must reproduce."""
    return PII_RE.sub(replacement, strip_invisible(text))
