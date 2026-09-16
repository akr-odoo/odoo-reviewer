#!/usr/bin/env python3
"""
Thin wrapper over the `gh` CLI — the ONLY component that talks to GitHub.

Claude never runs any of this; it only ever receives the text bundle these functions
produce. Everything shells out to `gh`, which carries the user's own auth, so there are
no tokens to manage here.

fetch_pr() returns the bundle dict written to bundle.json:

    {
      "ref": {"owner","repo","number"},
      "meta": {...gh pr view json...},
      "diff": "<unified diff>",
      "inline_comments": [ {path,line,user,body,...}, ... ],   # existing review comments
      "task_ids": [int, ...],
    }
"""
from __future__ import annotations

import json
import subprocess
from typing import Any

from .config import PRRef, TASK_ID_RE

# Fields pulled from `gh pr view`. headRefOid is the commit a review must anchor to.
_PR_FIELDS = [
    "number", "title", "body", "author", "url", "state", "isDraft",
    "baseRefName", "headRefName", "headRefOid",
    "additions", "deletions", "changedFiles", "files", "commits", "comments", "reviews",
]

# Odoo / common GitHub automation accounts that don't count as a human review.
_BOTS = {"robodoo", "fw-bot", "c-3po"}


class GhError(RuntimeError):
    pass


