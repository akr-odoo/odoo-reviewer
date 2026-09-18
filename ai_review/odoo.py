#!/usr/bin/env python3
"""
Odoo task enrichment via XML-RPC.

Given the [task_id]s found in commit/PR titles, read each project.task's description and
its chatter (log notes / comments) to give the reviewer the *intent* behind the change,
not just the diff. This reuses Odoo's own data model directly — the native chatter
(message_ids -> mail.message) is read in one batched call rather than scraped — and the
exact connect/auth pattern used by databases_report.py in this repo.

Enrichment is best-effort: any failure (bad creds, model/record access) is captured on
the Task and never blocks the review.
"""
from __future__ import annotations

import base64
import ssl
import xmlrpc.client
from html.parser import HTMLParser

from .config import OdooCreds
from .model import Task, TaskNote

# www.odoo.com presents a cert chain the stdlib occasionally rejects; the existing
# scripts in this repo use the same relaxation.
ssl._create_default_https_context = ssl._create_unverified_context

TASK_FIELDS = ["name", "description", "stage_id", "project_id", "user_ids", "message_ids"]
MESSAGE_FIELDS = ["author_id", "date", "body", "message_type"]
MAX_NOTES_PER_TASK = 40

ATTACHMENT_FIELDS = ["name", "mimetype", "file_size", "type", "res_id", "url"]
MAX_ATTACHMENTS_PER_TASK = 20
MAX_ATTACHMENT_TEXT = 20_000      # per text attachment
MAX_ATTACHMENT_TOTAL = 40_000     # total inlined attachment text per task


def _is_text_mime(mime: str) -> bool:
    mime = (mime or "").lower()
    return mime.startswith("text/") or mime in (
        "application/json", "application/xml", "application/javascript",
        "application/x-python", "application/x-sql", "application/csv",
    )


