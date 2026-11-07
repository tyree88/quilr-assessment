# Task 3 — LLM Gateway Streaming Guardrail (PII redaction)

Root `CLAUDE.md` rules apply. This file adds task context.
**Format: coding assessment.** The rubric is about *how the stream is handled*, not about the LLM. Mock the LLM.

## Task statement (verbatim from assessment)

> Implement an LLM Gateway proxy endpoint that routes text generation requests to an LLM provider and streams the response back to the client.
>
> Requirements:
> - Intercept the returning response chunk stream in real time.
> - Parse incoming stream deltas to detect and redact sensitive patterns (such as Emails, SSNs, or credit card numbers), replacing them with `[REDACTED]`.
> - Ensure the stream stays responsive without accumulating the full response in memory, minimizing Time To First Token (TTFT) and overall stream latency.
>
> Evaluation criteria:
> - Efficient asynchronous stream chunking and buffer state management.
> - Performant string/regex pattern matching over partial text streams.
> - Memory efficiency and low-latency proxying design.

## The core problem (say this back to me before any code)

**PII spans chunk boundaries.** `"john@exa"` + `"mple.com"` — neither chunk matches an email regex. A naive per-chunk regex **leaks**.
**Holding everything until the end fixes leaks but destroys TTFT.** Rubric penalizes both.

The solution is a **bounded carry-over buffer**: emit everything that *cannot* be the start of a PII match; hold back only a tail of length `K = max possible PII length − 1`. Flush the tail at stream end.

Everything else is plumbing.

## Design decisions (settled unless rubric says otherwise)

| Decision | Choice | Why |
|---|---|---|
| Framework | Starlette `StreamingResponse` over an `async` generator | Rubric: async stream chunking. No framework buffering. |
| Upstream protocol | OpenAI-style SSE (`data: {...}\n\n`, `choices[0].delta.content`, `data: [DONE]`) | Most common wire shape; reviewer recognizes it instantly |
| Upstream | **Mock provider** in-repo: async generator that yields deltas with configurable split points and `asyncio.sleep` jitter | Deterministic tests for boundary cases; no API key needed to grade |
| Real provider | Optional `PROVIDER=openai` branch using `httpx.AsyncClient.stream` | Shows we know the real thing; off by default |
| Redaction unit | Operate on the **text delta**, re-wrap into SSE frame | Patterns live in text, not in JSON framing |
| Buffer bound | `K = 64` chars default (longest PII we match ≈ 50 for email) — derived, not guessed, documented | Rubric: buffer state management |
| Patterns | Compiled once at import, combined into one alternation regex | Rubric: performant matching |

## Docs to pull on entry

