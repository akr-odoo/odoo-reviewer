#!/usr/bin/env python3
"""
Fetch the surrounding code context a reviewer needs beyond the raw diff.

Since Claude has no repo access, the SCRIPT fetches (over `gh`, at the right git ref) and
folds into the bundle:

- `fetch_module_tests`   the touched modules' `tests/` — executable docs for the expected API.
- `fetch_full_files`     the FULL post-change content of each changed source file, so the
                          reviewer sees whole functions/classes, not just hunks.
- `fetch_referencing_files`  files elsewhere on the TARGET branch that reference the symbols
                          this PR changes — the cheap way to catch cross-file / caller breakage
                          without indexing the whole repo.

Everything is budgeted (file count, per-file and total chars) so a big PR can't blow up the
prompt, and prioritized by relevance to the changed files.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
from typing import Any

from .config import PRRef

MAX_FILES = 12
MAX_TOTAL_CHARS = 80_000
MAX_FILE_CHARS = 20_000

# Budgets for the full-file and referencing-file context (kept tighter than tests).
MAX_FULL_FILES = 15
MAX_FULL_TOTAL_CHARS = 120_000
MAX_REF_FILES = 6
MAX_REF_TOTAL_CHARS = 60_000
MAX_REF_SYMBOLS = 6

# Python definitions whose callers we care about (public symbols added/changed in the diff).
_DEF_RE = re.compile(r"^\+\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
_SKIP_SUFFIXES = (".po", ".pot", ".csv", ".xml", ".svg", ".png", ".jpg", ".lock", ".json")


def _module_root(path: str) -> str | None:
    """Best-effort module root for an Odoo repo path.
    addons/sale/...           -> addons/sale
    odoo/addons/base/...      -> odoo/addons/base
    sale_subscription/...     -> sale_subscription   (enterprise: modules at repo root)
    """
    parts = path.split("/")
    if "addons" in parts:
        i = parts.index("addons")
        if i + 1 < len(parts):
            return "/".join(parts[: i + 2])
    return parts[0] if len(parts) >= 2 else None


def _changed_stems(files: list[dict[str, Any]]) -> set[str]:
    stems = set()
    for f in files:
        name = (f.get("path") or "").rsplit("/", 1)[-1]
        if name.endswith(".py"):
            stems.add(name[:-3])
    return stems


def _gh_json(args: list[str]) -> Any | None:
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True)
    except FileNotFoundError:
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def _fetch_blob(slug: str, path: str, gitref: str) -> str | None:
    """Return a file's decoded text at ``gitref`` via the contents API, or None."""
    blob = _gh_json(["api", f"repos/{slug}/contents/{path}?ref={gitref}"])
    if not isinstance(blob, dict) or "content" not in blob:
        return None
    try:
        return base64.b64decode(blob["content"]).decode("utf-8", "replace")
    except ValueError:
        return None


def fetch_module_tests(ref: PRRef, files: list[dict[str, Any]], gitref: str) -> dict[str, str]:
    """Return {repo_path: file_contents} for test files of the touched modules.

    ``gitref`` should be the PR's base branch: it is always present in the base repo
    (a fork's head commit is not), and existing tests are what we want as API reference.
    """
    roots: list[str] = []
    for f in files:
        root = _module_root(f.get("path") or "")
        if root and root not in roots:
            roots.append(root)

    stems = _changed_stems(files)
    collected: dict[str, str] = {}
    total = 0

    for root in roots:
        listing = _gh_json(
            ["api", f"repos/{ref.slug}/contents/{root}/tests?ref={gitref}"]
        )
        if not isinstance(listing, list):
            continue  # no tests dir in this module
        test_files = [
            e for e in listing
            if e.get("type") == "file"
            and e["name"].endswith(".py")
            and e["name"] != "__init__.py"
        ]
        # Relevance first: a test file whose stem overlaps a changed file stem.
        def relevance(entry: dict) -> int:
            stem = entry["name"][:-3].removeprefix("test_")
            return 0 if any(stem in s or s in stem for s in stems) else 1
        test_files.sort(key=lambda e: (relevance(e), e["name"]))

        for entry in test_files:
            if len(collected) >= MAX_FILES or total >= MAX_TOTAL_CHARS:
                return collected
            text = _fetch_blob(ref.slug, entry["path"], gitref)
            if text is None:
                continue
            text = text[:MAX_FILE_CHARS]
            collected[entry["path"]] = text
            total += len(text)

    return collected


def _reviewable_source(path: str) -> bool:
    return bool(path) and not path.endswith(_SKIP_SUFFIXES) and "/tests/" not in path


def fetch_full_files(ref: PRRef, files: list[dict[str, Any]], head_sha: str) -> dict[str, str]:
    """Full POST-CHANGE content of changed source files, so the reviewer sees whole
    functions/classes rather than isolated hunks.

    Fetched strictly at the PR head commit (the version under review). We deliberately do NOT
    fall back to the base branch: serving a pre-change version while labelling it "post-change"
    would mislead the reviewer. A file that can't be fetched at head (deleted at head, or a
    fork head unreachable via the base repo) is simply omitted."""
    if not head_sha:
        return {}
    collected: dict[str, str] = {}
    total = 0
    for f in files:
        path = f.get("path") or ""
        if not _reviewable_source(path):
            continue
        if len(collected) >= MAX_FULL_FILES or total >= MAX_FULL_TOTAL_CHARS:
            break
        text = _fetch_blob(ref.slug, path, head_sha)
        if text is None:
            continue
        collected[path] = text[:MAX_FILE_CHARS]
        total += len(collected[path])
    return collected


def _changed_symbols(diff: str) -> list[str]:
    """Names of functions/classes added or changed in the diff — their callers are what we
    want to check against the target branch."""
    seen: list[str] = []
    for line in diff.splitlines():
        m = _DEF_RE.match(line)
        if m and m.group(1) not in seen and not m.group(1).startswith("test_"):
            seen.append(m.group(1))
    return seen[:MAX_REF_SYMBOLS]


def fetch_referencing_files(ref: PRRef, diff: str, base_ref: str,
                            exclude: set[str]) -> dict[str, str]:
    """Files on the target branch that reference the PR's changed symbols (cross-file impact).

    Uses `gh search code` (which indexes the default/target branch), best-effort and budgeted;
    returns {} silently if search is unavailable or finds nothing. ``exclude`` skips files we
    already included (changed files / tests)."""
    symbols = _changed_symbols(diff)
    if not symbols:
        return {}
    collected: dict[str, str] = {}
    total = 0
    for sym in symbols:
        if len(collected) >= MAX_REF_FILES or total >= MAX_REF_TOTAL_CHARS:
            break
        hits = _gh_json(
            ["search", "code", f"{sym} repo:{ref.slug}", "--json", "path", "--limit", "5"]
        )
        if not isinstance(hits, list):
            continue
        for hit in hits:
            path = hit.get("path", "")
            if (not _reviewable_source(path) or path in exclude or path in collected):
                continue
            if len(collected) >= MAX_REF_FILES or total >= MAX_REF_TOTAL_CHARS:
                break
            text = _fetch_blob(ref.slug, path, base_ref)
            if text is None:
                continue
            text = text[:MAX_FILE_CHARS]
            collected[path] = text
            total += len(text)
    return collected
