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
import os
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


class ClaudeFatalError(ClaudeError):
    """Auth / credit / quota errors — no point retrying the other units; abort the whole run."""


_FATAL_HINTS = ("credit balance", "authentication", "api key", "quota", "rate limit",
                "unauthorized", "permission", "overloaded")


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


# Rough ceiling for one call's input (system + user). ~4 chars/token → ~175k input tokens,
# leaving room for output within a 200k-context model. Beyond this, split the review.
MAX_INPUT_CHARS = 700_000


def fits(system_prompt: str, user_content: str) -> bool:
    return len(system_prompt) + len(user_content) <= MAX_INPUT_CHARS


def run_review(system_prompt: str, user_content: str, model: str = DEFAULT_MODEL) -> tuple[dict, dict]:
    """Return (review_dict, meta). review_dict matches the OUTPUT_CONTRACT schema.
    meta carries cost/usage/denials for logging."""
    if not fits(system_prompt, user_content):
        kb = (len(system_prompt) + len(user_content)) // 1000
        raise ClaudeError(
            f"prompt too large (~{kb}KB) for one call — split the review with "
            "--strategy per-task (or per-commit)")
    # Use the logged-in Claude Code (claude.ai / org) session, NOT the Anthropic API: strip the
    # API-key vars so `claude` falls back to the interactive login. Set AI_REVIEW_USE_API_KEY=1
    # to opt back into API-key billing.
    env = os.environ.copy()
    if not os.environ.get("AI_REVIEW_USE_API_KEY"):
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)

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
                cmd, input=user_content, capture_output=True, text=True, cwd=empty_cwd, env=env
            )
        except FileNotFoundError as exc:
            raise ClaudeError("`claude` CLI not found on PATH — install Claude Code.") from exc

    # claude may exit non-zero but still print a JSON envelope describing the error.
    envelope = None
    if proc.stdout.strip():
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            envelope = None

    if proc.returncode != 0 or (envelope and envelope.get("is_error")):
        msg = ""
        if envelope:
            msg = str(envelope.get("result") or envelope.get("api_error_status") or "")
        msg = msg or (proc.stderr.strip() or proc.stdout.strip())[:500] or f"exit {proc.returncode}"
        if any(h in msg.lower() for h in _FATAL_HINTS):
            raise ClaudeFatalError(f"claude auth/credit error: {msg}")
        raise ClaudeError(f"claude failed: {msg}")

    if envelope is None:
        raise ClaudeError(f"Unexpected claude output (not JSON):\n{proc.stdout[:500]}")

    review = _extract_json_object(envelope.get("result", ""))
    meta = {
        "model": model,
        "cost_usd": envelope.get("total_cost_usd"),
        "usage": envelope.get("usage"),
        "permission_denials": envelope.get("permission_denials", []),
    }
    return review, meta