1. Starlette `StreamingResponse` — async generator, `media_type="text/event-stream"`, headers (`Cache-Control: no-cache`, `X-Accel-Buffering: no`)
2. `httpx.AsyncClient.stream(...)` + `aiter_lines()` / `aiter_bytes()` for upstream SSE
3. SSE wire format (`data:` lines, blank-line frame terminator, `[DONE]`)
4. Python `re` — `re.finditer`, `Pattern.search(pos=)`, why `str` slicing in a loop is O(n²) and how to avoid it
5. OpenAI chat completions streaming chunk schema (for the mock's shape)

## Redaction algorithm (we pair-build this function first — it is the whole grade)

```
state: carry: str = ""          # ≤ K chars we have not emitted yet

on_delta(text):
    buf = carry + text
    buf = PII_RE.sub("[REDACTED]", buf)       # redact every complete match
    # Now: anything in the last K chars *might* be the prefix of a PII match. Hold it.
    if len(buf) > K:
        emit buf[:-K]
        carry = buf[-K:]
    else:
        carry = buf
        emit nothing

on_end():
    emit PII_RE.sub("[REDACTED]", carry)       # final flush; nothing can follow
    carry = ""
```

**Grill points on this algorithm (you ask, I answer):**
1. Re-redacting `carry` each time: a `[REDACTED]` token is 10 chars and contains no digits/`@` → cannot re-match. Prove it or pick a sentinel that cannot match.
2. **Partial-match optimization**: instead of always holding K, hold only from the **earliest position in the tail that could start a match** (e.g. last `@`, last digit run, last `\w`). Cuts latency on prose. **Ponytail call**: do we ship it? Recommended: ship the simple K-hold first, add `could_start_pii(tail)` only if time allows, mark `# ponytail`.
3. TTFT: with K=64, first emit waits for 64 chars (~16 tokens). Acceptable? Alternatives: lower K to 40 and drop credit-card-with-spaces support, or implement the partial-match optimization. **Make me choose, and write the number in README.**
4. Why not a streaming automaton (Aho-Corasick / `regex` module partial matching)? Right answer for prod; overkill for 3 patterns and an assessment. State the upgrade path.

## Patterns (start set — pair-build each, with test strings)

| Kind | Regex (sketch) | Max len | Edge cases to test |
|---|---|---|---|
| Email | `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}` | cap at 64 via bounded quantifiers | split at `@`, split inside TLD, trailing `.` |
| SSN | `\b\d{3}-\d{2}-\d{4}\b` | 11 | `123-45-6789` vs phone `123-456-7890` (must **not** match), split after first dash |
| Credit card | `\b(?:\d[ -]?){13,19}\b` then **Luhn check in code** | 23 | 16 digits with spaces, with dashes, 15-digit Amex, random 16 digits failing Luhn (must **not** redact) |

Luhn in code, not regex → fewer false positives → rubric: *robust*. One function, one test with a known-valid test number (e.g. `4111 1111 1111 1111`).

## Proposed module layout (names only)

```
task3-llm-stream-guardrail/
├── pyproject.toml                 starlette · uvicorn · httpx · pydantic · pytest · pytest-asyncio
├── src/llm_guardrail/
│   ├── patterns.py                compiled PII_RE, K, luhn_ok(), redact(text) -> str
│   ├── redactor.py                class StreamRedactor: feed(delta)->str, flush()->str   (the algorithm above; pure, sync, no I/O)
│   ├── sse.py                     parse_sse_frame(line)->dict|None, make_sse_frame(dict)->bytes, DONE
│   ├── provider.py                Protocol: async def stream(req)->AsyncIterator[str delta]  · MockProvider(script, delay_ms) · OpenAIProvider (optional)
│   ├── app.py                     POST /v1/chat/completions → StreamingResponse(pipeline(...))
│   └── config.py                  PROVIDER, K_OVERRIDE, UPSTREAM_URL, TIMEOUT
└── tests/
    ├── test_redactor_unit.py      feed char-by-char vs whole string → identical output (property: chunking-invariant)
    ├── test_boundaries.py         every PII example split at every index → always redacted
    ├── test_false_positives.py    phone numbers, dates, 16 random digits failing Luhn, "a@b" → untouched
    ├── test_latency.py            with mock delay 20ms/token: first client byte arrives before token #(K/4+2); memory: no list of all chunks (assert via tracemalloc or by streaming 100k tokens under a fixed RSS budget)
    └── test_e2e.py                ASGITransport client reads SSE frames; frames well-formed; [DONE] forwarded
```

## Pressure Test Checklist

- [ ] **Chunking invariance**: for every test string, `"".join(feed(c) for c in s) + flush() == redact(s)` for char-level, word-level, and random splits
- [ ] Email split at `@` → redacted
- [ ] SSN split after `123-` → redacted
- [ ] Card `4111 1111 1111 1111` split every 4 chars → redacted; `4111 1111 1111 1112` (bad Luhn) → **untouched**
- [ ] Phone `512-555-0199` → untouched
- [ ] Stream of 100,000 tokens: process RSS delta < 10 MB; no `list` accumulation (grep for `.append(` in the hot path)
- [ ] TTFT measured: log timestamp of first upstream delta and first client byte; delta ≤ K-tokens × token-interval + ε. **Number goes in README.**
- [ ] Upstream disconnect mid-stream → client gets a final SSE `error` event, not a hung connection
- [ ] Client disconnect → upstream request cancelled (`asyncio.CancelledError` handled; no orphan task)
- [ ] `[DONE]` forwarded **after** `flush()` output, never before
- [ ] Non-stream request (`stream: false`) → either rejected `400` or passed through unredacted-then-redacted — **grill**; default: reject, scope says streaming
- [ ] Response headers: `text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`
- [ ] `ruff` clean, `pytest -q` green

## Interview questions you will drill me on

- "Walk me through what happens when an email is split across three chunks."
- "What is K and how did you derive it? What happens if a PII string is longer than K?" (→ it leaks; K is the contract; document the max supported length)
- "Why is memory bounded? Point to the line."
- "What is your TTFT cost in tokens, and how would you cut it?" (partial-match hold, lower K, automaton)
- "Why Luhn in code instead of a tighter regex?"
- "How does this behave under backpressure if the client reads slowly?" (async generator yields → natural backpressure; upstream read pauses)
- "What would you add for production?" (allow-list of safe tokens, per-tenant pattern sets, audit log of redaction counts — not the content)
