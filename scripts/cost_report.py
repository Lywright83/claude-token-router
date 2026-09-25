"""
cost_report.py — close the loop. Read real usage logs, find where the router
is actually wrong, and quantify what it's costing you.

The router's tier rules are a GUESS. This tells you how good the guess was.

Input: a JSONL file, one call per line. Log this from your agent loop —
every field maps to something the Anthropic API response already gives you:

    {"task": "extract IPs from log",
     "tier": "haiku",
     "escalated_from": null,          # tier we started at, if we escalated
     "input_tokens": 8000,            # usage.input_tokens
     "cached_input_tokens": 6000,     # usage.cache_read_input_tokens
     "cache_write_tokens": 0,         # usage.cache_creation_input_tokens
     "output_tokens": 1500,           # usage.output_tokens
     "batch": false,
     "validation_passed": true,
     "confidence": 0.9}

Run:  python cost_report.py usage.jsonl
      python cost_report.py --demo        # generate + analyze synthetic data
"""

from __future__ import annotations
from collections import defaultdict
import argparse
import json
import random
import sys

from router import Router


def load(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"  ! skipping malformed line {i}", file=sys.stderr)
    return rows


def analyze(r: Router, rows: list[dict]) -> None:
    if not rows:
        print("No rows to analyze.")
        return

    by_tier = defaultdict(lambda: {"n": 0, "cost": 0.0})
    total = 0.0
    cache_hit_tokens = 0
    total_input_tokens = 0

    escalations = defaultdict(int)      # "haiku->sonnet" -> count
    escalation_cost = 0.0               # what the WASTED first attempts cost
    uncached_calls = 0
    fable_calls = []

    for row in rows:
        tier = row["tier"]
        # Price at the model that actually served the call (e.g. an Opus 5.5
        # refusal that fell back to Opus 5), bucketed under its tier.
        price_key = r.key_for_model_id(row.get("model")) or tier
        est = r.estimate_cost(
            price_key,
            input_tokens=row.get("input_tokens", 0),
            output_tokens=row.get("output_tokens", 0),
            cached_input_tokens=row.get("cached_input_tokens", 0),
            cache_write_tokens=row.get("cache_write_tokens", 0),
            batch=row.get("batch", False),
        )
        by_tier[tier]["n"] += 1
        by_tier[tier]["cost"] += est.total
        total += est.total

        cache_hit_tokens += row.get("cached_input_tokens", 0)
        total_input_tokens += row.get("input_tokens", 0)
        if not row.get("cached_input_tokens"):
            uncached_calls += 1

        src = row.get("escalated_from")
        if src:
            escalations[f"{src}->{tier}"] += 1
            # The failed first attempt was pure waste. Price it at the source tier.
            wasted = r.estimate_cost(
                src,
                input_tokens=row.get("input_tokens", 0),
                output_tokens=row.get("output_tokens", 0),
                cached_input_tokens=row.get("cached_input_tokens", 0),
                batch=row.get("batch", False),
            )
            escalation_cost += wasted.total

        if tier == "fable":
            fable_calls.append(row.get("task", "(no task recorded)"))

    n = len(rows)
    hit_rate = (cache_hit_tokens / total_input_tokens * 100) if total_input_tokens else 0.0

    print("=" * 72)
    print(f"COST REPORT — {n} calls, ${total:.4f} total, ${total/n:.5f} avg/call")
    print("=" * 72)

    print("\nSPEND BY TIER")
    for tier in r.tier_order:
        if tier not in by_tier:
            continue
        d = by_tier[tier]
        share = d["cost"] / total * 100 if total else 0
        vol = d["n"] / n * 100
        print(f"  {tier:8} {d['n']:5} calls ({vol:4.1f}% vol)  "
              f"${d['cost']:8.4f} ({share:4.1f}% spend)")

    # --- Lever 1: caching. Usually the biggest miss. ----------------------
    print(f"\nCACHE HIT RATE: {hit_rate:.1f}% of input tokens served from cache")
    if uncached_calls:
        print(f"  ! {uncached_calls} calls ({uncached_calls/n*100:.0f}%) had ZERO cached input.")
        print(f"    Every one re-sent its full prefix at 10x the cache-read rate.")
        print(f"    This is almost always the single biggest recoverable cost.")
    if hit_rate < 40 and total_input_tokens:
        print(f"    A hit rate this low usually means a BUSTED PREFIX — something")
        print(f"    volatile (timestamp, turn counter, session id) leaked into the")
        print(f"    cached blocks. One changed byte misses the whole cache.")

    # --- Lever 2: misroutes, visible as escalations -----------------------
    print(f"\nESCALATIONS: {sum(escalations.values())} "
          f"({sum(escalations.values())/n*100:.1f}% of calls)")
    if escalations:
        for path, count in sorted(escalations.items(), key=lambda x: -x[1]):
            print(f"  {path:18} {count:4}x")
        print(f"  Wasted on failed first attempts: ${escalation_cost:.4f} "
              f"({escalation_cost/total*100:.1f}% of spend)")
        print("  -> A HIGH rate on one path means those task signals are in the")
        print("     wrong tier. Move them UP in routing.yaml — you're paying for")
        print("     two calls where one would do.")
    else:
        print("  None. If this stays at 0% you may be routing too HIGH —")
        print("  try demoting some signals and let escalation catch the misses.")

    # --- Lever 3: the expensive tier --------------------------------------
    if fable_calls:
        fc = by_tier["fable"]
        print(f"\nFABLE AUDIT: {fc['n']} calls, ${fc['cost']:.4f} "
              f"({fc['cost']/total*100:.1f}% of total spend)")
        for t in fable_calls[:5]:
            print(f"  - {t[:60]}")
        print("  -> For each: would OPUS have produced the same decision?")
        print("     If yes, that call was a routing bug. Tighten fable signals.")

    # --- Headline ---------------------------------------------------------
    print("\n" + "-" * 72)
    recoverable = escalation_cost
    if uncached_calls:
        print("TOP FIX: enable caching on the uncached calls. Biggest lever, "
              "and it\n         requires no change to your tier rules.")
    elif escalations:
        worst = max(escalations.items(), key=lambda x: -x[1])[0]
        print(f"TOP FIX: retune the '{worst}' path in routing.yaml. "
              f"${recoverable:.4f} wasted\n         on first attempts that were "
              f"always going to fail.")
    else:
        print("TOP FIX: nothing obvious. Cache is warm and routing is stable.")
    print("-" * 72)


