# Step 1: canonical-schema

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/ARCHITECTURE.md` — data flow, especially the note that the wire format is OpenAI's Chat Completions schema
- `/docs/ADR.md` — read ADR-008 (OpenAI-compatible wire format, Claude as preferred provider) and ADR-009 (streaming translation approach) closely; these define what this step's models must support
- `/docs/TRD.md` — §6.1 (gateway API spec), §7 (provider adapter interface — note the method signatures reference `OpenAIChatRequest`, `OpenAIChatResponse`, `OpenAIChatChunk`, which this step defines), §8 (observability attributes that will eventually be read off these objects — don't build observability now, just be aware some fields exist for that reason)
- `phases/proxy-layer/step0.md` and the code it produced: `gateway/` directory skeleton, `gateway/config/loader.py`, `gateway/db.py`, `gateway/main.py`. Read the actual files, not just the step description — check `git log` / the working tree for what step 0 actually built, since step 0's `summary` field in `phases/proxy-layer/index.json` only gives a one-line pointer.

## Task

Define the canonical OpenAI-compatible request/response/stream-chunk Pydantic models that every later step (mocks, provider adapters, gateway routing, streaming) imports and translates to/from. This step contains **no business logic** — only data shapes and basic field validation.

Create `gateway/schemas.py` with (at minimum — use your judgement on exact field lists, but cover everything listed):

```python
class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None

class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str | None

class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: Usage

class ChatCompletionChunkDelta(BaseModel):
    role: str | None = None
    content: str | None = None

class ChatCompletionChunkChoice(BaseModel):
    index: int
    delta: ChatCompletionChunkDelta
    finish_reason: str | None = None

class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChatCompletionChunkChoice]
```

Rules that must not be violated:
- Field names and nesting must match OpenAI's actual Chat Completions API shape (`choices[].message`, `choices[].delta`, `usage.prompt_tokens`/`completion_tokens`/`total_tokens`, etc.) — ADR-008 requires this to be the literal wire format, not an approximation. If you're unsure of an exact field name, prefer the well-known OpenAI shape over inventing your own.
- These models are used for both the gateway's public API (what team callers send/receive) and internally (what provider adapters return before any gateway-added fields are attached) — don't add gateway-internal fields (team_id, cost, latency) to these classes. Those belong to whatever internal wrapper types later steps introduce, not here.
- `ChatCompletionRequest` intentionally has no `X-Priority` field — that's a header, not a body field (ADR-020), and priority handling isn't part of this phase anyway.

Also add a short test file `tests/test_schemas.py` with a handful of cases: valid request parses, invalid `role` rejected, a full response round-trips through `.model_dump_json()` / `.model_validate_json()`.

## Acceptance Criteria

```bash
uv run ruff check .
uv run pytest tests/test_schemas.py -v
```

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Do the model field names match OpenAI's real Chat Completions schema (spot-check against the TRD or your own knowledge of the API)?
   - Is this file free of any gateway-internal concerns (auth, cost, provider routing)?
   - Does it avoid duplicating anything already defined in `gateway/config/loader.py` (step 0)?
3. Based on the result, update `phases/proxy-layer/index.json` step 1:
   - Success → `"status": "completed"`, `"summary": "one-line summary — module path and class names, for the next step to import"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't add request/response models specific to a single provider (e.g. an `AnthropicRequest` class) here. Reason: provider-native shapes belong in `gateway/providers/` (step 3), which translates to/from these canonical models — keeping native shapes here would blur the ADR-008 boundary.
- Don't implement any HTTP endpoints or client calls in this step. Reason: this is data-shape-only; step 2 (mocks) and step 6 (routing) are where these models get used over the wire.
- Do not break existing tests.
