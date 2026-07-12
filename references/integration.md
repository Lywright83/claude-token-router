# Integration — wiring the router into an agent stack

The router is a decision layer. It does not call the API itself; it tells your agent loop which model to use, how to build the cached prefix, whether to batch, and when to escalate. Keep it that way — it stays portable across SDKs and MCP setups.

## The loop contract

```
task ---> router.route(task, role) ---> RouteDecision(tier, model_id, batch_eligible)
            |
            v
   build_cache_blocks(system_prompt, tool_definitions, persistent_context)
            |
            v
   YOUR API call (sync, or batch if batch_eligible)
            |
            v
   validate output  +  read model's self-reported confidence
            |
            v
   router.next_tier_on_failure(tier, validation_passed, confidence)
        -> None: done.  -> "sonnet"/"opus": retry once at that tier.
```

## 1. Routing a task

```python
from router import Router
r = Router("routing.yaml")

decision = r.route("write a FastAPI endpoint for the alert feed")
# decision.tier == "sonnet"; decision.model_id == "claude-sonnet-4-6"

# In a swarm, pass the role so a worker stays Haiku regardless of phrasing:
decision = r.route("analyze this host's processes", role="worker")
```

Decompose compound tasks before routing. "Build the detection module" is three subtasks — design (Opus), code (Sonnet), config + validation (Haiku). Route each. The classifier is for subtasks.

## 2. Caching — the biggest lever, get the mechanics right

The router builds content blocks and marks the end of the stable prefix with `cache_control`. The SDK caches everything up to and including the marked block.

```python
blocks = r.build_cache_blocks(
    system_prompt=SYSTEM_PROMPT,
    tool_definitions=TOOLS_TEXT,
    persistent_context=SCOPE_YAML,   # standing instructions, scope files
)
# Pass `blocks` as the cached prefix; append the per-turn variable content after.
```

Rules that decide whether you actually hit cache:
- **Byte-identical prefix.** One changed character in the cached span busts it. Keep volatile content (timestamps, turn counters) out of the cached blocks.
- **Order matters.** Cache-eligible content goes first, variable content last.
- **TTL choice.** 5-min for tight back-and-forth loops; 1-hour for context that lives across a long session or many spaced calls. The 1-hour write costs more (2x vs 1.25x) but survives gaps.
- **Minimum size.** Below ~1,024 tokens the write overhead isn't worth it; send uncached.

## 3. Batch — stack it on caching for async work

If `decision.batch_eligible` is true (nightly jobs, backfills, bulk reports, eval sweeps), submit through the Message Batches API for 50% off both directions. It completes within 24h and stacks with caching. Don't batch anything a human is waiting on.

## 4. Escalation contract

Default down a tier and escalate on failure rather than routing everything high. After each call, give the router two signals:

- **validation_passed** — did the output pass your check? (tests green, schema valid, parses, non-empty, etc.)
- **confidence** — optional 0–1 score. Easiest source: ask the model to end its output with a confidence value, or derive it from a validator. Below the threshold (default 0.6) escalates even if validation nominally passed.

```python
nxt = r.next_tier_on_failure(decision.tier, validation_passed=passed, confidence=conf)
if nxt:
    decision = r.route_at_tier(nxt)  # or just use nxt + models[nxt]["id"]
    # retry once
```

`max_escalations` defaults to 1 — one cheap miss plus one good retry still beats Opus-by-default. Raise it only if you've measured that two-step escalation pays off for your mix.

## 5. Cost instrumentation

Wrap every call with `estimate_cost` (predicted) and log actual token usage from the API response. Comparing the two over a week tells you whether your tier rules and cache-hit rate match reality, and where to retune `routing.yaml`. Don't trust a projected savings percentage — measure.

## MCP / GravityClaw-style stacks

The router is plain Python with one dependency (PyYAML), so it drops into an MCP server or a thin orchestration wrapper. Load `routing.yaml` once at startup; expose `route`, `build_cache_blocks`, and `next_tier_on_failure` as internal helpers the orchestrator calls before and after each model invocation. Keep `routing.yaml` in version control — it's the tuning surface, and edits there shouldn't require a code change.

