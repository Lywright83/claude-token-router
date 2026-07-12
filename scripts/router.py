"""
router.py — a thin, dependency-light router for Claude model tiers.

Reads routing.yaml. Classifies a task (rules-based, no LLM call), builds a
cached prefix block, estimates cost, and implements confidence-gated
escalation. Designed to be imported into an agent stack, not forked.

Only stdlib + PyYAML. Install: pip install pyyaml

Quick start:
    from router import Router
    r = Router("routing.yaml")
    decision = r.route("extract all IPs from this log file")
    # decision.tier == "haiku", decision.model_id == "claude-haiku-4-5"

    # Build a cached request prefix (stable across calls -> cache hits):
    prefix = r.build_cache_blocks(
        system_prompt=SYSTEM, tool_definitions=TOOLS, persistent_context=SCOPE
    )

    # Estimate cost before sending:
    est = r.estimate_cost("sonnet", input_tokens=8000, output_tokens=1500,
                          cached_input_tokens=6000, batch=False)

    # Escalation contract: after a call, report back.
    nxt = r.next_tier_on_failure("haiku", validation_passed=False, confidence=0.4)
    # nxt == "sonnet"  (or None if no escalation warranted)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import yaml


@dataclass
class RouteDecision:
    tier: str
    model_id: str
    reason: str
    matched_signals: list = field(default_factory=list)
    batch_eligible: bool = False


@dataclass
class CostEstimate:
    tier: str
    input_cost: float
    output_cost: float
    cache_write_cost: float
    total: float
    notes: str = ""


class Router:
    def __init__(self, config_path: str = "routing.yaml"):
        # Resolve relative to the repo root (parent of scripts/) so the scripts
        # work whether you run them from the root or from inside scripts/.
        # An absolute path, or one that exists as given, is used as-is.
        p = Path(config_path)
        if not p.is_absolute() and not p.exists():
            candidate = Path(__file__).resolve().parent.parent / config_path
            if candidate.exists():
                p = candidate
        with open(p, "r") as f:
            self.cfg = yaml.safe_load(f)
        self.models = self.cfg["models"]
        self.rules = self.cfg["rules"]
        self.mult = self.cfg["multipliers"]
        self.tier_order = self.cfg["tier_order"]
        self.default_tier = self.cfg.get("default_tier", "sonnet")

    # -- classification -----------------------------------------------------
    def classify(self, task: str) -> RouteDecision:
        """Rules-based tier selection. No model call. Checks tiers low->high so
        that a higher-tier signal (e.g. 'architect') wins over an incidental
        low-tier word, while genuinely simple tasks settle on haiku."""
        t = task.lower()
        best_tier = None
        best_matches: list = []
        # Walk low -> high; let the highest matching tier win.
        for tier in self.tier_order:
            sigs = [s for s in self.rules.get(tier, {}).get("signals", []) if s in t]
            if sigs:
                best_tier = tier
                best_matches = sigs
        if best_tier is None:
            return RouteDecision(
                tier=self.default_tier,
                model_id=self.models[self.default_tier]["id"],
                reason="no signal matched; using default tier",
            )
        return RouteDecision(
            tier=best_tier,
            model_id=self.models[best_tier]["id"],
            reason=f"matched {best_tier} signals",
            matched_signals=best_matches,
        )

    def is_batch_eligible(self, task: str) -> bool:
        bcfg = self.cfg.get("batch", {})
        if not bcfg.get("enabled"):
            return False
        t = task.lower()
        return any(s in t for s in bcfg.get("eligible_signals", []))

    def route(self, task: str, role: Optional[str] = None) -> RouteDecision:
        """Full routing. If a swarm role is given, role mapping overrides the
        text classifier (a worker is a worker regardless of phrasing)."""
        if role:
            roles = self.cfg.get("swarm", {}).get("roles", {})
            if role in roles:
                tier = roles[role]
                d = RouteDecision(
                    tier=tier,
                    model_id=self.models[tier]["id"],
                    reason=f"swarm role '{role}' -> {tier}",
                )
                d.batch_eligible = self.is_batch_eligible(task)
                return d
        d = self.classify(task)
        d.batch_eligible = self.is_batch_eligible(task)
        return d

    def route_at_tier(self, tier: str) -> RouteDecision:
        """Force a decision at a specific tier (used after escalation)."""
        return RouteDecision(
            tier=tier,
            model_id=self.models[tier]["id"],
            reason=f"forced tier '{tier}' (escalation)",
        )
    def next_tier_on_failure(
        self, current_tier: str, validation_passed: bool, confidence: float = 1.0
    ) -> Optional[str]:
        """Confidence-gated escalation. Returns the next tier up, or None if no
        escalation is warranted (passed + confident) or already at the top."""
        esc = self.cfg.get("escalation", {})
        if not esc.get("enabled"):
            return None
        threshold = esc.get("confidence_threshold", 0.6)
        if validation_passed and confidence >= threshold:
            return None
        idx = self.tier_order.index(current_tier)
        if idx + 1 >= len(self.tier_order):
            return None  # already top tier
        nxt = self.tier_order[idx + 1]

        # Cost guard: tiers listed in require_explicit_routing (e.g. fable at
        # $10/$50) must never be reached by escalation drift. They are opt-in
        # only — routed to deliberately via signals or role. Without this, a
        # flaky validator could walk a cheap task all the way up the ladder and
        # quietly bill 10x. Stop at the last non-guarded tier instead.
        guarded = self.cfg.get("cost_guards", {}).get("require_explicit_routing", [])
        if nxt in guarded:
            return None
        return nxt

    # -- caching ------------------------------------------------------------
    def build_cache_blocks(self, **parts) -> list:
        """Build Anthropic-style content blocks with cache_control on the
        stable prefix. Pass the configured cache_parts as kwargs (e.g.
        system_prompt=..., tool_definitions=..., persistent_context=...).
        Concatenated text must be byte-identical across calls to hit cache."""
        ccfg = self.cfg.get("caching", {})
        if not ccfg.get("enabled"):
            return [{"type": "text", "text": v} for v in parts.values() if v]
        ttl = ccfg.get("ttl", "5m")
        ordered = ccfg.get("cache_parts", list(parts.keys()))
        blocks = []
        for name in ordered:
            text = parts.get(name)
            if text:
                blocks.append({"type": "text", "text": text})
        # Mark the LAST block of the stable prefix with cache_control; the SDK
        # caches everything up to and including the marked block.
        if blocks:
            blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": ttl}
        return blocks

    # -- cost ---------------------------------------------------------------
    def estimate_cost(
        self,
        tier: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_ttl: str = "5m",
        batch: bool = False,
    ) -> CostEstimate:
        """Estimate USD cost for one call. cached_input_tokens read from cache
        at 0.1x; cache_write_tokens billed at the write multiplier once."""
        m = self.models[tier]
        in_rate = m["input_per_mtok"] / 1_000_000
        out_rate = m["output_per_mtok"] / 1_000_000
        batch_mult = self.mult["batch"] if batch else 1.0

        fresh_in = max(0, input_tokens - cached_input_tokens)
        input_cost = fresh_in * in_rate * batch_mult
        cache_read_cost = cached_input_tokens * in_rate * self.mult["cache_read"] * batch_mult
        write_mult = self.mult["cache_write_1h"] if cache_ttl == "1h" else self.mult["cache_write_5m"]
        cache_write_cost = cache_write_tokens * in_rate * write_mult  # writes not batch-discounted
        output_cost = output_tokens * out_rate * batch_mult

        total = input_cost + cache_read_cost + cache_write_cost + output_cost
        notes = []
        if batch:
            notes.append("batch 50% off")
        if cached_input_tokens:
            notes.append(f"{cached_input_tokens} tok cache-read @0.1x")

        # Cost guard: flag calls above the configured per-call ceiling. Fable at
        # $10/$50 makes this worth surfacing before you send, not after.
        warn_at = self.cfg.get("cost_guards", {}).get("warn_above_usd_per_call")
        if warn_at and total > warn_at:
            notes.append(f"!! WARN: ${total:.4f} exceeds ${warn_at:.2f}/call guard")

        return CostEstimate(
            tier=tier,
            input_cost=round(input_cost + cache_read_cost, 6),
            output_cost=round(output_cost, 6),
            cache_write_cost=round(cache_write_cost, 6),
            total=round(total, 6),
            notes="; ".join(notes),
        )


if __name__ == "__main__":
    r = Router("routing.yaml")
    samples = [
        ("extract all IPs and timestamps from this log", None),
        ("write a FastAPI endpoint for the signal feed", None),
        ("architect a novel multi-agent detection pipeline from scratch", None),
        ("nightly bulk-summarize all alerts into a report", None),
        ("run this scan", "worker"),
        ("escalate to fable: long-horizon autonomous run, drift is costly", None),
        ("final call on the irreversible migration", "final_arbiter"),
    ]
    print("ROUTING:")
    for task, role in samples:
        d = r.route(task, role=role)
        print(f"  [{d.tier:6}] batch={d.batch_eligible!s:5} {task[:46]!r}")

    print("\nESCALATION GUARD (fable must NOT be reachable by drift):")
    for tier in ["haiku", "sonnet", "opus", "fable"]:
        nxt = r.next_tier_on_failure(tier, validation_passed=False, confidence=0.2)
        print(f"  {tier:6} fails -> {nxt}")

    print("\nCOST (8k in / 6k cached / 1.5k out):")
    for tier in r.tier_order:
        e = r.estimate_cost(tier, 8000, 1500, cached_input_tokens=6000)
        print(f"  {tier:6} = ${e.total:.5f}  [{e.notes}]")
