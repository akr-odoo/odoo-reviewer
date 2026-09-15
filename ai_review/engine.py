#!/usr/bin/env python3
"""
Review strategies — how the diff is split across agents before findings are collected.

    whole      one agent over the entire PR diff (cheap, predictable; the default)
    per-task   group commits sharing a [task_id] into one unit; one agent per unit,
               then a holistic synthesis agent that merges/dedups and adds cross-unit findings
    per-commit like per-task but one agent per commit

All strategies return the SAME shape — {"summary": str, "findings": [...]} plus a meta dict —
so the caller's anchoring/output code is identical regardless of strategy. Per-unit agents run
concurrently (they are independent `claude` subprocesses).
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import claude, prompt


def group_commits(commits_detail: list[dict[str, Any]], by: str) -> list[dict[str, Any]]:
    """Build review units. ``by`` is 'task' or 'commit'."""
    if by == "commit":
        return [
            {
                "label": f"commit {c['sha'][:8]} — {c['headline']}",
                "task_ids": c.get("task_ids", []),
                "commits": [c],
                "diff": c["diff"],
            }
            for c in commits_detail
            if c.get("diff")
        ]

    # by == 'task': commits sharing any [task_id] go together; task-less commits stand alone.
    groups: dict[tuple, list[dict[str, Any]]] = {}
    order: list[tuple] = []
    for c in commits_detail:
        key = tuple(sorted(c["task_ids"])) if c.get("task_ids") else ("__sha__", c["sha"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(c)

    units = []
    for key in order:
        members = groups[key]
        diff = "\n".join(c["diff"] for c in members if c.get("diff"))
        if not diff:
            continue
        if key[0] == "__sha__":
            label = f"commit {members[0]['sha'][:8]} — {members[0]['headline']}"
            task_ids = []
        else:
            label = f"task(s) {', '.join(map(str, key))}"
            task_ids = list(key)
        units.append({"label": label, "task_ids": task_ids, "commits": members, "diff": diff})
    return units


def _run(system_prompt: str, user_content: str, model: str, costs: list) -> dict:
    raw, meta = claude.run_review(system_prompt, user_content, model=model)
    costs.append(meta)
    return raw


def _find_phase(bundle, skill_text, strategy, model, workers, focus_diff, costs, log, context):
    """Discovery phase — returns (raw_review, strategy_label, units)."""
    system_prompt = prompt.build_system_prompt(skill_text, context=context)

    if focus_diff is not None:
        commit_lines = "\n".join(f"- {c['headline']}" for c in bundle.get("commits_detail", []))
        content = prompt.build_incremental_content(bundle, focus_diff, commit_lines)
        return _run(system_prompt, content, model, costs), "incremental", 0

    if strategy == "whole" or not bundle.get("commits_detail"):
        if strategy != "whole":
            log("No per-commit detail in bundle; falling back to whole-PR review.")
        return _run(system_prompt, prompt.build_user_content(bundle), model, costs), "whole", 0

    by = "commit" if strategy == "per-commit" else "task"
    units = group_commits(bundle["commits_detail"], by)
    if len(units) <= 1:
        log(f"Only one unit for strategy '{strategy}'; reviewing as a whole PR.")
        return _run(system_prompt, prompt.build_user_content(bundle), model, costs), "whole", 0

    log(f"Strategy '{strategy}': {len(units)} unit(s) + 1 synthesis pass "
        f"({min(workers, len(units))} parallel).")

    def review_unit(unit: dict) -> dict:
        try:
            raw = _run(system_prompt, prompt.build_group_content(bundle, unit), model, costs)
        except Exception as exc:  # one unit failing must not abort the whole grouped review
            log(f"  [unit '{unit['label']}' failed: {exc}]")
            return {"label": unit["label"], "summary": "", "findings": []}
        return {"label": unit["label"], "summary": raw.get("summary", ""),
                "findings": raw.get("findings", [])}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        unit_results = list(pool.map(review_unit, units))
    for r in unit_results:
        log(f"  · {r['label']}: {len(r['findings'])} finding(s)")

    synth_input = json.dumps(unit_results, ensure_ascii=False, indent=2)
    final = _run(system_prompt, prompt.build_synthesis_content(bundle, synth_input), model, costs)
    return final, strategy, len(units)


def _verify_phase(bundle, findings, model, costs, log, context):
    """Critic phase — attach a verdict + confidence to each finding by refuting it."""
    if not findings:
        return
    log(f"Verifying {len(findings)} finding(s) with a skeptical critic pass …")
    content = prompt.build_verification_content(bundle, findings)
    raw, meta = claude.run_review(prompt.build_verify_system(context), content, model=model)
    costs.append(meta)
    verdicts = {
        v["index"]: v for v in raw.get("verdicts", [])
        if isinstance(v, dict) and isinstance(v.get("index"), int)
    }
    for i, f in enumerate(findings):
        v = verdicts.get(i)
        if not v:
            f["verdict"] = f.get("verdict") or "uncertain"
            continue
        f["verdict"] = str(v.get("verdict", "uncertain")).lower()
        f["verify_reason"] = str(v.get("reason", ""))
        # Keep the critic's confidence on its OWN axis — do not clobber the finder's
        # self-rated "confidence" (suppression blends verdict + finder confidence).
        try:
            f["verify_confidence"] = float(v["confidence"])
        except (KeyError, TypeError, ValueError):
            pass
    refuted = sum(1 for f in findings if f.get("verdict") == "refuted")
    log(f"  critic: {refuted} refuted, {len(findings) - refuted} kept for review.")


def review(
    bundle: dict[str, Any],
    skill_text: str,
    *,
    strategy: str = "whole",
    model: str = claude.DEFAULT_MODEL,
    workers: int = 4,
    verify: bool = True,
    focus_diff: str | None = None,
    context: str = "pstech",
    log: Callable[[str], None] = lambda _m: None,
) -> tuple[dict, dict]:
    """Return (raw_review, meta). raw_review = {"summary", "findings"} with each finding
    annotated by the critic pass (verdict/confidence) when ``verify`` is on."""
    costs: list[dict] = []
    raw, strat_label, units = _find_phase(
        bundle, skill_text, strategy, model, workers, focus_diff, costs, log, context
    )
    if verify:
        _verify_phase(bundle, raw.get("findings", []), model, costs, log, context)
    return raw, _aggregate(costs, model, strategy=strat_label, units=units, verified=verify)


def _aggregate(costs: list[dict], model: str, *, strategy: str, units: int = 0,
               verified: bool = False) -> dict:
    total_cost = sum(c.get("cost_usd") or 0 for c in costs)
    denials = [d for c in costs for d in c.get("permission_denials", [])]
    return {
        "model": model,
        "strategy": strategy,
        "units": units,
        "agents": len(costs),
        "verified": verified,
        "cost_usd": total_cost,
        "permission_denials": denials,
    }
