#!/usr/bin/env python3
"""
ai_review — stage-by-stage AI code review for GitHub PRs.

    python -m ai_review doctor                 # check setup (gh / claude / Odoo)
    python -m ai_review fetch  <pr> [-R o/r]   # pull PR + task context -> bundle.json
    python -m ai_review review <pr> [-R o/r]   # run Claude -> review.json + review.md
    python -m ai_review push   <pr> [-R o/r]   # post a DRAFT review (edit review.json first)
    python -m ai_review run    <pr> [-R o/r]   # fetch + review (stops before push)
    python -m ai_review process <pr> [-R o/r]  # one shot: fetch + review + push

<pr> may be a PR URL, owner/repo#123, or a bare number with -R owner/repo.
The AI only ever sees text; only this tool touches GitHub and Odoo.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from . import github, odoo, skill, claude, context, engine
from .config import MARKER, PRRef, odoo_creds, load_dotenv
from .diff import parse_diff
from .model import Finding, Review, load_json, save_json

try:
    from rich.console import Console
    _c = Console()
    def out(msg: str = "") -> None: _c.print(msg)
except ImportError:  # degrade gracefully
    def out(msg: str = "") -> None: print(msg)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ref(args: argparse.Namespace) -> PRRef:
    return PRRef.parse(args.pr, getattr(args, "repo", None))


def _bundle_path(ref: PRRef):
    return ref.work_dir() / "bundle.json"


def _review_paths(ref: PRRef):
    d = ref.work_dir()
    return d / "review.json", d / "review.md"


def _state_path(ref: PRRef):
    return ref.work_dir() / "state.json"


def _is_duplicate(f: Finding, existing: list[dict]) -> bool:
    """A finding duplicates an existing PR comment only when it targets the SAME file and the
    SAME line. Kept strict on purpose: a looser proximity match would suppress distinct findings
    that merely sit near a human comment. Note this reliably catches ai_review's own comments
    only once the draft review has been submitted (pending comments aren't returned by the API)."""
    if not f.path or not f.line:
        return False
    for c in existing:
        cl = c.get("line")
        if c.get("path") == f.path and cl and int(cl) == f.line:
            return True
    return False


def _apply_suppression(review: Review, existing: list[dict], min_conf: float) -> None:
    """Mark findings that should not be posted (kept in review.json for transparency):
    - refuted by the critic → always suppressed;
    - not confirmed AND finder-confidence below threshold → suppressed (a confirmed finding is
      kept even at low finder-confidence, since the critic independently validated it);
    - duplicates an existing comment on the same line.
    The critic's own confidence lives on verify_confidence and is not conflated with this."""
    for f in review.findings:
        reasons = []
        if f.verdict == "refuted":
            reasons.append("refuted by critic")
        elif f.verdict != "confirmed" and f.confidence < min_conf:
            reasons.append(f"confidence {f.confidence:.2f} < {min_conf:.2f}")
        if _is_duplicate(f, existing):
            reasons.append("duplicate of an existing comment")
        if reasons:
            f.suppressed = True
            f.suppressed_reason = "; ".join(reasons)


def _summary_body(review: Review) -> str:
    """PR-review body: the summary plus any non-anchored (general) findings."""
    parts = [review.summary.strip() or "_No summary provided._"]
    general = review.general
    if general:
        parts.append("\n### General findings (not tied to a diff line)")
        for f in general:
            loc = f" (`{f.path}`)" if f.path else ""
            parts.append(f"- **[{f.severity}] {f.title}**{loc}: {f.body}")
    parts.append(f"\n_🤖 Draft review by `ai_review` ({review.model})._ {MARKER}")
    return "\n".join(parts)


def _render_finding(f: Finding) -> list[str]:
    tag = "inline" if (f.anchored and f.line) else "general"
    loc = f"{f.path}:{f.line}" if f.line else f.path or "—"
    verdict = f" · verdict: **{f.verdict}** ({f.verify_confidence:.2f})" if f.verdict else ""
    lines = [
        f"### [{f.severity}] {f.title}  _({tag})_",
        f"`{loc}` · {f.category} · confidence {f.confidence:.2f}{verdict}",
        "",
        f.body,
    ]
    if f.verify_reason:
        lines += ["", f"> _critic: {f.verify_reason}_"]
    if f.suggestion.strip():
        lines += ["", "```suggestion", f.suggestion.rstrip(), "```"]
    lines.append("")
    return lines


def _render_md(ref: PRRef, review: Review) -> str:
    lines = [
        f"# AI review — {ref.slug}#{ref.number}",
        "",
        f"Model: `{review.model}` · {len(review.inline)} inline · {len(review.general)} general"
        f" · {len(review.suppressed)} suppressed",
        "",
        "> Edit `review.json` to add/remove/adjust findings, then `push`. "
        "Suppressed findings below are kept for transparency and are NOT posted.",
        "",
        "## Summary",
        review.summary or "_(none)_",
        "",
        "## Findings to post",
    ]
    postable = review.postable
    if not postable:
        lines.append("_None._")
    for f in postable:
        lines += _render_finding(f)
    if review.suppressed:
        lines += ["", "## Suppressed (not posted)"]
        for f in review.suppressed:
            loc = f"{f.path}:{f.line}" if f.line else (f.path or "—")
            lines.append(f"- **[{f.severity}] {f.title}** (`{loc}`) — _{f.suppressed_reason}_")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        mark = "[green]✓[/green]" if passed else "[red]✗[/red]"
        out(f"  {mark} {label}" + (f" — {detail}" if detail else ""))

    out("ai_review doctor\n")
    try:
        login = github.auth_login()
        check("gh authenticated", True, f"logged in as {login}")
    except Exception as exc:  # noqa: BLE001
        check("gh authenticated", False, str(exc).splitlines()[0])

    import shutil
    check("claude CLI present", shutil.which("claude") is not None, "run Claude Code at least once to log in")

    creds = odoo_creds(task=True)
    if not creds.complete:
        check("Odoo credentials", False, "set ODOO_URL/DB/USER/API_KEY (or TASK_ODOO_*)")
    else:
        client = odoo.OdooClient(creds)
        try:
            client.connect()
            check("Odoo authenticated", True, f"uid {client.uid} @ {creds.url} ({creds.db})")
        except Exception as exc:  # noqa: BLE001
            check("Odoo authenticated", False, str(exc).splitlines()[0])

    out("\n" + ("[green]All good.[/green]" if ok else "[red]Some checks failed — see above.[/red]"))
    return 0 if ok else 1


def cmd_fetch(args: argparse.Namespace) -> int:
    ref = _ref(args)
    out(f"Fetching {ref.slug}#{ref.number} …")
    bundle = github.fetch_pr(ref)
    out(f"  diff: {len(bundle['diff'])} bytes · task ids: {bundle['task_ids'] or 'none'}")

    detail, total = github.fetch_commits_detail(ref, bundle["meta"])
    bundle["commits_detail"] = detail
    # Full ordered SHA list (oldest-first, as `gh pr view` returns) — incremental uses this to
    # locate the last-reviewed commit even on PRs larger than the per-commit-diff cap.
    bundle["commit_shas"] = [c.get("oid", "") for c in bundle["meta"].get("commits", [])]
    if total > len(detail):
        out(f"  [yellow]⚠ PR has {total} commits; per-commit/per-task strategies cover the first {len(detail)}.[/yellow]")

    tasks, warn = odoo.enrich_tasks(bundle["task_ids"], odoo_creds(task=True))
    if warn:
        out(f"  [yellow]⚠ {warn}[/yellow]")
    bundle["tasks"] = [t.to_dict() for t in tasks]
    for t in tasks:
        if not t.error:
            out(f"  task {t.id}: {t.name} ({len(t.notes)} notes)")

    base = bundle["meta"].get("baseRefName")
    skill_text = skill.fetch_skill(base)
    bundle["skill_chars"] = len(skill_text)
    out(f"  skill text: {len(skill_text)} chars (base {base})"
        + ("" if skill_text else " [yellow]— fetch failed, will use fallback[/yellow]"))

    files = bundle["meta"].get("files", [])
    head_sha = bundle["meta"].get("headRefOid", "")

    if args.no_tests:
        bundle["module_tests"] = {}
    else:
        # Base branch: always in the base repo (a fork's head commit is not) and carries
        # the existing tests we want as API reference.
        tests = context.fetch_module_tests(ref, files, base)
        bundle["module_tests"] = tests
        out(f"  module tests: {len(tests)} file(s)" + (f" — {', '.join(tests)}" if tests else ""))

    if args.no_context:
        bundle["full_files"] = {}
        bundle["referencing_files"] = {}
    else:
        full = context.fetch_full_files(ref, files, head_sha)
        bundle["full_files"] = full
        out(f"  full changed files: {len(full)}")
        exclude = set(full) | set(bundle.get("module_tests", {}))
        refs = context.fetch_referencing_files(ref, bundle["diff"], base, exclude)
        bundle["referencing_files"] = refs
        out(f"  target-branch references: {len(refs)}" + (f" — {', '.join(refs)}" if refs else ""))

    save_json(_bundle_path(ref), bundle)
    out(f"[green]Wrote[/green] {_bundle_path(ref)}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    ref = _ref(args)
    bpath = _bundle_path(ref)
    if not bpath.is_file():
        out(f"[red]No bundle for {ref.slug}#{ref.number} — run `fetch` first.[/red]")
        return 1
    bundle = load_json(bpath)

    # Human-first gate: a human must have reviewed/commented before the AI piles on.
    humans = github.human_reviewers(bundle, MARKER)
    if humans:
        out(f"Human review detected: {', '.join(humans)} — AI will add on top.")
    elif args.force:
        out("[yellow]⚠ No human review yet — proceeding because --force was given.[/yellow]")
    else:
        out("[yellow]No human review on this PR yet.[/yellow] A human should review first "
            "(the AI is meant to build on human comments). Re-run with --force to override.")
        return 1

    # Incremental: review only commits added since the last review of this PR.
    focus_diff = None
    if args.incremental:
        focus_diff = _incremental_focus(ref, bundle)
        if focus_diff == "":          # up to date, nothing new
            out("No new commits since the last review — nothing to do.")
            return 0

    skill_text = skill.fetch_skill(bundle["meta"].get("baseRefName"))
    out(f"Reviewing {ref.slug}#{ref.number} with {args.model} "
        f"(strategy: {'incremental' if focus_diff else args.strategy}, "
        f"verify: {'off' if args.no_verify else 'on'}) …")

    raw, meta = engine.review(
        bundle, skill_text,
        strategy=args.strategy, model=args.model, workers=args.workers,
        verify=not args.no_verify, focus_diff=focus_diff, context=args.context, log=out,
    )
    if meta["permission_denials"]:
        out(f"  [yellow]⚠ sandbox denials recorded: {meta['permission_denials']}[/yellow]")

    index = parse_diff(bundle["diff"])
    review = Review(summary=raw.get("summary", ""), model=args.model)
    for item in raw.get("findings", []):
        f = Finding.from_dict(item)
        f.anchored = index.valid(f.path, f.line, f.side)
        review.findings.append(f)

    _apply_suppression(review, bundle.get("inline_comments", []), args.min_confidence)

    rjson, rmd = _review_paths(ref)
    save_json(rjson, review.to_dict())
    rmd.write_text(_render_md(ref, review))
    _save_state(ref, bundle)

    cost = meta.get("cost_usd")
    out(f"[green]Wrote[/green] {rjson} and {rmd}")
    out(f"  {len(review.findings)} findings — {len(review.inline)} inline, {len(review.general)} general"
        f", {len(review.suppressed)} suppressed · {meta.get('agents', 1)} agent(s)"
        + (f" · ${cost:.4f}" if cost else ""))
    out("  Review/edit review.json, then `push`.")
    return 0


def _incremental_focus(ref: PRRef, bundle: dict) -> str | None:
    """Combined diff of commits newer than the last-reviewed SHA (relies on gh's oldest-first
    commit order). Returns None to fall back to a full review, "" if there is nothing new."""
    spath = _state_path(ref)
    last = load_json(spath).get("last_reviewed_sha") if spath.is_file() else None
    detail = bundle.get("commits_detail", [])
    shas = bundle.get("commit_shas") or [c["sha"] for c in detail]
    if not last:
        out("[yellow]⚠ No prior review of this PR — doing a full review.[/yellow]")
        return None
    if last not in shas:
        out("[yellow]⚠ Last-reviewed commit not in history (rebase/force-push?) — full review.[/yellow]")
        return None
    new_shas = shas[shas.index(last) + 1:]
    if not new_shas:
        return ""
    out(f"Incremental: {len(new_shas)} new commit(s) since {last[:8]}.")
    # Prefer the diff already in the bundle; fetch any new commit beyond the per-commit cap.
    have = {c["sha"]: c.get("diff", "") for c in detail}
    diffs = [have.get(sha) or github.fetch_commit_diff(ref.slug, sha) for sha in new_shas]
    return "\n".join(d for d in diffs if d)


def _save_state(ref: PRRef, bundle: dict) -> None:
    save_json(_state_path(ref), {
        "last_reviewed_sha": bundle["meta"].get("headRefOid", ""),
        "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


def cmd_push(args: argparse.Namespace) -> int:
    ref = _ref(args)
    rjson, _ = _review_paths(ref)
    bpath = _bundle_path(ref)
    if not rjson.is_file() or not bpath.is_file():
        out(f"[red]Missing bundle/review for {ref.slug}#{ref.number} — run fetch + review first.[/red]")
        return 1
    review = Review.from_dict(load_json(rjson))
    bundle = load_json(bpath)
    commit_id = bundle["meta"].get("headRefOid", "")

    comments = [
        {"path": f.path, "line": f.line, "side": f.side, "body": f.comment_body()}
        for f in review.inline
    ]
    body = _summary_body(review)
    event = args.submit  # None -> draft/pending

    if args.separate_summary:
        summary_body, review_body = body, f"🤖 Draft inline review by `ai_review`. {MARKER}"
    else:
        summary_body, review_body = None, body

    out(f"Prepared draft review for {ref.slug}#{ref.number}: "
        f"{len(comments)} inline comment(s), {len(review.general)} general.")

    if args.dry_run:
        out("\n[bold]--- review payload (dry run) ---[/bold]")
        out(github.render_payload(commit_id, review_body, comments, event))
        if args.separate_summary:
            out("\n[bold]--- separate summary comment (dry run) ---[/bold]")
            out(summary_body)
        return 0

    # Idempotency: one pending review per user per PR.
    try:
        login = github.auth_login()
        if event is None and github.pending_review_exists(ref, login):
            out("[yellow]⚠ You already have a pending review on this PR. "
                "Submit/dismiss it in GitHub before posting a new draft.[/yellow]")
            return 1
    except Exception as exc:  # noqa: BLE001 — non-fatal preflight
        out(f"[yellow]⚠ Could not check existing reviews: {exc}[/yellow]")

    if not args.yes:
        where = "a DRAFT (pending) review" if event is None else f"a submitted review ({event})"
        resp = input(f"Post {where} to {ref.slug}#{ref.number}? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            out("Aborted.")
            return 1

    result = github.post_review(ref, commit_id, review_body, comments, event=event)
    out(f"[green]Posted[/green] review id {result.get('id')} (state: {result.get('state')})")
    if args.separate_summary:
        c = github.post_issue_comment(ref, summary_body)
        out(f"[green]Posted[/green] summary comment {c.get('html_url')}")
    if event is None:
        out("  It's a DRAFT — open the PR in GitHub to read and submit it.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    rc = cmd_fetch(args)
    return rc or cmd_review(args)


def cmd_process(args: argparse.Namespace) -> int:
    """One shot: fetch → review → push, from just the PR link. push still defaults to a draft
    and asks before posting (pass --yes to run fully unattended)."""
    rc = cmd_fetch(args)
    if rc:
        return rc
    rc = cmd_review(args)
    if rc:
        return rc
    return cmd_push(args)


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ai_review", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_pr(sp):
        sp.add_argument("pr", help="PR URL, owner/repo#123, or a number (with -R)")
        sp.add_argument("-R", "--repo", help="owner/repo (when pr is a bare number)")

    def add_fetch_flags(sp):
        sp.add_argument("--no-tests", action="store_true",
                        help="skip fetching the touched modules' test files")
        sp.add_argument("--no-context", action="store_true",
                        help="skip fetching full changed files + target-branch references")

    def add_review_flags(sp):
        sp.add_argument("--model", default=claude.DEFAULT_MODEL,
                        help=f"claude model (default: {claude.DEFAULT_MODEL})")
        sp.add_argument("--strategy", choices=["whole", "per-task", "per-commit"], default="whole",
                        help="whole PR (default), or split by [task_id]/commit + a holistic pass")
        sp.add_argument("--workers", type=int, default=4,
                        help="parallel per-unit agents (grouped strategies)")
        sp.add_argument("--no-verify", action="store_true",
                        help="skip the skeptical critic pass that refutes/keeps findings")
        sp.add_argument("--min-confidence", type=float, default=0.6,
                        help="suppress findings below this confidence (0-1, default 0.6)")
        sp.add_argument("--incremental", action="store_true",
                        help="review only commits added since the last review of this PR")
        sp.add_argument("--context", choices=["pstech", "rnd"], default="pstech",
                        help="recalibrate the guidelines for PS Tech customizations (default) or "
                             "Odoo core R&D contributions")
        sp.add_argument("--force", action="store_true",
                        help="review even if no human has reviewed the PR yet")

    def add_push_flags(sp):
        sp.add_argument("--dry-run", action="store_true", help="print the payload, post nothing")
        sp.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
        sp.add_argument("--separate-summary", action="store_true",
                        help="post the summary as its own top-level comment")
        sp.add_argument("--submit", choices=["COMMENT", "APPROVE", "REQUEST_CHANGES"], default=None,
                        help="submit immediately instead of leaving a draft (default: draft)")

    sub.add_parser("doctor", help="check gh / claude / Odoo setup").set_defaults(func=cmd_doctor)

    sp = sub.add_parser("fetch", help="pull PR + task context into bundle.json")
    add_pr(sp); add_fetch_flags(sp); sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("review", help="run Claude and write review.json/.md")
    add_pr(sp); add_review_flags(sp); sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("push", help="post a DRAFT review to the PR")
    add_pr(sp); add_push_flags(sp); sp.set_defaults(func=cmd_push)

    sp = sub.add_parser("run", help="fetch + review (stops before push)")
    add_pr(sp); add_fetch_flags(sp); add_review_flags(sp); sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("process", help="one shot: fetch + review + push, from just the PR link")
    add_pr(sp); add_fetch_flags(sp); add_review_flags(sp); add_push_flags(sp)
    sp.set_defaults(func=cmd_process)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, github.GhError, claude.ClaudeError) as exc:
        out(f"[red]Error:[/red] {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
