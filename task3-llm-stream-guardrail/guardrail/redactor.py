import string

from guardrail.patterns import MAX_MATCH, PII_RE, replacement, strip_invisible

# Chars a still-growing match can end with: email/SSN/unspaced card (no space), spaced card.
_NO_SPACE = frozenset(string.ascii_letters + string.digits + "._%+@-")
_CARD_RUN = frozenset(" -")


def _tail_start(text: str, lo: int) -> int:
    """Start of the shortest suffix that could still be part of a growing match.

    A match not yet complete must reach the end of text through chars of one class, so a
    char outside both classes ends any hope of one; text before it is safe to emit.
    """
    a = b = len(text)
    while a > lo and (text[a - 1] in _NO_SPACE or text[a - 1].isdecimal()):
        a -= 1
    while b > lo and (text[b - 1] in _CARD_RUN or text[b - 1].isdecimal()):
        b -= 1
    return min(a, b)


class StreamRedactor:
    """Redacts PII in a text stream, holding back at most K + MAX_MATCH chars (2 x 128).

    The hold limit is at most K back from the end, and a match that ends past the limit is
    held from its start, which can be up to MAX_MATCH further back.

    Invariant: feed() over any chunking of s, then flush(), returns redact(s).

    Why it is safe: every pattern is at most MAX_MATCH chars and looks at most one char
    past its end. With K >= MAX_MATCH, a match ending at or before len(buf) - K has all the
    text it could ever need, so it is final. A match ending later may still grow: hold it.
    """

    def __init__(self, k: int = MAX_MATCH) -> None:
        if k < MAX_MATCH:
            raise ValueError("k must be >= MAX_MATCH or PII longer than k leaks")
        self._k = k
        self._ctx = ""  # last emitted char: left context for the (?<!\d) lookbehinds
        self._buf = ""  # raw text not yet emitted; never holds redacted output

    def feed(self, delta: str) -> str:
        self._buf += strip_invisible(delta)
        return self._drain(final=False)

    def flush(self) -> str:
        return self._drain(final=True)

    def _drain(self, final: bool) -> str:
        text = self._ctx + self._buf
        start = len(self._ctx)
        if final:
            limit = len(text)
        else:  # hold at most K, and only the tail that could still be PII (TTFT on prose)
            limit = _tail_start(text, max(start, len(text) - self._k))
        out, pos = "", start
        cut = max(limit, start)
        for m in PII_RE.finditer(text, start):  # pos= keeps the context char visible
            if m.end() > limit:
                cut = min(cut, m.start())  # may still grow: hold from its start, never past limit
                break
            out += text[pos:m.start()] + replacement(m)
            pos = m.end()
        out += text[pos:cut]
        self._ctx, self._buf = text[cut - 1:cut], text[cut:]
        return out

    @property
    def held(self) -> int:
        """Chars received but not yet emitted (for tests and logs)."""
        return len(self._buf)
