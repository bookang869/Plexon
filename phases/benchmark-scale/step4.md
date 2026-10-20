# Step 4: budget-overshoot-investigation

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/ADR.md` — especially ADR-002 (portfolio/demo rigor, not production hardening) and ADR-004 (Redis hot-path / Postgres durable-state split) — this step adds one new ADR entry in the same style
- `gateway/ratelimit/budget.py` — `check_budget` (reads current spend, decides admit/block) and `record_spend` (increments spend, called only after the provider has already answered) — this is the check-then-act race being investigated
- `gateway/routes.py` — confirms the call order: `check_budget` happens before the provider call, `record_spend` is awaited after the response is built, so N concurrent in-flight requests can all read pre-request spend and all get admitted before any of them records
- `/benchmarks/bench_ratelimit_budget.py`'s `test_budget_overshoot_under_concurrency` and its module docstring — this is the existing benchmark that measures and bounds the overshoot (currently ~3.09% at 40-concurrency against a cap sized to 97% of the full wave's cost)
- `/benchmarks/thresholds.py`'s `BUDGET_OVERSHOOT_MAX_PCT` comment

## Task

This is a documentation-only step: confirm the root cause of the measured budget overshoot and record it as an explicit, accepted engineering decision — not a step to change `budget.py`'s behavior. Per direction for this phase, the ~3.09% overshoot at the benchmark's tested concurrency is not worth blocking the project over; the goal is to make sure it's *understood* and *on the record*, not to eliminate it now.

1. Confirm (by reading the two files above, and re-running the existing benchmark once if useful) that the mechanism is exactly the check-then-act race already gestured at in `bench_ratelimit_budget.py`'s module docstring: `check_budget` reads the Redis spend counter and decides admit/block *before* the provider call; `record_spend`'s `INCRBYFLOAT` only happens after that request's response is already built. Concurrent requests admitted in the same narrow window can all observe pre-request spend and all get admitted, so the overshoot is bounded by the cost of however many requests are in flight at the moment spend crosses the cap — not an ongoing leak, and not something the token-bucket's atomic-EVAL admission model helps with here (dollar spend isn't a per-request atomic decrement the way a rate-limit token is, since the cost of a request isn't known until the provider responds).

2. Add one new entry to `/docs/ADR.md`, following the existing numbering and format (see ADR-002/ADR-004 for the section style: `### ADR-0XX: <title>` with **Context**, **Decision**, **Consequences**). Use the next available ADR number in sequence. Content should cover, in your own words:
   - **Context**: budget enforcement is check-then-act (`check_budget` then, later, `record_spend`), not atomic; `benchmarks/bench_ratelimit_budget.py`'s `test_budget_overshoot_under_concurrency` measures and bounds this at ~3% overshoot under a 40-request concurrent wave.
   - **Decision**: accept the bounded overshoot as documented, known behavior rather than redesigning budget admission to be atomic (e.g. a Redis-EVAL reserve-then-confirm scheme) right now — consistent with ADR-002's "production-grade behavior where it's the point of the project, not everywhere" framing; rate limiting (ADR-011's token bucket) is the primitive that needed atomicity, budget enforcement's looser bound is an accepted tradeoff.
   - **Consequences**: overshoot is bounded by in-flight concurrency at the moment of crossing (not unbounded), continuously measured by the existing benchmark as a regression signal via `BUDGET_OVERSHOOT_MAX_PCT`; revisit with an atomic reserve-based design if this project's scope or a real deployment ever requires a harder budget guarantee.

3. Do not modify `gateway/ratelimit/budget.py`, `gateway/routes.py`, or `benchmarks/bench_ratelimit_budget.py` in this step.

## Acceptance Criteria

```bash
grep -c "^### ADR-" docs/ADR.md   # confirm exactly one new entry was appended vs. the pre-step count
ruff check .   # unaffected, but confirms nothing else was accidentally touched
git diff --stat   # confirm only docs/ADR.md changed
```

## Verification Procedure

1. Run the AC commands above.
2. Read the new ADR entry back and confirm it accurately describes the check-then-act mechanism (not a vaguer "there's some overshoot" statement) and explicitly states the decision to accept it, not fix it, with the reasoning tied to ADR-002.
3. Confirm `git diff --stat` shows only `docs/ADR.md` changed — no code files touched.
4. Based on the result, update this step's entry in `phases/benchmark-scale/index.json`:
   - Success → `"status": "completed"`, `"summary": "one-line summary of the output"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't modify `gateway/ratelimit/budget.py` or any other gateway code. Reason: explicit direction for this phase is to investigate and document, not fix — "wouldn't hold the whole project up over 3.09%."
- Don't modify `benchmarks/bench_ratelimit_budget.py`. Reason: it already measures and bounds the behavior this step documents; no benchmark change is needed.
- Don't remove or contradict ADR-002's existing scope framing. Reason: this new entry should read as consistent with, not a reversal of, that decision.
- Don't break existing tests (none should be affected by a docs-only change; run `uv run pytest` if in doubt).
