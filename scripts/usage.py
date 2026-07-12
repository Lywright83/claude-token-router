"""
usage.py — a full agent turn, end to end, with a stubbed API call.

Runnable with no API key. Swap `fake_call()` for a real Anthropic client call
and this becomes your production loop. The point is to show the CONTRACT:

    route -> build cached prefix -> call -> validate -> escalate (maybe) -> log

Run:  python usage.py
"""

from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional
import json
import random

from router import Router

# --- The stable prefix. This is what gets cached. -------------------------
# CRITICAL: byte-identical across calls or you miss the cache. Keep timestamps,
# turn counters, and anything else volatile OUT of these three strings.
SYSTEM_PROMPT = "You are a security analysis agent. Be precise. Cite evidence."
TOOL_DEFINITIONS = json.dumps([{"name": "grep_logs", "description": "search logs"}])
PERSISTENT_CONTEXT = "scope: prod-web-tier\nrules_of_engagement: read-only\n"


@dataclass
class TurnResult:
    task: str
    tier: str
    model_id: str
    escalated_from: Optional[str]
    validation_passed: bool
    confidence: float
    est_cost_usd: float
    batch: bool


def fake_call(model_id: str, blocks: list, task: str) -> tuple[str, float]:
    """Stand-in for the real API call.

    Replace with:
        client.messages.create(
            model=model_id,
            max_tokens=1024,
            system=blocks,               # cached prefix goes here
            messages=[{"role": "user", "content": task}],
        )

    Returns (output_text, confidence). In production, get confidence either by
    asking the model to emit a score, or by deriving it from your validator.
    Here we fake a weak result from the cheapest tier to demo escalation.
    """
    weak = "haiku" in model_id
    confidence = 0.35 if weak else 0.9
    return f"[{model_id}] result for: {task[:40]}", confidence


def validate(output: str) -> bool:
    """Your real check: schema parse, tests green, non-empty, regex match, etc.
    Returning False here is what triggers escalation."""
    return bool(output) and "result for" in output


def run_turn(r: Router, task: str, role: str | None = None) -> TurnResult:
    # 1. ROUTE ------------------------------------------------------------
    d = r.route(task, role=role)
    escalated_from = None

    # 2. CACHE ------------------------------------------------------------
    # Same three parts every turn -> cache hit after the first write.
    blocks = r.build_cache_blocks(
        system_prompt=SYSTEM_PROMPT,
        tool_definitions=TOOL_DEFINITIONS,
        persistent_context=PERSISTENT_CONTEXT,
    )

    # 3. CALL -------------------------------------------------------------
    output, confidence = fake_call(d.model_id, blocks, task)

    # 4. VALIDATE ---------------------------------------------------------
    passed = validate(output)

    # 5. ESCALATE (at most once; never drifts into fable) ------------------
    nxt = r.next_tier_on_failure(d.tier, validation_passed=passed, confidence=confidence)
    if nxt:
        escalated_from = d.tier
        d = r.route_at_tier(nxt)
        output, confidence = fake_call(d.model_id, blocks, task)
        passed = validate(output)

    # 6. COST + LOG -------------------------------------------------------
    # Use REAL token counts from the API response (usage.input_tokens,
    # usage.cache_read_input_tokens, usage.output_tokens). Estimated here.
    est = r.estimate_cost(
        d.tier,
        input_tokens=8000,
        output_tokens=1500,
        cached_input_tokens=6000,   # the stable prefix, served from cache
        batch=d.batch_eligible,
    )

    return TurnResult(
        task=task,
        tier=d.tier,
        model_id=d.model_id,
        escalated_from=escalated_from,
        validation_passed=passed,
        confidence=confidence,
        est_cost_usd=est.total,
        batch=d.batch_eligible,
    )


if __name__ == "__main__":
    r = Router("routing.yaml")

    tasks = [
        ("extract all source IPs from the auth log", None),
        ("write a detection rule for lateral movement", None),
        ("architect a novel zero-trust segmentation design from scratch", None),
        ("nightly bulk report of all alert volumes", None),
        ("scan this host", "worker"),
        ("final call on the irreversible cutover", "final_arbiter"),
    ]

    print(f"{'TIER':8} {'ESC':>10}  {'COST':>9}  {'BATCH':5}  TASK")
    print("-" * 78)
    total = 0.0
    for task, role in tasks:
        res = run_turn(r, task, role)
        total += res.est_cost_usd
        esc = f"{res.escalated_from}->" if res.escalated_from else ""
        print(
            f"{res.tier:8} {esc:>10}  ${res.est_cost_usd:8.5f}  "
            f"{str(res.batch):5}  {res.task[:34]}"
        )
    print("-" * 78)
    print(f"{'TOTAL':8} {'':>10}  ${total:8.5f}")

    print("\nNote the haiku task escalated to sonnet (confidence 0.35 < 0.6).")
    print("Note nothing drifted into fable — it was only reached by explicit role.")
