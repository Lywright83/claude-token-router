# Cost model — the math behind the levers

All prices USD per million tokens (MTok), verified 2026-07-11. Output is 5x input on every current tier. **Re-verify before quoting dollar figures — rates and availability change.**

| Model | Input | Output |
|-------|-------|--------|
| Haiku 4.5 | $1.00 | $5.00 |
| Sonnet 5 | $3.00* | $15.00* |
| Sonnet 4.6 | $3.00 | $15.00 |
| Opus 4.8 | $5.00 | $25.00 |
| Fable 5 | $10.00 | $50.00 |

\* Sonnet 5 intro pricing is $2/$10 through 2026-08-31, then reverts to $3/$15. Budget at the standard rate.

**Fable 5** returned to general availability on 2026-07-01 (the June 12 export-control suspension was lifted June 30). At $10/$50 it is 2x Opus in both directions — the tier where a routing mistake actually costs real money. It is opt-in only in this router by design.

**Sonnet 5 tokenizer:** produces 1.0-1.35x more tokens for the same text. That inflates BOTH your token counts and your cached-prefix size, so a naive migration can cost more per call even at the same headline rate. Re-benchmark before switching cached workloads.

## The five levers and what each multiplies

1. **Prompt caching.** Cache read = 0.1x base input (90% off the cached portion). Cache write = 1.25x (5-min TTL) or 2.0x (1-hour TTL), paid once. The write is a one-time premium; every subsequent read inside the TTL is the 90%-off rate. Break-even is fast — typically after the second or third reuse of the cached prefix.
2. **Batch API.** 0.5x on both input and output for async jobs (completed within 24h). Stacks with caching.
3. **Tier routing.** Haiku is 3x cheaper than Sonnet and 5x cheaper than Opus on input. The Opus→Sonnet gap is smaller than people expect (1.67x); the Haiku→Sonnet gap (3x) is the bigger swing. Fable is 2x Opus and 10x Haiku — the one gap large enough that a single misroute is visible on the invoice.
4. **Context trimming.** Linear — fewer input tokens, proportionally lower cost. Biggest effect in swarms that re-send history to every subagent.
5. **Global vs US-only inference.** US-only routing carries a ~1.1x multiplier on recent models. Use global default unless data residency requires otherwise.

## Why caching usually beats routing for agent loops

An agent turn re-sends the system prompt, tool schemas, and persistent context every call. Say that stable prefix is 6,000 tokens and the per-turn variable input is 2,000 tokens, output 1,500, on Sonnet.

- **No caching:** (8,000 x $3 + 1,500 x $15) / 1e6 = $0.0465 per turn.
- **With caching** (prefix cached after first write): (2,000 x $3 + 6,000 x $0.30 + 1,500 x $15) / 1e6 = $0.0285 per turn.

That's ~39% off *without changing the model*. Over a 50-turn agent session, caching alone saves more than routing that same session down a tier would — and you can do both.

## Worked example: a swarm fan-out

Orchestrator (Sonnet) plans, then fans out to 6 worker subagents that each run a simple scan (Haiku), then a reviewer (Opus) does a final pass. Each worker: 250 input tokens (200 cached), 50 output, batch-eligible.

- **Worker cost each (Haiku + cache + batch):** (50 x $1 x 0.5 + 200 x $0.10 x 0.5 + 50 x $5 x 0.5) / 1e6 = **$0.00016**.
- **Six workers:** ~$0.001.
- Compare to running those 6 workers on Opus synchronously, uncached: 6 x (250 x $5 + 50 x $25) / 1e6 = $0.0150 — roughly **90x more** for the same grunt work.

That's the compounding: right tier (Haiku) x caching x batch on the part of the workload that's high-volume and simple.

## Sizing a real budget

You can't quote a savings percentage without the user's actual token mix. The two numbers that decide everything:

1. **Cache-hit rate** — what fraction of input tokens are repeated, cacheable prefix. High in agent loops (often 60–90%), low in one-off diverse prompts.
2. **Task distribution** — what share of calls are genuinely Haiku-able vs need Sonnet vs truly need Opus. Most stacks are ~Haiku 20% / Sonnet 70% / Opus 10% by volume, but heavily workload-dependent.

Use `router.estimate_cost(...)` with the user's real per-call token counts and hit rate rather than guessing. The honest answer to "how much will I save" is: measure current spend, instrument the router, compare a week later.

## When Claude isn't the right tier at all

For millions of trivial classification/extraction calls where quality tolerance is high, a cheaper non-Claude small model can undercut even Haiku. Routing is about fit. If a slice of the workload is that simple and that high-volume, it's honest to route it off-platform and keep Claude for the work that needs it.
