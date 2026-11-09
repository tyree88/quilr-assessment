# Task 3: streaming PII guardrail for an LLM gateway

`POST /v1/chat/completions` forwards an OpenAI-style streaming request to an upstream
provider and streams the answer back, replacing emails, SSNs and card numbers with
`[REDACTED]` as the text flows. It never holds the whole response.

## Run

```bash
uv sync
uv run pytest -q                      # 97 tests
uvx ruff@0.17.4 check .
uv run python probe.py                # 30 hostile cases over a live uvicorn socket
uv run uvicorn guardrail.app:app      # gateway on :8000, built-in mock upstream
```

```bash
curl -N localhost:8000/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"demo","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

Set `UPSTREAM_URL` (and optionally `UPSTREAM_API_KEY`) to proxy a real OpenAI-compatible
endpoint instead of the mock.

## Design

```
upstream SSE bytes ─ httpx aiter_lines ─ iter_events ─ delta_text ─ StreamRedactor ─ chunk_frame ─ client
                     (stall timeout)     (WHATWG SSE)  (choices[0])  (bounded hold)
```

| File | Role |
|---|---|
| `guardrail/patterns.py` | `EMAIL`, `SSN`, `CARD`, `luhn_ok`, one `PII_RE` alternation, `redact()` reference |
| `guardrail/redactor.py` | `StreamRedactor.feed()` / `flush()`: the bounded carry buffer |
| `guardrail/sse.py` | SSE line parser, chunk content extraction, output frames |
| `guardrail/app.py` | Starlette endpoint, request validation, upstream status check, relay |
| `guardrail/provider.py` | Mock upstream on a real `httpx` transport (scripted, delays, failures) |
| `probe.py` | Live-socket hostile-input run |

### The core problem

PII can span chunks (`"john@exa"` + `"mple.com"`), so a per-chunk regex leaks. Holding the
whole response fixes that but destroys time to first token. `StreamRedactor` keeps only
what could still turn into a match:

1. **Commit rule.** Each `feed()` scans `buffer` once with `PII_RE.finditer`. A match that ends
   at or before the hold limit is final: emitted as `[REDACTED]`. The first match that ends
   later may still grow (`john@example.co` + `m`), so the buffer is held from its start.
   Everything before the hold point is emitted at once.
2. **Raw carry.** The buffer only ever holds raw text, never redacted output, so `[REDACTED]`
   can never be re-matched.
3. **One context char.** The digit lookbehinds (`(?<!\d)`) need the char before the buffer, so the
   last emitted char is kept and the scan uses `finditer(text, pos=1)`. Slicing would fake a
   string start there (`Pattern.search` docs: `pos` is not the same as slicing).
4. **Hold limit.** `limit = max(len − K, start of the trailing run that could still be PII)`.
   A growing match has to reach the end of the buffer through chars of one class
   (`[A-Za-z0-9._%+@-]` plus digits, or digits, space and dash for spaced cards), so on prose
   only the last partial word is held.
5. **Flush.** `flush()` commits everything at stream end, and `[DONE]` is sent after it.

**Invariant, tested:** for any chunking of `s`, `"".join(feed(c)) + flush() == redact(s)`.
That is tested for whole, per-char, per-word and random splits, for every pair of split
indices around each PII example, and with a fuzz test over 400 PII-dense strings. The fuzz
test found a real bug (text between the limit and a pending match was emitted); it is fixed.

### K derivation

Every quantifier is bounded, so the longest possible match is a provable number:

| Pattern | Max length |
|---|---|
| Email: local ≤ 64 (RFC 5321 §4.5.3.1.1) + `@` + domain ≤ 48 + `.` + TLD ≤ 14 | **128** |
| Card: 19 digits + 18 single separators | 37 |
| SSN: `ddd-dd-dddd` | 11 |

`K = MAX_MATCH = 128`. The patterns look at most one char past a match (`(?!\d)`), so a match
ending at or before `len − K` has seen everything it could need. `StreamRedactor` refuses
`k < MAX_MATCH`. **What the caps cost:** a local part over 64 chars leaks its head (a 70-char
one leaks its first 6, asserted in `test_pii_longer_than_k_leaks_its_head_as_documented`), and
an address whose domain part (before the last dot) is over 48 chars or whose TLD is over 14
letters is **not matched at all** and leaks whole, even though it is far shorter than K:
`jane@` + 49 chars + `.com` is 58 chars (asserted in `test_email_over_domain_cap_is_not_matched`).

### Numbers

| Measure | Result | Proof |
|---|---|---|
| Held buffer | ≤ 2 × 128 chars, whatever the stream length | `test_buffer_stays_bounded` (20k feeds) |
| TTFT on prose | first byte after **1 token** (22 ms at 20 ms/token over uvicorn); a plain K-hold would wait 128 chars ≈ 25 such tokens | `probe.py` TTFT line, `test_first_byte_arrives_after_one_token_not_after_k_chars` |
| Memory, 100k-token stream | peak traced ≈ 50 KB (limit asserted: 10 MB), 4,000 redactions | `test_100k_token_stream_stays_in_bounded_memory` |
| Frames | one client frame per upstream token, no batching | same TTFT test |
| Cost per delta | one `finditer` over ≤ K + held + delta chars; bounded quantifiers keep it linear | `redactor.py` `_drain` |

### Failure behaviour

| Situation | Client sees |
|---|---|
| Upstream returns non-200 | `502 {"error":{"code":"upstream_error"}}`, checked **before** the stream starts (after the first byte the status is fixed at 200); no upstream body is echoed |
| Upstream unreachable | `502 upstream_unavailable` |
| Mid-stream reset / stall > `stall_s` / malformed chunk / end without `[DONE]` | final `event: error` frame with a fixed code (`upstream_error`, `upstream_timeout`, `upstream_protocol_error`, `upstream_truncated`). The held tail is **dropped**, not flushed: it may be the head of PII whose end never came (fail closed). |
| Client disconnects | Starlette (ASGI spec 2.3 under uvicorn 0.34) cancels the generator; the upstream is closed in a shielded `finally` and by a background task. Tested over raw ASGI and live sockets. |
| `stream: false`, unknown fields (e.g. `n`), bad types, body > 1 MB | `400` / `413` with field names only, never input values |

Response headers: `text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`.

## Verification notes

- `httpx.ASGITransport` buffers the whole response and reports a disconnect only after the
  body ends (httpx 0.28 `_transports/asgi.py`). So TTFT, disconnect and memory are tested by
  calling the ASGI app directly (`tests/test_stream_behaviour.py`), and over real sockets in
  `probe.py`.
- The mock upstream is an `httpx.MockTransport` with its own byte stream, so the gateway's real
  client path runs (status check, `aiter_lines`, `aclose`). It yields to the event loop on
  every step like a socket does; without that, the 100k-token test showed 43 MB, because
  cancelled per-line timeout timers were never purged.
- Every guard was mutation-checked (removed, test run, failure confirmed): the four digit
  lookarounds, the Luhn branch and inner-SSN redaction, the hold, the context char, the K guard,
  the partial hold's char classes, the status check, `[DONE]` ordering, body cap, stall
  timeout, upstream close.

## Known limits / upgrades

- **Email caps leak.** A local part over 64 chars leaks its head; a domain part over 48 chars
  or a TLD over 14 letters is not matched and leaks whole (from 58 chars up, well under K).
  Raising the caps raises K; the cost is TTFT only while the tail still looks like PII.
- **Pattern coverage.** Not matched: SSNs without dashes or separated by spaces
  (`123 45 6789`); cards with double spaces, or separated by non-breaking spaces (U+00A0) or
  dots (`4111.1111.1111.1111`); internationalized emails. Upgrade: normalize separators
  before matching, add patterns per tenant.
- **Zero-width joiner** (U+200D) is kept because emoji need it, so `john\u200d@x.com` is not
  joined back together.
- **Only `choices[0].delta.content` is redacted.** Tool-call arguments and `refusal` text pass
  untouched; extra chunk fields (`id`, `finish_reason`, `usage`) are not forwarded.
- **No per-line size cap.** `aiter_lines()` buffers a line until its newline, so one huge
  upstream event is held whole (the probe sends 1 MB fine). Upgrade: a bounded line splitter.
- **Production:** an allow-list for safe tokens, per-tenant pattern sets, a redaction-count
  audit log (counts, never content), and the `regex` module's `partial=True` matching or an
  automaton instead of rescanning the held tail.