## Retuning

Misroutes show up as escalations. If a class of task keeps escalating from Haiku to Sonnet, its signals belong under `sonnet`, not `haiku`. If Opus calls rarely change the outcome versus a Sonnet retry, tighten the `opus` signals. The config is meant to drift toward your actual workload over time.

## Fable 5 and the escalation guard (read before touching `tier_order`)

Fable 5 ($10/$50) returned to general availability on 2026-07-01. It sits at the top of `tier_order`, but **escalation deliberately cannot reach it.** `cost_guards.require_explicit_routing: [fable]` makes `next_tier_on_failure()` stop at Opus and return `None`.

That asymmetry is the whole point. Escalation is driven by a validator, and validators are flaky — a bad confidence signal or a brittle schema check can fail repeatedly. Without the guard, a 250-token Haiku grunt task could walk haiku → sonnet → opus → fable and bill 10x for work that never needed it. Cheap tiers should fail upward freely; the expensive one should require you to mean it.

To route to Fable, do it on purpose:

```python
d = r.route("long-horizon autonomous run, drift is costly")   # signal match
d = r.route("final call on the irreversible migration", role="final_arbiter")
d = r.route_at_tier("fable")                                   # explicit override
```

Log every Fable call (`cost_guards.audit_tiers`). The audit question is simple: *would Opus have produced the same decision?* If yes, that call was a routing bug — tighten the `fable` signals in `routing.yaml`. Fable earns its rate only when the extra capability changes the outcome, not the polish.

## Sonnet 5 migration — re-benchmark your cache before switching

Sonnet 5 uses a new tokenizer that yields **1.0–1.35x more tokens for the same text**. This interacts badly with caching if you migrate blind:

- Your cached prefix gets *larger* in token terms, so cache writes and reads both cost more than your old numbers predict.
- Any hardcoded token budgets, truncation thresholds, or context-window math derived from Sonnet 4.6 counts will be wrong.
- Cache-hit *rate* shouldn't change (that's about byte-identity, not tokenization), but cost per hit will.

Before migrating a cached-prefix workload: measure actual token counts on both models with your real prompts, recompute with `estimate_cost`, and compare. `sonnet_4_6` remains in `routing.yaml` as a pinned fallback with known-stable counts — keep it there until you've done the measurement.

## Logging (required for `cost_report.py`)

The router's tier rules are a hypothesis. `cost_report.py` tests it — but only if you log. Emit one JSONL line per call from your agent loop. Every field maps to something the API response already hands you:

```python
log.write(json.dumps({
    "task": task,
    "tier": decision.tier,
    "escalated_from": escalated_from,              # None if no escalation
    "input_tokens": resp.usage.input_tokens,
    "cached_input_tokens": resp.usage.cache_read_input_tokens,
    "cache_write_tokens": resp.usage.cache_creation_input_tokens,
    "output_tokens": resp.usage.output_tokens,
    "batch": decision.batch_eligible,
    "validation_passed": passed,
    "confidence": confidence,
}) + "\n")
```

Then: `python cost_report.py usage.jsonl`

Read the output in this order:

1. **Cache-hit rate.** Below ~40% on an agent loop means a busted prefix — something volatile leaked into the cached blocks. Fix this before touching tier rules; it's usually the biggest recoverable cost and requires no routing change at all.
2. **Escalation paths.** A high rate on one path (e.g. `haiku->sonnet` at 36%) means those signals sit in the wrong tier. Move them up in `routing.yaml`. But check the *dollar* column before you act — a loud escalation bug can cost less than a few uncached calls. Prioritize by spend, not by percentage.
3. **Zero escalations** is its own warning: you're routing too high. Demote signals and let escalation catch the misses.
4. **Fable audit.** For every Fable call, ask: would Opus have produced the same decision? If yes, that call was a routing bug.

Re-run weekly. `routing.yaml` should drift toward your actual workload over time — that's the design, not a defect.
