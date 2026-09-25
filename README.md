# claude-token-router

A model-tier routing layer for Claude agent stacks. Routes each task to the cheapest model that can do it correctly, and stacks prompt caching, batching, and confidence-gated escalation on top.

Rules-based. No LLM call to decide which LLM to call.

```
route -> build cached prefix -> call -> validate -> escalate (at most once) -> log
```

## The thing most routers get wrong

A model-picking router optimizes *which model* a task goes to. But in an agent loop, that's usually the **smaller** cost variable. Agent loops re-send the same system prompt, tool schemas, and context on every turn — they're input-token-heavy, and caching that prefix cuts the repeated portion by 90%.

Cost levers, in order of actual impact:

1. **Prompt caching** — cache reads cost 0.1x base input (0.05x on Opus 5.5, 0.025x on Fable 5.1). Biggest win for agent loops. Caches are **model-scoped**, so every tier hop is a guaranteed cache miss — price that loss before routing across tiers to save money.
2. **Context discipline** — don't stuff full history into every subagent.
3. **Tier routing** — the 5–10x spread between Haiku and the top tiers. (Haiku→Sonnet is only 2x; don't push marginal work down on reflex.)
4. **Batch API** — 50% off async work. Stacks with caching.
5. **Confidence-gated escalation** — catches misroutes cheaply.

Routing bolted onto an uncached, context-bloated swarm underdelivers. This repo does all five.

## Quick start

```bash
pip install pyyaml

python scripts/router.py          # routing + escalation guard + cost demo
python scripts/usage.py           # full agent turn, stubbed API call
python scripts/cost_report.py --demo   # find misroutes in synthetic data
```

Then integrate:

```python
from router import Router
r = Router("routing.yaml")

d = r.route("extract all IPs from this log")     # -> haiku
d = r.route("write a detection rule")            # -> sonnet
d = r.route("architect this from scratch")       # -> opus
d = r.route("scan this host", role="worker")     # -> haiku (role override)

blocks = r.build_cache_blocks(
    system_prompt=SYSTEM, tool_definitions=TOOLS, persistent_context=SCOPE
)

# after the call:
nxt = r.next_tier_on_failure(d.tier, validation_passed=passed, confidence=conf)
if nxt:
    d = r.route_at_tier(nxt)   # retry once, one tier up
```

See `scripts/usage.py` for the full loop and `references/integration.md` for wiring details.

## The escalation guard

Escalation walks the ladder **up to Opus and stops**. The top tier is reachable only by explicit signal, an explicit role, or `route_at_tier()`.

```
haiku  fails -> sonnet
sonnet fails -> opus
opus   fails -> None      <- dead-ends here, on purpose
```

Escalation is driven by a validator, and validators are flaky. Without this guard, a bad confidence score could walk a 250-token grunt task from the cheapest tier to the most expensive one and bill 10x for work that never needed it. Cheap tiers should fail upward freely; the expensive one should require you to mean it.

Configured in `routing.yaml`:

```yaml
cost_guards:
  require_explicit_routing: [fable]
  audit_tiers: [opus, fable]
```

**Don't remove it.** It's the difference between a router and a leak.

## Measure, don't assume

The tier rules in `routing.yaml` are a **guess** about your workload. `cost_report.py` tests the guess against real usage logs.

Sample output from the bundled demo (which has two bugs planted in it):

```
SPEND BY TIER
  haiku    28 calls (31.8% vol)  $0.0795 ( 4.4% spend)
  sonnet   57 calls (64.8% vol)  $1.1318 (63.3% spend)
  fable     3 calls ( 3.4% vol)  $0.5760 (32.2% spend)

CACHE HIT RATE: 38.1%
  ! 25 calls (28%) had ZERO cached input.

ESCALATIONS: 32 (36.4% of calls)
  haiku->sonnet   32x
  Wasted on failed first attempts: $0.0909 (5.1% of spend)

TOP FIX: enable caching on the uncached calls.
```

Read that carefully. The escalation bug is **louder** (36% of calls) but the caching bug is **more expensive**. Eyeball this and you'd chase the wrong one. The report ranks by dollars, not percentages — that's the entire point of it.

Two counterintuitive rules that fall out of using it:

- **Cache-hit rate below ~40% on an agent loop = busted prefix.** Something volatile (timestamp, turn counter, session ID) leaked into the cached blocks. One changed byte misses the whole cache. Fix this before touching tier rules.
- **A 0% escalation rate is a warning, not a win.** It means you're routing too high. Some escalation means the cheap tiers are being trusted.

## Files

| Path | What it is |
|------|-----------|
| `SKILL.md` | The playbook — cost levers, tier definitions, routing cascade |
| `routing.yaml` | Tier rules, escalation thresholds, caching/batch/cost-guard policy. **The tuning surface.** |
| `scripts/router.py` | The router. Import it, don't fork it. |
| `scripts/usage.py` | Full agent turn with a stubbed call. The integration contract. |
| `scripts/cost_report.py` | Reads JSONL usage logs, finds your real misroutes |
| `references/cost-model.md` | The math, with worked examples |
| `references/integration.md` | Wiring, caching mechanics, logging schema |

## Pricing caveats (read before trusting cost output)

Rates change. **Re-verify against the official pricing page before committing budget.** Two live traps baked into the config on purpose:

- **Intro pricing expires.** Where a model has a promotional rate, `routing.yaml` hardcodes the **standard** rate deliberately, so estimates don't silently under-report when the promo ends.
- **Tokenizers differ between model generations.** A newer model can produce more tokens for the same text, inflating both your cached prefix and per-call counts. Benchmark real token counts on your actual prompts before migrating a cached workload to a new model generation.

## When Claude isn't the answer

If a slice of your workload is millions of trivial, high-volume classification calls, a cheaper non-Claude model may beat even the cheapest Claude tier on that slice. Routing is about fit, not loyalty. Route it off-platform and keep Claude for the work that needs it.

## License

MIT. See [LICENSE](LICENSE).