def make_demo(path: str) -> None:
    """Synthetic log with two planted bugs: a busted cache and a bad haiku rule."""
    random.seed(7)
    rows = []
    for _ in range(60):  # haiku workers, but the rule is too aggressive
        esc = random.random() < 0.45   # 45% fail -> planted misroute
        rows.append({
            "task": "classify alert severity",
            "tier": "sonnet" if esc else "haiku",
            "escalated_from": "haiku" if esc else None,
            "input_tokens": 3000, "cached_input_tokens": 2400,
            "cache_write_tokens": 0, "output_tokens": 400,
            "batch": False, "validation_passed": True,
            "confidence": 0.9 if not esc else 0.4,
        })
    for _ in range(25):  # sonnet coding, cache NOT enabled -> planted bug
        rows.append({
            "task": "write detection rule", "tier": "sonnet", "escalated_from": None,
            "input_tokens": 9000, "cached_input_tokens": 0,
            "cache_write_tokens": 0, "output_tokens": 2000,
            "batch": False, "validation_passed": True, "confidence": 0.9,
        })
    for _ in range(3):
        rows.append({
            "task": "final call on irreversible cutover", "tier": "fable",
            "escalated_from": None, "input_tokens": 12000,
            "cached_input_tokens": 8000, "cache_write_tokens": 0,
            "output_tokens": 3000, "batch": False,
            "validation_passed": True, "confidence": 0.95,
        })
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile", nargs="?", help="JSONL usage log")
    ap.add_argument("--demo", action="store_true", help="generate + analyze synthetic data")
    ap.add_argument("--config", default="routing.yaml")
    args = ap.parse_args()

    r = Router(args.config)
    if args.demo:
        make_demo("demo_usage.jsonl")
        print("(synthetic demo data with two planted bugs)\n")
        analyze(r, load("demo_usage.jsonl"))
    elif args.logfile:
        analyze(r, load(args.logfile))
    else:
        ap.error("give a logfile or --demo")
