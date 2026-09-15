#!/usr/bin/env python3
"""
Invoke the `claude` CLI headless, fully sandboxed, and parse its review JSON.

The guarantee the user asked for — the AI gets NO access to GitHub or MCP — is enforced here:

  --strict-mcp-config --mcp-config '{"mcpServers":{}}'   -> zero MCP servers (no GitHub MCP)
  --disallowed-tools <all built-ins>                     -> no shell / filesystem / web
  (run from an empty temp cwd, no --add-dir)             -> no repo context to act on

GitHub is reachable only through Bash(`gh`) or a GH MCP server; both are blocked. We also
assert the envelope's `permission_denials` is empty, so an attempted tool use would surface
rather than pass silently.

Default model is the latest Opus; override with --model. (A future multi-agent setup can
fan several models/roles through run_review.)
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile

DEFAULT_MODEL = "opus"  # alias -> latest Opus (claude-opus-4-8)

# Built-in tools to explicitly deny. Belt-and-suspenders on top of the empty MCP config.
_DISALLOWED = [
    "Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "NotebookEdit",
    "WebFetch", "WebSearch", "Glob", "Grep", "Task", "TodoWrite",
]

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


class ClaudeError(RuntimeError):
    pass


def _extract_json_object(text: str) -> dict:
    """Pull the review JSON object out of the model's reply, tolerating a stray fence
    or surrounding prose."""
    text = text.strip()
    # 1. The contract asks for pure JSON — try that first (cheapest, no greedy-match risk).
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 2. A single ```json … ``` fenced block.
    if m := _JSON_FENCE_RE.search(text):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 3. Last resort: the outermost {...} span.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ClaudeError("Could not find a JSON object in Claude's response.")


def run_review(system_prompt: str, user_content: str, model: str = DEFAULT_MODEL) -> tuple[dict, dict]:
    """Return (review_dict, meta). review_dict matches the OUTPUT_CONTRACT schema.
    meta carries cost/usage/denials for logging."""
    with tempfile.TemporaryDirectory() as empty_cwd:
        cmd = [
            "claude", "-p",
            "--output-format", "json",
            "--model", model,
            "--system-prompt", system_prompt,
            "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}',
            "--disallowed-tools", *_DISALLOWED,
        ]
        try:
            proc = subprocess.run(
                cmd, input=user_content, capture_output=True, text=True, cwd=empty_cwd
            )
        except FileNotFoundError as exc:
            raise ClaudeError("`claude` CLI not found on PATH — install Claude Code.") from exc

    if proc.returncode != 0:
        raise ClaudeError(f"claude exited {proc.returncode}:\n{proc.stderr.strip()}")

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeError(f"Unexpected claude output (not JSON):\n{proc.stdout[:500]}") from exc

    if envelope.get("is_error"):
        raise ClaudeError(f"claude reported an error: {envelope.get('result')}")

    review = _extract_json_object(envelope.get("result", ""))
    meta = {
        "model": model,
        "cost_usd": envelope.get("total_cost_usd"),
        "usage": envelope.get("usage"),
        "permission_denials": envelope.get("permission_denials", []),
    }
    return review, meta
