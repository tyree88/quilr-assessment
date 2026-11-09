from guardrail.patterns import MAX_MATCH, PII_RE, replacement, strip_invisible


class StreamRedactor:
    """Redacts PII in a text stream while holding back at most K characters.

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
        limit = len(text) if final else len(text) - self._k
        out, pos, cut = "", start, None
        for m in PII_RE.finditer(text, start):  # pos= keeps the context char visible
            if m.end() > limit:
                cut = m.start()  # may still grow with more text: hold from its start
                break
            out += text[pos:m.start()] + replacement(m)
            pos = m.end()
        if cut is None:
            cut = max(limit, pos)
        out += text[pos:cut]
        self._ctx, self._buf = text[cut - 1:cut], text[cut:]
        return out

    @property
    def held(self) -> int:
        """Chars received but not yet emitted (for tests and logs)."""
        return len(self._buf)
