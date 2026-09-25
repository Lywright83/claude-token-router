---
name: claude-token-router
description: Route tasks across Claude model tiers (Haiku/Sonnet/Opus) to minimize token cost while preserving quality, with prompt caching, context trimming, batch routing, and confidence-gated escalation. Use this skill whenever the user is building or tuning a model-routing layer, an agent stack, or a multi-agent swarm; mentions token optimization, cost control, "which model should I use", routing.yaml, a thin router, cache hits, batch API, or wants to cut their Claude API bill. Trigger even when the user only says "optimize my Claude usage" or "route tasks across models" without naming the mechanism.
---

# Claude Token Router

A routing layer that picks the cheapest Claude model that can do each task correctly, then stacks the structural cost levers (caching, context discipline, batch) on top. Built for agentic stacks and multi-agent swarms, not one-off chats.

## Core principle

The router optimizes *which model* a task goes to. But for an agent stack, model choice is usually the **smaller** cost variable. The dominant drivers, in order of impact:

1. **Prompt caching** — repeated system prompts, tool schemas, scope files, and context re-sent every turn. Cache reads cost 0.1x base input (90% off; **0.05x on Opus 5.5, 0.025x on Fable 5.1**). This is almost always the biggest single win for an agent loop, because those loops are input-token-heavy. Note caches are **model-scoped**, so every tier hop is a guaranteed cache miss — see the cache-namespace warning below.
2. **Context discipline** — don't stuff full history into every subagent. Pass summarized state, not raw transcripts.
3. **Tier routing** — the 4x spread between Haiku ($1/$5) and Opus 5.5 ($4/$20) per MTok. Real, but secondary to the above.
4. **Batch API** — 50% off for anything async. Stacks with caching.
5. **Confidence-gated escalation** — catches misroutes cheaply.

A router layered on an uncached, context-bloated swarm underdelivers. Apply the levers as a stack. **When advising a user, lead with caching and context before model routing** — that's where the money is.

## Verified pricing (per MTok, USD — verified 2026-09-21, Opus 5.5 added 2026-09-25; confirm before quoting)

**Current lineup — these are what `routing.yaml` routes to:**

| Model | Input | Output | Cache read | 5-min write (1.25x) | 1-hr write (2x) |
|-------|-------|--------|------------|---------------------|-----------------|
| Haiku 4.5 (`claude-haiku-4-5`) | $1.00 | $5.00 | $0.10 | $1.25 | $2.00 |
| Sonnet 5 (`claude-sonnet-5`) | $2.00 | $10.00 | $0.20 | $2.50 | $4.00 |
| Opus 5.5 (`claude-opus-5-5`) | $4.00 | $20.00 | **$0.20** † | $5.00 | $8.00 |
| Fable 5.1 (`claude-fable-5-1`) | $10.00 | $50.00 | **$0.25** † | $12.50 | $20.00 |