def _gh(args: list[str], *, stdin: str | None = None) -> str:
    try:
        proc = subprocess.run(
            ["gh", *args],
            input=stdin,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:  # gh not installed
        raise GhError("`gh` CLI not found on PATH — install it and run `gh auth login`.") from exc
    if proc.returncode != 0:
        raise GhError(f"gh {' '.join(args)}\n{proc.stderr.strip()}")
    return proc.stdout


def _gh_json(args: list[str], *, stdin: str | None = None) -> Any:
    return json.loads(_gh(args, stdin=stdin))


# --------------------------------------------------------------------------- #
# Identity / preflight
# --------------------------------------------------------------------------- #
def auth_login() -> str:
    """Return the authenticated gh login (also doubles as an auth check)."""
    return _gh(["api", "user", "--jq", ".login"]).strip()


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def extract_task_ids(meta: dict[str, Any]) -> list[int]:
    """Collect unique [task_id]s from the PR title + body and every commit headline/body."""
    haystacks = [meta.get("title", ""), meta.get("body", "")]
    for commit in meta.get("commits", []):
        haystacks.append(commit.get("messageHeadline", ""))
        haystacks.append(commit.get("messageBody", ""))
    ids: list[int] = []
    for text in haystacks:
        for match in TASK_ID_RE.findall(text or ""):
            value = int(match)
            if value not in ids:
                ids.append(value)
    return ids


MAX_COMMITS_DETAIL = 40  # cap API calls on very large PRs


def fetch_commits_detail(ref: PRRef, meta: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Per-commit detail for the grouped review strategies: sha, headline, its own
    [task_id]s, and its diff. Returns (detail, total_commits) so callers can warn on cap."""
    commits = meta.get("commits", [])
    detail: list[dict[str, Any]] = []
    for c in commits[:MAX_COMMITS_DETAIL]:
        sha = c.get("oid", "")
        text = f"{c.get('messageHeadline', '')}\n{c.get('messageBody', '')}"
        task_ids = [int(x) for x in dict.fromkeys(TASK_ID_RE.findall(text))]
        detail.append({
            "sha": sha,
            "headline": c.get("messageHeadline", ""),
            "task_ids": task_ids,
            "diff": fetch_commit_diff(ref.slug, sha),
        })
    return detail, len(commits)


def fetch_commit_diff(slug: str, sha: str) -> str:
    """Reconstruct a unified diff for a single commit from the commits API `files[].patch`.
    Fork-PR commits are reachable through the base repo, so `slug` is the base repo."""
    data = _gh_json(["api", f"repos/{slug}/commits/{sha}"])
    chunks = []
    for f in data.get("files", []):
        patch = f.get("patch")
        if not patch:
            continue  # binary / too-large files have no patch
        path = f.get("filename", "")
        prev = f.get("previous_filename", path)
        chunks.append(f"diff --git a/{prev} b/{path}\n--- a/{prev}\n+++ b/{path}\n{patch}")
    return "\n".join(chunks)


def fetch_pr(ref: PRRef) -> dict[str, Any]:
    num = str(ref.number)
    meta = _gh_json(["pr", "view", num, "-R", ref.slug, "--json", ",".join(_PR_FIELDS)])
    diff = _gh(["pr", "diff", num, "-R", ref.slug])
    # Inline review comments live on the pulls API, not in `gh pr view`'s issue comments.
    raw_comments = _gh_json(
        ["api", f"repos/{ref.slug}/pulls/{num}/comments", "--paginate"]
    )
    inline_comments = [
        {
            "path": c.get("path"),
            "line": c.get("line") or c.get("original_line"),
            "side": c.get("side", "RIGHT"),
            "user": (c.get("user") or {}).get("login"),
            "body": c.get("body", ""),
        }
        for c in raw_comments
    ]
    # Draft (pending) review comments: GitHub omits these from pulls/N/comments until the review
    # is submitted, but the review's author can still read them. Include them so the human-first
    # gate counts the reviewer's own drafts and the AI builds on (and doesn't repeat) them.
    for rev in _gh_json(["api", f"repos/{ref.slug}/pulls/{num}/reviews", "--paginate"]) or []:
        if rev.get("state") != "PENDING":
            continue
        drafts = _gh_json(["api", f"repos/{ref.slug}/pulls/{num}/reviews/{rev['id']}/comments"]) or []
        for c in drafts:
            inline_comments.append({
                "path": c.get("path"),
                "line": c.get("line") or c.get("original_line"),
                "side": c.get("side", "RIGHT"),
                "user": (c.get("user") or {}).get("login"),
                "body": c.get("body", ""),
                "draft": True,
            })
    return {
        "ref": {"owner": ref.owner, "repo": ref.repo, "number": ref.number},
        "meta": meta,
        "diff": diff,
        "inline_comments": inline_comments,
        "task_ids": extract_task_ids(meta),
    }


# --------------------------------------------------------------------------- #
# Post
# --------------------------------------------------------------------------- #
def _is_bot(login: str) -> bool:
    low = (login or "").lower()
    return not low or low.endswith("bot") or low.endswith("[bot]") or low in _BOTS


def human_reviewers(bundle: dict[str, Any], marker: str) -> list[str]:
    """Logins of humans who have already reviewed/commented on the PR, excluding the PR
    author, bots, and anything ai_review itself posted (identified by ``marker``). This is
    the signal the human-first gate checks before letting the AI review."""
    meta = bundle.get("meta", {})
    author = (meta.get("author") or {}).get("login", "")
    logins: set[str] = set()

    def consider(login: str, body: str) -> None:
        if login and login != author and not _is_bot(login) and marker not in (body or ""):
            logins.add(login)

    for r in meta.get("reviews", []):
        if r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"):
            consider((r.get("author") or {}).get("login", ""), r.get("body", ""))
    for c in meta.get("comments", []):
        consider((c.get("author") or {}).get("login", ""), c.get("body", ""))
    for c in bundle.get("inline_comments", []):
        consider(c.get("user", ""), c.get("body", ""))
    return sorted(logins)


def pending_review(ref: PRRef, login: str) -> dict[str, Any] | None:
    """The current user's existing PENDING (draft) review on this PR, or None. GitHub allows
    only one pending review per user, so the AI appends to this one instead of creating a second."""
    reviews = _gh_json(["api", f"repos/{ref.slug}/pulls/{ref.number}/reviews", "--paginate"]) or []
    for r in reviews:
        if r.get("state") == "PENDING" and (r.get("user") or {}).get("login") == login:
            return r
    return None


def add_comment_to_pending_review(review_node_id: str, path: str, line: int, side: str, body: str) -> None:
    """Append one inline comment to an existing pending review (GraphQL), so the human's draft
    and the AI's comments end up in a single review the human submits."""
    mutation = (
        "mutation($id:ID!,$path:String!,$line:Int!,$side:DiffSide!,$body:String!){"
        "addPullRequestReviewThread(input:{pullRequestReviewId:$id,path:$path,"
        "line:$line,side:$side,body:$body}){thread{id}}}"
    )
    _gh(["api", "graphql", "-f", f"query={mutation}", "-f", f"id={review_node_id}",
         "-f", f"path={path}", "-F", f"line={line}", "-f", f"side={side}", "-f", f"body={body}"])


def post_review(
    ref: PRRef,
    commit_id: str,
    body: str,
    comments: list[dict[str, Any]],
    event: str | None = None,
) -> dict[str, Any]:
    """Create a PR review. event=None -> a DRAFT/pending review (not submitted)."""
    payload: dict[str, Any] = {"commit_id": commit_id, "body": body, "comments": comments}
    if event:
        payload["event"] = event
    return _gh_json(
        ["api", "--method", "POST", f"repos/{ref.slug}/pulls/{ref.number}/reviews", "--input", "-"],
        stdin=json.dumps(payload),
    )


def post_issue_comment(ref: PRRef, body: str) -> dict[str, Any]:
    return _gh_json(
        ["api", "--method", "POST", f"repos/{ref.slug}/issues/{ref.number}/comments", "--input", "-"],
        stdin=json.dumps({"body": body}),
    )


def render_payload(commit_id: str, body: str, comments: list[dict[str, Any]], event: str | None) -> str:
    """Human-readable dump of what post_review would send (for --dry-run)."""
    payload = {"commit_id": commit_id, "event": event or "PENDING (draft)", "body": body, "comments": comments}
    return json.dumps(payload, indent=2, ensure_ascii=False)
