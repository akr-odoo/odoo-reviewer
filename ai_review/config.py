#!/usr/bin/env python3
"""
Configuration, environment loading, and the PR/Odoo reference types.

Credentials are read from the environment (never hardcoded), matching the rest of
the scripts in this repo:

    ODOO_URL        (default https://www.odoo.com)
    ODOO_DB         (default openerp)
    ODOO_USER
    ODOO_API_KEY    (Preferences > Account Security > New API Key)

Task enrichment uses the same connection by default, but each var can be overridden
with a ``TASK_<name>`` variant if R&D tasks ever move to a separate instance:

    TASK_ODOO_URL / TASK_ODOO_DB / TASK_ODOO_USER / TASK_ODOO_API_KEY

A ``.env`` file sitting next to this package (or in the current directory) is loaded
automatically so teammates can drop credentials in one file instead of editing their
shell profile.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
PACKAGE_DIR = Path(__file__).resolve().parent
WORK_DIR = PACKAGE_DIR / ".work"          # per-PR artifacts (bundle/review)
CACHE_DIR = PACKAGE_DIR / ".cache"        # cached skill text

# --------------------------------------------------------------------------- #
# Skill (odoo house rules) location
# --------------------------------------------------------------------------- #
SKILL_REPO = "odoo/odoo"
SKILL_PATH = "skills/odoo-review/SKILL.md"
SKILL_DEFAULT_REF = "20.0"

# Regex that finds a [task_id] in a commit/PR title, e.g. "[FIX] foo [12345]".
TASK_ID_RE = re.compile(r"\[(\d{3,})\]")

# Hidden HTML marker embedded in everything ai_review posts, so later runs can tell our own
# comments apart from human ones (human-first gate + dedup).
MARKER = "<!-- ai_review -->"


# --------------------------------------------------------------------------- #
# .env loading (tiny, dependency-free)
# --------------------------------------------------------------------------- #
LOADED_DOTENV: str | None = None   # path of the .env that was loaded (for `doctor`)


def load_dotenv() -> None:
    """Load the first .env found (cwd, package dir, repo root). A FILLED-IN value in .env wins
    over a shell export so the file is the source of truth; an empty entry never clobbers a real
    env var. (Previously a shell export always beat .env, which was surprising.)"""
    global LOADED_DOTENV
    seen: set = set()
    for candidate in (Path.cwd() / ".env", PACKAGE_DIR / ".env", PACKAGE_DIR.parent / ".env"):
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if not candidate.is_file():
            continue
        for raw in candidate.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if value:
                os.environ[key] = value          # filled .env value wins over the shell
            else:
                os.environ.setdefault(key, value)
        LOADED_DOTENV = str(candidate)
        return


# --------------------------------------------------------------------------- #
# Odoo credentials
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OdooCreds:
    url: str
    db: str
    user: str
    api_key: str

    @property
    def complete(self) -> bool:
        return all((self.url, self.db, self.user, self.api_key))


def odoo_creds(task: bool = False) -> OdooCreds:
    """Read Odoo creds. When ``task`` is set, TASK_* overrides win, else fall back
    to the primary ODOO_* vars so a single set of credentials just works."""
    def pick(name: str, default: str = "") -> str:
        if task:
            return os.environ.get(f"TASK_{name}") or os.environ.get(name, default)
        return os.environ.get(name, default)

    return OdooCreds(
        url=pick("ODOO_URL", "https://www.odoo.com").rstrip("/"),
        db=pick("ODOO_DB", "openerp"),
        user=pick("ODOO_USER"),
        api_key=pick("ODOO_API_KEY"),
    )


# --------------------------------------------------------------------------- #
# PR reference
# --------------------------------------------------------------------------- #
_PR_URL_RE = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<num>\d+)")
_PR_SHORT_RE = re.compile(r"(?P<owner>[^/\s]+)/(?P<repo>[^/#\s]+)#(?P<num>\d+)")


@dataclass(frozen=True)
class PRRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def work_key(self) -> str:
        return f"{self.owner}__{self.repo}__{self.number}"

    def work_dir(self) -> Path:
        return WORK_DIR / self.work_key

    @classmethod
    def parse(cls, value: str, repo: str | None = None) -> "PRRef":
        """Accept a PR URL, ``owner/repo#123``, or a bare number plus ``repo``
        (``owner/repo``, usually from -R)."""
        value = value.strip()
        if m := _PR_URL_RE.search(value):
            return cls(m["owner"], m["repo"], int(m["num"]))
        if m := _PR_SHORT_RE.fullmatch(value):
            return cls(m["owner"], m["repo"], int(m["num"]))
        if value.isdigit():
            if not repo or "/" not in repo:
                raise ValueError(
                    "A bare PR number needs -R owner/repo (or pass a full PR URL)."
                )
            owner, name = repo.split("/", 1)
            return cls(owner, name, int(value))
        raise ValueError(f"Could not parse PR reference: {value!r}")
