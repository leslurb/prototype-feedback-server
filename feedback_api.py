"""Work-queue API. Built by make_router(db, now_iso) so this module never imports
main (no circular import, no second init_db())."""
from __future__ import annotations

import json
import uuid
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import feedback_state as fs

UPDATABLE = ("run_id", "lane", "commit_sha", "verify_selector", "after_path", "verified_at", "verify_json")


class StatusChange(BaseModel):
    to: str
    expected_from: str
    actor: str
    note: Optional[str] = None
    run_id: Optional[str] = None
    lane: Optional[str] = None
    commit_sha: Optional[str] = None
    verify_selector: Optional[str] = None
    after_path: Optional[str] = None
    verified_at: Optional[str] = None
    verify_json: Optional[str] = None


class QueueRequest(BaseModel):
    comment_ids: list[str] = []
    batch_ids: list[str] = []
    project_ids: list[str] = []


def load_project(c, pid: Optional[str]) -> Optional[dict]:
    if not pid:
        return None
    p = c.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    if p is None:
        return None
    d = dict(p)
    d["url_prefixes"] = json.loads(d.get("url_prefixes_json") or "[]")
    return d


def resolve_for_comment(c, row) -> Optional[dict]:
    b = c.execute("SELECT repo_hints_json FROM batches WHERE id = ?", (row["batch_id"],)).fetchone()
    hints = json.loads((b["repo_hints_json"] if b else None) or "{}")
    return fs.resolve_repo(load_project(c, row["project_id"]), row["page_url"], hints)


def make_router(db: Callable, now_iso: Callable[[], str]) -> APIRouter:
    router = APIRouter()

    def change_status(c, cid: str, body: StatusChange) -> dict:
        row = c.execute("SELECT status, recipient FROM comments WHERE id = ?", (cid,)).fetchone()
        if row is None:
            raise HTTPException(404, f"comment {cid} not found")
        if body.to not in fs.STATUSES or not fs.can_transition(body.expected_from, body.to, body.actor):
            raise HTTPException(422, f"{body.actor} may not move {body.expected_from} -> {body.to}")
        if body.to == "queued" and row["recipient"] != "llm":
            raise HTTPException(422, "human-recipient comments are never queued")
        extra = {k: getattr(body, k) for k in UPDATABLE if getattr(body, k) is not None}
        sets = ", ".join(["status = ?", "status_note = ?", "updated_at = ?"] + [f"{k} = ?" for k in extra])
        cur = c.execute(
            f"UPDATE comments SET {sets} WHERE id = ? AND status = ?",
            (body.to, body.note, now_iso(), *extra.values(), cid, body.expected_from),
        )
        if cur.rowcount == 0:
            current = c.execute("SELECT status FROM comments WHERE id = ?", (cid,)).fetchone()["status"]
            raise HTTPException(409, {"error": "stale expected_from", "current": current})
        c.execute(
            """INSERT INTO status_events (id, comment_id, at, from_status, to_status, actor, note, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), cid, now_iso(), body.expected_from, body.to, body.actor, body.note, body.run_id),
        )
        return {"id": cid, "status": body.to}

    @router.post("/api/comment/{cid}/status")
    def api_comment_status(cid: str, body: StatusChange):
        with db() as c:
            return change_status(c, cid, body)

    @router.post("/api/comments/queue")
    def api_bulk_queue(req: QueueRequest):
        queued, skipped = [], []
        with db() as c:
            rows = c.execute(
                f"""SELECT * FROM comments
                    WHERE id IN ({','.join('?' * len(req.comment_ids)) or "''"})
                       OR batch_id IN ({','.join('?' * len(req.batch_ids)) or "''"})
                       OR project_id IN ({','.join('?' * len(req.project_ids)) or "''"})
                    ORDER BY created_at, id""",
                (*req.comment_ids, *req.batch_ids, *req.project_ids),
            ).fetchall()
            for row in rows:
                if row["status"] != "new":
                    skipped.append({"id": row["id"], "reason": f"status {row['status']}"})
                elif row["recipient"] != "llm":
                    skipped.append({"id": row["id"], "reason": "human recipient"})
                elif resolve_for_comment(c, row) is None:
                    skipped.append({"id": row["id"], "reason": "no repo mapped"})
                else:
                    change_status(c, row["id"], StatusChange(to="queued", expected_from="new", actor=fs.USER))
                    queued.append(row["id"])
        return {"queued": queued, "skipped": skipped}

    router.change_status = change_status  # reused by later endpoints in this module
    return router
