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

    router.change_status = change_status  # reused by later endpoints in this module
    return router
