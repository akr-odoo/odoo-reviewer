#!/usr/bin/env python3
"""
Unified-diff parsing and comment-anchor validation.

GitHub rejects an entire review if any inline comment points at a line that is not part
of the PR diff. So before posting we build, from the diff itself, the exact set of
positions a comment may target:

    RIGHT side -> added ('+') and context (' ') lines, numbered in the new file
    LEFT  side -> removed ('-') and context (' ') lines, numbered in the old file

`DiffIndex.valid(path, line, side)` is the single source of truth used by review() to
decide whether a finding becomes an inline comment or falls back to the summary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# @@ -oldStart[,oldLen] +newStart[,newLen] @@  (len defaults to 1 when omitted)
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip_prefix(target: str) -> str | None:
    """Turn a '--- a/foo' / '+++ b/foo' marker into 'foo', or None for /dev/null."""
    if target == "/dev/null":
        return None
    return target[2:] if target[:2] in ("a/", "b/") else target


@dataclass
class DiffIndex:
    # path -> set of valid line numbers, per side
    right: dict[str, set[int]] = field(default_factory=dict)
    left: dict[str, set[int]] = field(default_factory=dict)

    def valid(self, path: str, line: int | None, side: str = "RIGHT") -> bool:
        if not path or not line:
            return False
        table = self.right if side.upper() != "LEFT" else self.left
        return line in table.get(path, set())

    @property
    def paths(self) -> set[str]:
        return set(self.right) | set(self.left)


def parse_diff(diff_text: str) -> DiffIndex:
    """Build the set of anchorable (path, line, side) positions from a unified diff.

    Correctness note: diff *body* lines are prefixed with one char (' ', '+', '-'), so a
    removed source line like ``-- deprecated`` arrives as ``--- deprecated`` and an added
    ``++ x`` as ``+++ x``. We therefore only interpret ``---``/``+++``/``diff --git`` as file
    headers when we are NOT inside a hunk, and consume exactly the line counts declared by the
    ``@@`` header so body text can never be mistaken for a header.
    """
    index = DiffIndex()
    path: str | None = None       # effective path for anchoring (new, or old for deletions)
    old_path: str | None = None
    rem_old = rem_new = 0          # lines left to consume in the current hunk
    old_ln = new_ln = 0
    in_hunk = False

    for line in diff_text.splitlines():
        if in_hunk and (rem_old > 0 or rem_new > 0):
            c = line[:1]
            if c == "+":
                if path:
                    index.right[path].add(new_ln)
                new_ln += 1
                rem_new -= 1
            elif c == "-":
                if path:
                    index.left[path].add(old_ln)
                old_ln += 1
                rem_old -= 1
            elif c == " ":
                if path:
                    index.right[path].add(new_ln)
                    index.left[path].add(old_ln)
                new_ln += 1
                old_ln += 1
                rem_old -= 1
                rem_new -= 1
            elif c == "\\":
                pass  # "\ No newline at end of file" — annotates the prior line, no count
            else:
                # Not a valid body line (e.g. a new "diff --git"/blank): the hunk ended early
                # (malformed counts). Stop consuming and reprocess this line as a header.
                in_hunk = False
            if c in ("+", "-", " ", "\\"):
                if rem_old <= 0 and rem_new <= 0:
                    in_hunk = False
                continue

        # Header territory (between hunks / files).
        if line.startswith("diff --git"):
            path = old_path = None
        elif line.startswith("--- "):
            old_path = _strip_prefix(line[4:].strip())
        elif line.startswith("+++ "):
            new_path = _strip_prefix(line[4:].strip())
            path = new_path or old_path       # deletions keep the old path for LEFT anchoring
            if path:
                index.right.setdefault(path, set())
                index.left.setdefault(path, set())
        elif m := _HUNK_RE.match(line):
            old_ln, rem_old = int(m.group(1)), int(m.group(2) or 1)
            new_ln, rem_new = int(m.group(3)), int(m.group(4) or 1)
            in_hunk = True

    return index