**Legacy (still served, pinned in `routing.yaml` but not routed to):** Sonnet 4.6 `claude-sonnet-4-6` $3/$15 · Opus 5 `claude-opus-5` $5/$25 (opus's refusal fallback) · Opus 4.8 `claude-opus-4-8` $5/$25 · Fable 5 `claude-fable-5` $10/$50.

† **Opus 5.5 cache reads are 0.05x base input and Fable 5.1's are 0.025x, not the usual 0.1x.** Every other model reads at 0.1x. `routing.yaml` carries this as a per-model `cache_read_mult`, honored in `router.py`. Cache *write* multipliers are assumed standard (1.25x / 2x) — that half is not separately confirmed.

Output is 5x input on every tier. Batch API = 50% off input and output, and stacks with caching (a cached batch request can land near 5% of the standard non-cached cost).

**Five things that will bite you if ignored:**
- **Opus 5.5 is cheaper than the Opus it replaced ($4/$20 vs $5/$25) — and always thinks.** `thinking: {type: "disabled"}` and `budget_tokens` both return 400 at every effort level; `output_config.effort` is the only dial, and its default is `medium` (not `high`), so set it explicitly (`routing.yaml` carries a per-model `effort`). Thinking counts toward `max_tokens` — a limit sized for a thinking-off route truncates replies. Read content by block type (responses usually open with a thinking block). Forced `tool_choice` returns 400. Handle `stop_reason: "refusal"` (cyber / bio / reasoning_extraction classifiers) and opt into server-side fallbacks; a fallback to Opus 5 runs without Opus 5.5's thinking blocks.
- **Sonnet 5 is $2/$10 permanently.** The scheduled 2026-09-01 reversion to $3/$15 was **cancelled on 2026-08-10** — introductory pricing became the standard rate. Older copies of this skill (and `routing.yaml` v1) hardcoded $3/$15 defensively against that reversion and overstated every Sonnet estimate by 50%. If you see $3/$15 for Sonnet 5 anywhere, that source is stale.
- **The Haiku→Sonnet gap is 2x, not 3x.** Sonnet 5 at $2/$10 is only double Haiku. The cheap-tier savings are smaller than they look, and judged *per completed task* a Haiku call that needs a Sonnet retry is already a loss. Don't push marginal work down to Haiku on reflex.
- **Fable 5.1 is $10/$50 — 2.5x Opus 5.5 in both directions.** A misroute here is the single most expensive mistake the router can make. It is reachable only by explicit signal or the `final_arbiter` role; `cost_guards.require_explicit_routing` blocks escalation from drifting into it. Do not remove that guard. It also has breaking API differences (thinking always on, forced `tool_choice` returns 400, no prefill, 30-day retention required).
- **Sonnet 5 uses a new tokenizer (1.0–1.35x more tokens for the same text).** Since caching depends on byte-identical prefixes and token counts, **re-benchmark cache-hit rates and per-call token counts before migrating cached-prefix workloads onto it.** `sonnet_4_6` is kept as a pinned fallback with known-stable counts — but note it now costs *more* than Sonnet 5, so pin it for token stability, never for cost.

**Cache-namespace warning (the most important caveat in this skill).** Prompt caches are **model-scoped**. Every tier hop is a guaranteed cache miss on the shared prefix — the routed call pays full input price to rebuild a prefix the previous tier already had warm. On an input-heavy agent loop, the cache you lose by hopping tiers can exceed the per-token spread you gained. **Before adding a tier hop to save money, measure the simpler alternative first: one model at lower `effort`, keeping a single warm cache namespace.** Lower effort on a current model often matches or beats a prior-generation model at high effort.

**Effort is a lever this router does not price.** `output_config.effort` (`low`→`max`, default `high`; **`medium` on Opus 5.5**) changes token *volume*, not per-token rate, so it never shows up in `estimate_cost()` — but it is the first quality-trading lever after caching and is usually cheaper to reach for than a tier change. `routing.yaml` carries an `effort:` block with per-role defaults. Haiku 4.5 does not support effort at all.

Mythos 5.1 shares Fable's underlying model but is trusted-access only (Project Glasswing) — not publicly routable. These rates change; re-verify before committing budget.

## Tier definitions — route by complexity, not topic

The cheapest model that clears the quality bar is the right one. Misrouting up wastes tokens; misrouting down wastes time on retries.

**Haiku — mechanical, deterministic, low-ambiguity**
Log/field extraction, regex, format conversion (JSON↔YAML, markdown cleanup), classification with clear labels (severity tagging, signal flagging), single-doc summarization, YAML/syntax validation, and high-volume swarm subtasks where each call is simple. This is the right tier for the repetitive worker agents in a fan-out.

**Sonnet — applied reasoning, the workhorse (~70–80% of tasks)**
Code generation and debugging, multi-step technical instructions, query writing, framework mapping, standard professional writing, most agent orchestration and tool-use loops, and research synthesis from a handful of sources. This is the default. When in doubt between Sonnet and a neighbor, start here.

**Opus — novel design, deep ambiguity, high-stakes correctness**
Designing systems from scratch, adversarial/creative reasoning where it materially changes the result, multi-framework reasoning with tradeoffs, anything where a wrong answer is expensive and not obviously wrong, and final review of Sonnet output before it ships. The exception, not the default.

**Fable — frontier tier, opt-in only**
Long-horizon autonomous runs where drift is costly, genuinely novel work with no known approach, and final gates on decisions that are expensive *and irreversible*. The test is whether the extra capability changes the **outcome**, not merely the polish — if an Opus retry would plausibly get there, use Opus. At 2.5x Opus in both directions, this tier earns its cost only rarely. Log every Fable call; if one wouldn't have changed the decision versus Opus, that's a routing bug — tighten the signals.

## Routing cascade

```
1. Output deterministic / pattern-based?         -> Haiku
2. Needs judgment but a known approach exists?    -> Sonnet
3. Novel design, adversarial, or high-stakes?     -> Opus
4. Outcome-changing, long-horizon, irreversible?  -> Fable  (explicit only)
5. Uncertain between two tiers?                    -> pick the lower, escalate on failure
```

Escalation walks the ladder **up to Opus and stops**. Fable is never reached by escalation drift — only by deliberate routing. That asymmetry is intentional: the cheap tiers should fail upward freely, the expensive one should require you to mean it.

## How to apply it

1. **Decompose before routing.** A compound task ("build the detection module") isn't one tier. Split it: Opus designs the architecture, Sonnet writes the code, Haiku formats configs and validates YAML. Route each piece. The router classifies *subtasks*, not whole projects.
2. **Use rules, not an LLM, to classify.** A model call to decide the model spends tokens to save tokens. Use the keyword/heuristic classifier in `scripts/router.py` for most cases. Only reach for a model-based classifier when tasks are genuinely too ambiguous for rules — and then use Haiku for the classification call.
3. **Escalate, don't guess.** Default down a tier. If output fails validation (test fails, schema invalid, self-reported confidence below threshold), retry once at the next tier. One cheap miss + one good retry still beats sending everything to Opus.
4. **Swarm fan-out control.** Planner/orchestrator = Opus (the default since Opus 5.5 narrowed the Opus/Sonnet gap to 2x; drop to Sonnet only if `cost_report.py` shows it doesn't change outcomes). Repetitive worker subagents = Haiku. Never pay Opus rates for parallel grunts.
5. **Cache the stable prefix.** Put system prompt, tool definitions, and persistent context (scope files, standing instructions) in a cached block. Order matters: cache-eligible content goes first and must be byte-identical across calls or the cache misses.

## Files in this skill

- `routing.yaml` — declarative tier rules, escalation thresholds, caching, batch, and cost guards. The router reads this; edit it to retune without touching code. Read it before customizing rules.
- `scripts/router.py` — thin, dependency-light router. Rules-based classifier, cache-block builder, cost estimator, confidence-gated escalation. Read it before wiring into an agent stack; it's meant to be imported, not forked.
- `scripts/usage.py` — a runnable end-to-end agent turn with a stubbed API call (route → cache → call → validate → escalate → log). **Read this first when integrating** — it's the contract in ~120 lines. Swap `fake_call()` for a real client call and it's production.
- `scripts/cost_report.py` — reads a JSONL usage log and finds where the router is actually wrong: cache-hit rate, escalation paths (= misrouted signals), wasted spend on failed first attempts, and a Fable audit. Run `--demo` to see it work. **This closes the loop** — tier rules are a guess until this measures them.
- `references/cost-model.md` — worked cost examples and the math behind the levers. Read when sizing a budget or explaining savings.
- `references/integration.md` — wiring into an agent loop / MCP stack, caching mechanics, batch, the escalation contract, the Fable guard, and Sonnet 5 tokenizer migration. Read when integrating.

## Measure, don't assume

The tier rules in `routing.yaml` are a **guess** about your workload. `cost_report.py` tells you how good the guess was. The two diagnostics that matter:

- **Escalation rate per path.** High `haiku->sonnet` means those signals are in the wrong tier — you're paying for two calls where one would do. Move them up. Conversely, a **0% escalation rate means you're routing too high** — demote some signals and let escalation catch the misses. Some escalation is healthy; it means the cheap tiers are being trusted.
- **Cache-hit rate.** Below ~40% on an agent loop usually means a **busted prefix** — something volatile (timestamp, turn counter, session id) leaked into the cached blocks. One changed byte misses the whole cache.

Beware of chasing the *loudest* signal instead of the *most expensive* one. A 36%-escalation bug can cost less than a handful of uncached calls. Let the dollar column set your priority, not the percentage column.

## Guardrails

Re-verify pricing before quoting dollar figures — rates and model availability shift. Don't promise a specific percentage saving without the user's actual token mix; the savings depend entirely on cache-hit rate and task distribution. If the user's workload is millions of trivial classification calls, note honestly that a cheaper non-Claude model may win on that slice — routing is about fit, not loyalty.