class _TextExtractor(HTMLParser):
    """Collapse Odoo HTML (descriptions, chatter) into readable plain text."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in ("br", "p", "div", "li", "tr"):
            self._parts.append("\n")
        if tag == "li":
            self._parts.append("- ")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        lines = [ln.strip() for ln in "".join(self._parts).splitlines()]
        return "\n".join(ln for ln in lines if ln).strip()


def html_to_text(value: object) -> str:
    if not value or value in ("False", False):
        return ""
    parser = _TextExtractor()
    parser.feed(str(value))
    return parser.text()


class OdooClient:
    def __init__(self, creds: OdooCreds) -> None:
        self.creds = creds
        self.uid = 0
        self._models: xmlrpc.client.ServerProxy | None = None

    def connect(self) -> None:
        common = xmlrpc.client.ServerProxy(f"{self.creds.url}/xmlrpc/2/common")
        self.uid = common.authenticate(self.creds.db, self.creds.user, self.creds.api_key, {})
        if not self.uid:
            raise PermissionError("Odoo authentication failed (check ODOO_USER / ODOO_API_KEY).")
        self._models = xmlrpc.client.ServerProxy(f"{self.creds.url}/xmlrpc/2/object")

    def _call(self, model: str, method: str, *args: object, **kw: object) -> object:
        assert self._models is not None, "connect() first"
        return self._models.execute_kw(
            self.creds.db, self.uid, self.creds.api_key, model, method, list(args), kw
        )

    def fetch_tasks(self, task_ids: list[int]) -> list[Task]:
        """Read tasks + their chatter notes. Missing/forbidden ids come back with .error."""
        if not task_ids:
            return []
        records = self._call("project.task", "read", task_ids, fields=TASK_FIELDS)
        found = {r["id"]: r for r in records}

        # Batch-read every referenced message in one go, then group per task.
        all_msg_ids = [mid for r in records for mid in r.get("message_ids", [])]
        messages: dict[int, dict] = {}
        if all_msg_ids:
            for m in self._call("mail.message", "read", all_msg_ids, fields=MESSAGE_FIELDS):
                messages[m["id"]] = m

        # Resolve assignee names (user_ids is many2many -> bare ids) in one batch.
        all_user_ids = [uid for r in records for uid in (r.get("user_ids") or [])]
        user_names: dict[int, str] = {}
        if all_user_ids:
            for u in self._call("res.users", "read", list(set(all_user_ids)), fields=["name"]):
                user_names[u["id"]] = u.get("name", "")

        attachments = self._fetch_attachments(task_ids)

        tasks: list[Task] = []
        for tid in task_ids:
            rec = found.get(tid)
            if not rec:
                tasks.append(Task(id=tid, error="task not found or not accessible"))
                continue
            notes: list[TaskNote] = []
            for mid in rec.get("message_ids", []):
                m = messages.get(mid)
                if not m or m.get("message_type") == "notification":
                    continue
                body = html_to_text(m.get("body"))
                if not body:
                    continue
                author = (m.get("author_id") or [None, ""])[1] if m.get("author_id") else ""
                notes.append(TaskNote(author=author, date=str(m.get("date") or ""), body=body))
            tasks.append(
                Task(
                    id=tid,
                    name=rec.get("name", ""),
                    description=html_to_text(rec.get("description")),
                    stage=(rec.get("stage_id") or [None, ""])[1] if rec.get("stage_id") else "",
                    project=(rec.get("project_id") or [None, ""])[1] if rec.get("project_id") else "",
                    assignees=[user_names.get(uid, str(uid)) for uid in (rec.get("user_ids") or [])],
                    notes=notes[-MAX_NOTES_PER_TASK:],
                    attachments=attachments.get(tid, []),
                )
            )
        return tasks

    def _fetch_attachments(self, task_ids: list[int]) -> dict[int, list[dict]]:
        """Attachments linked to each task (direct + chatter). Text files are inlined (decoded);
        images/binaries are listed by name/type only — the text-only reviewer can't see pixels."""
        domain = [["res_model", "=", "project.task"], ["res_id", "in", task_ids]]
        try:
            recs = self._call("ir.attachment", "search_read", domain,
                              fields=ATTACHMENT_FIELDS, order="id desc", limit=200)
        except Exception:  # noqa: BLE001 — attachments are best-effort
            return {}

        by_task: dict[int, list[dict]] = {}
        inlined: dict[int, int] = {}
        for a in recs or []:
            tid = a.get("res_id")
            bucket = by_task.setdefault(tid, [])
            if len(bucket) >= MAX_ATTACHMENTS_PER_TASK:
                continue
            att = {"name": a.get("name", ""), "mimetype": a.get("mimetype", ""),
                   "size": a.get("file_size", 0), "text": ""}
            used = inlined.get(tid, 0)
            if (_is_text_mime(a.get("mimetype", "")) and a.get("type") == "binary"
                    and used < MAX_ATTACHMENT_TOTAL):
                blob = self._call("ir.attachment", "read", [a["id"]], fields=["datas"])
                data = (blob[0].get("datas") if blob else None)
                if data:
                    try:
                        text = base64.b64decode(data).decode("utf-8", "replace")[:MAX_ATTACHMENT_TEXT]
                        att["text"] = text
                        inlined[tid] = used + len(text)
                    except (ValueError, TypeError):
                        pass
            bucket.append(att)
        return by_task


def enrich_tasks(task_ids: list[int], creds: OdooCreds) -> tuple[list[Task], str]:
    """Return (tasks, warning). Never raises — warning is '' on success."""
    if not task_ids:
        return [], ""
    if not creds.complete:
        return (
            [Task(id=t, error="odoo credentials not set") for t in task_ids],
            "Odoo credentials incomplete — skipping task enrichment.",
        )
    client = OdooClient(creds)
    try:
        client.connect()
        return client.fetch_tasks(task_ids), ""
    except Exception as exc:  # noqa: BLE001 — enrichment must never block a review
        return (
            [Task(id=t, error=str(exc)) for t in task_ids],
            f"Odoo enrichment failed ({exc}); continuing without task context.",
        )
