# Cost model — the math behind the levers

All prices USD per million tokens (MTok), verified 2026-09-21; Opus 5.5 added 2026-09-25. Output is 5x input on every current tier. **Re-verify before quoting dollar figures — rates and availability change.**

| Model | Input | Output | Cache read |
|-------|-------|--------|------------|
| Haiku 4.5 (`claude-haiku-4-5`) | $1.00 | $5.00 | $0.10 (0.1x) |
| Sonnet 5 (`claude-sonnet-5`) | $2.00 | $10.00 | $0.20 (0.1x) |
| Opus 5.5 (`claude-opus-5-5`) | $4.00 | $20.00 | **$0.20 (0.05x)** |
| Fable 5.1 (`claude-fable-5-1`) | $10.00 | $50.00 | **$0.25 (0.025x)** |

Legacy, pinned but not routed to: Sonnet 4.6 $3/$15 · Opus 5 $5/$25 · Opus 4.8 $5/$25 · Fable 5 $10/$50.

**Sonnet 5 is $2/$10 permanently.** The scheduled 2026-09-01 reversion to $3/$15 was cancelled on 2026-08-10; intro pricing became standard. Any source still quoting $3/$15 for Sonnet 5 is stale.

**Opus 5.5** at $4/$20 replaced Opus 5 ($5/$25) as the opus tier: 20% cheaper per token, 60% cheaper on cache reads (0.05x), and it tends to finish tasks in fewer tokens — so cost per solved task drops by more than the list price. It always thinks; `output_config.effort` (default `medium`) is its cost dial.

**Fable 5.1** at $10/$50 is 2.5x Opus 5.5 in both directions — the tier where a routing mistake actually costs real money. It is opt-in only in this router by design. Its cache reads are 0.025x base input, a 4x-cheaper read than the flat 0.1x assumption would give you; `routing.yaml` models this with a per-model `cache_read_mult`.

**Sonnet 5 tokenizer:** produces 1.0-1.35x more tokens for the same text. That inflates BOTH your token counts and your cached-prefix size, so a naive migration can cost more per call even at the same headline rate. Re-benchmark before switching cached workloads.

## The five levers and what each multiplies

1. **Prompt caching.** Cache read = 0.1x base input (90% off the cached portion; 0.05x on Opus 5.5, 0.025x on Fable 5.1). Cache write = 1.25x (5-min TTL) or 2.0x (1-hour TTL), paid once. The write is a one-time premium; every subsequent read inside the TTL is the discounted rate. Break-even is fast — typically after the second or third reuse of the cached prefix. **Caches are model-scoped:** a tier hop throws the warm prefix away and pays full input price to rebuild it on the new model. Price that loss before routing across tiers to save money.
2. **Batch API.** 0.5x on both input and output for async jobs (completed within 24h). Stacks with caching.
3. **Tier routing.** Haiku is **2x** cheaper than Sonnet and 4x cheaper than Opus 5.5 on input. The Opus→Sonnet gap is **2x** (was 2.5x against Opus 5's $5/$25); the Haiku→Sonnet gap is **2x**. That narrow Opus gap is why the swarm orchestrator role now routes to Opus. Fable is 2.5x Opus and 10x Haiku — the one gap large enough that a single misroute is visible on the invoice.
   Because the cheap-tier spreads are narrower than they look, judge **cost per completed task**: a Haiku call that needs a Sonnet retry has already cost more than going to Sonnet first.
4. **Context trimming.** Linear — fewer input tokens, proportionally lower cost. Biggest effect in swarms that re-send history to every subagent.
5. **Global vs US-only inference.** US-only routing carries a ~1.1x multiplier on recent models. Use global default unless data residency requires otherwise.

## Why caching usually beats routing for agent loops

An agent turn re-sends the system prompt, tool schemas, and persistent context every call. Say that stable prefix is 6,000 tokens and the per-turn variable input is 2,000 tokens, output 1,500, on Sonnet.

- **No caching:** (8,000 x $2 + 1,500 x $10) / 1e6 = $0.0310 per turn.
- **With caching** (prefix cached after first write): (2,000 x $2 + 6,000 x $0.20 + 1,500 x $10) / 1e6 = $0.0202 per turn.

That's ~35% off *without changing the model*. Over a 50-turn agent session, caching alone saves more than routing that same session down a tier would — and you can do both. (Cross-check: `python3 scripts/router.py` prints `sonnet = $0.02020` for exactly this 8k/6k-cached/1.5k shape.)

## Worked example: a swarm fan-out

Orchestrator (Opus) plans, then fans out to 6 worker subagents that each run a simple scan (Haiku), then a reviewer (Opus) does a final pass. Each worker: 250 input tokens (200 cached), 50 output, batch-eligible.

- **Worker cost each (Haiku + cache + batch):** (50 x $1 x 0.5 + 200 x $0.10 x 0.5 + 50 x $5 x 0.5) / 1e6 = **$0.00016**.
- **Six workers:** 6 x $0.00016 = ~$0.00096.
- Compare to running those 6 workers on Opus 5.5 synchronously, uncached: 6 x (250 x $4 + 50 x $20) / 1e6 = $0.0120 — roughly **12.5x more** for the same grunt work.

That's the compounding: right tier (Haiku) x caching x batch on the part of the workload that's high-volume and simple.

## Sizing a real budget

You can't quote a savings percentage without the user's actual token mix. The two numbers that decide everything:

1. **Cache-hit rate** — what fraction of input tokens are repeated, cacheable prefix. High in agent loops (often 60–90%), low in one-off diverse prompts.
2. **Task distribution** — what share of calls are genuinely Haiku-able vs need Sonnet vs truly need Opus. Most stacks are ~Haiku 20% / Sonnet 70% / Opus 10% by volume, but heavily workload-dependent.

Use `router.estimate_cost(...)` with the user's real per-call token counts and hit rate rather than guessing. The honest answer to "how much will I save" is: measure current spend, instrument the router, compare a week later.

## When Claude isn't the right tier at all

For millions of trivial classification/extraction calls where quality tolerance is high, a cheaper non-Claude small model can undercut even Haiku. Routing is about fit. If a slice of the workload is that simple and that high-volume, it's honest to route it off-platform and keep Claude for the work that needs it.
