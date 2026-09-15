#!/usr/bin/env python3
"""
Fetch and cache the odoo-review house-rules skill (and everything it links to) from odoo/odoo.

The *script* fetches this over `gh` (never Claude), at the PR's base branch so the rules match
the version under review. The top-level `skills/odoo-review/SKILL.md` is only a dispatcher — it
points, via RELATIVE links, at three sibling skills and their guideline files:

    ../odoo-guidelines/SKILL.md       every file outside static/
    ../odoo-web-guidelines/SKILL.md   every file under static/ (JS, Owl, SCSS, hoot)
    ../odoo-security/SKILL.md         controllers, ir.access, sudo(), raw SQL, eval, RPC methods

So we breadth-first crawl every `.md` referenced within the `skills/` tree (resolving the
relative paths), SKILL dispatchers first, then their guideline files, up to a char budget.
If the fetch fails, review() falls back to the condensed methodology embedded in prompt.py.
"""
from __future__ import annotations

import base64
import json
import posixpath
import re
import subprocess
from collections import deque

from .config import SKILL_REPO, SKILL_PATH, SKILL_DEFAULT_REF, CACHE_DIR

MAX_TOTAL_CHARS = 180_000
# Any ".md" path mentioned in a backtick code span `...` or a markdown link (...). Captures
# both "../odoo-guidelines/SKILL.md" and "../odoo-guidelines/guidelines/stable.md#anchor".
_REF_RE = re.compile(r"[`(]([^`()\s]+?\.md)")


def _gh_contents(repo: str, path: str, ref: str) -> str | None:
    """Return decoded file contents via the GitHub contents API, or None if missing."""
    try:
        # NB: pass ref as a query param in the URL. Using `-f ref=` makes `gh api`
        # switch to POST, which the contents API rejects with 404.
        out = subprocess.run(
            ["gh", "api", f"repos/{repo}/contents/{path}?ref={ref}"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return None
    if out.returncode != 0:
        return None
    try:
        payload = json.loads(out.stdout)
        return base64.b64decode(payload["content"]).decode("utf-8", "replace")
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


# Bump when the crawl/format changes so stale caches from an older version are ignored.
_CACHE_VERSION = "v2"


def _cache_path(ref: str):
    return CACHE_DIR / f"skill_{_CACHE_VERSION}_{ref.replace('/', '_')}.md"


def _linked_md(text: str, from_path: str) -> list[str]:
    """Resolve the .md paths referenced in ``text`` against the file's directory, keeping
    only targets inside the skills/ tree."""
    base_dir = posixpath.dirname(from_path)
    out: list[str] = []
    for rel in _REF_RE.findall(text):
        target = posixpath.normpath(posixpath.join(base_dir, rel))
        if not target.startswith("skills/") or target in out:
            continue
        if target.rsplit("/", 1)[-1] == "AUTHORING.md":
            continue  # meta-doc about writing the skills, not review rules
        out.append(target)
    return out


def fetch_skill(ref: str | None, *, use_cache: bool = True) -> str:
    """Return the combined skill text for ``ref`` (falls back to the default branch)."""
    ref = ref or SKILL_DEFAULT_REF
    cache = _cache_path(ref)
    if use_cache and cache.is_file():
        return cache.read_text()

    root = _gh_contents(SKILL_REPO, SKILL_PATH, ref)
    if root is None and ref != SKILL_DEFAULT_REF:
        ref = SKILL_DEFAULT_REF
        root = _gh_contents(SKILL_REPO, SKILL_PATH, ref)
    if root is None:
        return ""

    # BFS so the dispatcher SKILLs (referenced directly by root) are pulled before deeper
    # guideline files, and the char budget trims the long tail rather than the core tables.
    collected: list[tuple[str, str]] = [(SKILL_PATH, root)]
    seen = {SKILL_PATH}
    total = len(root)
    queue: deque[tuple[str, str]] = deque([(SKILL_PATH, root)])
    while queue and total < MAX_TOTAL_CHARS:
        cur_path, cur_text = queue.popleft()
        for target in _linked_md(cur_text, cur_path):
            if target in seen or total >= MAX_TOTAL_CHARS:
                continue
            seen.add(target)
            text = _gh_contents(SKILL_REPO, target, ref)
            if text is None:
                continue
            collected.append((target, text))
            total += len(text)
            queue.append((target, text))

    combined = "\n\n".join(f"===== {p} =====\n{t}" for p, t in collected)[:MAX_TOTAL_CHARS]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(combined)
    return combined
