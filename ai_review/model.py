#!/usr/bin/env python3
"""
Typed data structures shared across the pipeline, plus JSON (de)serialization.

The two artifacts written to a PR's work dir are plain JSON:

    bundle.json  -> everything fetch() collected (dict; schema documented in github.py)
    review.json  -> a Review (summary + findings), hand-editable before push

Finding and Task are dataclasses because they are structured, validated, and round-tripped.
The bundle stays a dict: it is mostly pass-through JSON from `gh` and gains nothing from
a rigid type.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .config import MARKER

SIDES = ("RIGHT", "LEFT")
SEVERITIES = ("blocker", "major", "minor", "nit")


# --------------------------------------------------------------------------- #
# Odoo task
# --------------------------------------------------------------------------- #
@dataclass
class TaskNote:
    author: str
    date: str
    body: str                      # plain text (HTML already stripped)


@dataclass
class Task:
    id: int
    name: str = ""
    description: str = ""          # plain text
    stage: str = ""
    project: str = ""
    assignees: list[str] = field(default_factory=list)
    notes: list[TaskNote] = field(default_factory=list)
    error: str = ""                # populated when the task could not be read

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        notes = [TaskNote(**n) for n in d.get("notes", [])]
        return cls(**{**d, "notes": notes})


# --------------------------------------------------------------------------- #
# Review finding
# --------------------------------------------------------------------------- #
@dataclass
class Finding:
    path: str
    line: int | None
    side: str = "RIGHT"
    severity: str = "minor"
    category: str = ""
    title: str = ""
    body: str = ""
    suggestion: str = ""           # optional replacement code -> GitHub ```suggestion
    confidence: float = 1.0        # the FINDER's self-rated confidence (0-1)
    anchored: bool = False         # set by review(): does (path,line,side) hit the diff?
    verdict: str = ""              # verification pass: confirmed | refuted | uncertain
    verify_confidence: float = 0.0  # the CRITIC's confidence in its verdict (separate axis)
    verify_reason: str = ""        # why the verifier reached that verdict
    suppressed: bool = False       # excluded from posting (refuted / low-confidence / duplicate)
    suppressed_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Finding":
        def s(key: str, default: str = "") -> str:      # coerce null/missing -> str
            val = d.get(key)
            return default if val is None else str(val)

        line = d.get("line")
        try:
            line = int(line) if line not in (None, "") else None
        except (TypeError, ValueError):
            line = None
        def conf(key: str, default: float) -> float:
            try:
                v = float(d[key]) if d.get(key) is not None else default
            except (TypeError, ValueError):
                v = default
            return min(1.0, max(0.0, v))

        confidence = conf("confidence", 1.0)
        verify_confidence = conf("verify_confidence", 0.0)
        side = s("side", "RIGHT").upper()
        return cls(
            path=s("path") or s("file"),                # tolerate a "file" alias
            line=line,
            side=side if side in SIDES else "RIGHT",
            severity=s("severity", "minor"),
            category=s("category"),
            title=s("title"),
            body=s("body"),
            suggestion=s("suggestion"),
            confidence=confidence,
            anchored=bool(d.get("anchored", False)),
            verdict=s("verdict"),
            verify_confidence=verify_confidence,
            verify_reason=s("verify_reason"),
            suppressed=bool(d.get("suppressed", False)),
            suppressed_reason=s("suppressed_reason"),
        )

    def comment_body(self) -> str:
        """Body for a GitHub inline comment, with a suggestion block if present. Carries the
        hidden MARKER so a later run recognizes its own (submitted) comments."""
        header = f"**[{self.severity}] {self.title}**" if self.title else f"**[{self.severity}]**"
        parts = [header, "", self.body]
        if self.suggestion.strip():
            parts += ["", "```suggestion", self.suggestion.rstrip("\n"), "```"]
        parts.append(f"\n{MARKER}")
        return "\n".join(parts).strip()


# --------------------------------------------------------------------------- #
# Review
# --------------------------------------------------------------------------- #
@dataclass
class Review:
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "model": self.model,
            "findings": [f.to_dict() for f in self.findings],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Review":
        return cls(
            summary=d.get("summary", ""),
            model=d.get("model", ""),
            findings=[Finding.from_dict(f) for f in d.get("findings", [])],
        )

    @property
    def postable(self) -> list[Finding]:
        return [f for f in self.findings if not f.suppressed]

    @property
    def inline(self) -> list[Finding]:
        return [f for f in self.postable if f.anchored and f.line]

    @property
    def general(self) -> list[Finding]:
        return [f for f in self.postable if not (f.anchored and f.line)]

    @property
    def suppressed(self) -> list[Finding]:
        return [f for f in self.findings if f.suppressed]


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #
def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())
