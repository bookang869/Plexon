# Step 5: enrichment

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/ADR.md` — read ADR-015 (rule-based content filtering, not ML moderation) and ADR-021 (per-team enrichment/content-filter storage in `teams.config` jsonb) closely
- `/docs/TRD.md` — §5's `enrichment_defaults` YAML section, §4.1's `teams.config jsonb` column note
- `phases/proxy-layer/step0.md`'s actual output: `gateway/config/loader.py` (how `enrichment_defaults` is exposed from the global config)
- `phases/proxy-layer/step1.md`'s actual output: `gateway/schemas.py` (`ChatCompletionRequest`/`ChatMessage` — this step transforms these)
- `phases/proxy-layer/step4.md`'s actual output: `gateway/auth/team_auth.py` (`Team.config` — the raw per-team jsonb this step interprets)

## Task

Implement `gateway/enrichment/` — request enrichment (system prompt / disclaimer injection) and rule-based content filtering, merging global YAML defaults with per-team Postgres overrides.

### Config merge semantics

Global `enrichment_defaults` (YAML, step 0) defines the baseline. A team's `config` jsonb column (step 4's `Team.config`) can override or extend it. Define the merge precisely — don't leave it ambiguous:
- `system_prompt` / `disclaimer`: if the team's config sets one, it replaces the global default entirely for that team (not concatenated) — a team either wants the global default or its own, not both stacked silently.
- `content_filter.blocklist`: team-level blocklist entries are **added** to the global blocklist (union), not a replacement — a team can tighten filtering but the global baseline still applies. `content_filter.enabled`: team can override (e.g. explicitly disable) if their config sets it.

Model this merge as an explicit Pydantic model, e.g. `gateway/enrichment/config.py`:

```python
class ContentFilterConfig(BaseModel):
    enabled: bool
    blocklist: list[str]

class EnrichmentConfig(BaseModel):
    system_prompt: str | None
    disclaimer: str | None
    content_filter: ContentFilterConfig

def resolve_enrichment_config(global_defaults: EnrichmentDefaults, team_config: dict) -> EnrichmentConfig: ...
```

### `gateway/enrichment/enrich.py`

```python
def enrich_request(request: ChatCompletionRequest, config: EnrichmentConfig) -> ChatCompletionRequest: ...
```

- Injects `system_prompt` as a new leading `role: system` message if configured and no system message already exists in the request; if the request already has a system message, prepend the configured prompt to it rather than silently dropping either (both matter: the team's own prompt and the org-wide policy prompt).
- Appends `disclaimer` text to the end of the **last user message's** content if configured (simplest reasonable interpretation — this wasn't pinned down further in the ADRs, so keep it simple rather than inventing a separate disclaimer-injection point).
- Returns a new `ChatCompletionRequest` (don't mutate the input in place — the caller in `gateway-routing` (step 6) may want the original for logging).

### `gateway/enrichment/content_filter.py`

```python
class ContentFilterResult(BaseModel):
    blocked: bool
    matched_terms: list[str]

def check_content_filter(request: ChatCompletionRequest, config: ContentFilterConfig) -> ContentFilterResult: ...
```

- Case-insensitive keyword match (per ADR-015: rule-based, not ML) against every message's `content` in the request. Support blocklist entries as either plain substrings or `/regex/` (a string wrapped in slashes signals regex — document this convention in a docstring, since it's a real but small design choice).
- If `config.enabled` is `False`, always return `blocked=False, matched_terms=[]` without running any matching.
- This function only **detects** a violation — it doesn't raise an HTTP exception itself. The caller (`gateway-routing`, step 6) decides what to do with a blocked result (return `400`, per typical practice — but that wiring belongs to step 6, not here, to keep this module a pure decision function that's easy to unit test).

## Acceptance Criteria

```bash
uv run ruff check .
uv run pytest tests/test_enrichment.py -v
# tests/test_enrichment.py must cover:
#   - system prompt injection when request has no system message
#   - system prompt injection when request already has one (both preserved, order matters)
#   - disclaimer appended to last user message
#   - team-level blocklist entries add to (don't replace) the global blocklist
#   - team-level system_prompt override replaces (doesn't concatenate with) the global one
#   - content filter: substring match, regex match, case-insensitivity, disabled filter always passes
```

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Is the global-vs-team merge semantics exactly as specified above (replace for prompts, union for blocklist)?
   - Does `check_content_filter` stay a pure function with no HTTP/exception concerns?
   - Is this rule-based only — no ML/external moderation API call (ADR-015)?
3. Based on the result, update `phases/proxy-layer/index.json` step 5:
   - Success → `"status": "completed"`, `"summary": "one-line summary — function/module names, merge semantics, for the routing step to wire in"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't call any external moderation API or ML model. Reason: ADR-015 is explicit — rule-based only, to avoid scope creep into a content-moderation project.
- Don't raise HTTP exceptions from `content_filter.py`. Reason: keeps the module a pure, easily-unit-tested decision function; HTTP-layer decisions belong in `gateway-routing` (step 6).
- Don't mutate the input `ChatCompletionRequest` in place in `enrich_request`. Reason: the routing step may need the original request for logging/audit purposes later.
- Do not break existing tests.
