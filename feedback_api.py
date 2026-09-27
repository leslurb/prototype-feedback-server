"""Work-queue API. Built by make_router(db, now_iso) so this module never imports
main (no circular import, no second init_db())."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import feedback_state as fs

UPDATABLE = ("run_id", "lane", "commit_sha", "verify_selector", "after_path", "verified_at", "verify_json")


class RunStart(BaseModel):
    cap: int


class RunEnd(BaseModel):
    status: Literal["finished", "stopped", "failed"]
    summary: dict = {}


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


class DirectIssue(BaseModel):
    title: str
    page: str
    recipient: str = "llm"
    details: Optional[str] = None
    viewport_w: Optional[int] = None
    expected_result: Optional[str] = None
    pin_comment_id: Optional[str] = None
    queue: bool = False


class RoundMark(BaseModel):
    what: str  # shipped | rereview_sent


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
    b = c.execute("SELECT repo_hints_json, project_id FROM batches WHERE id = ?", (row["batch_id"],)).fetchone()
    hints = json.loads((b["repo_hints_json"] if b else None) or "{}")
    pid = row["project_id"] or (b["project_id"] if b else None)
    return fs.resolve_repo(load_project(c, pid), row["page_url"], hints)


def make_router(db: Callable, now_iso: Callable[[], str]) -> APIRouter:
    router = APIRouter()

    def change_status(c, cid: str, body: StatusChange) -> dict:
        row = c.execute("SELECT status, recipient FROM comments WHERE id = ?", (cid,)).fetchone()
        if row is None:
            raise HTTPException(404, f"comment {cid} not found")
        # A human-recipient comment has no "handled" state: the user closes it straight from new.
        human_close = (body.expected_from, body.to, body.actor) == ("new", "done", fs.USER) \
            and row["recipient"] == "human"
        if body.to not in fs.STATUSES or not (human_close or fs.can_transition(body.expected_from, body.to, body.actor)):
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
                    try:
                        change_status(c, row["id"], StatusChange(to="queued", expected_from="new", actor=fs.USER))
                        queued.append(row["id"])
                    except HTTPException as e:
                        skipped.append({"id": row["id"], "reason": f"changed concurrently: {e.detail}"})
            found_ids = {row["id"] for row in rows}
            for cid in req.comment_ids:
                if cid not in found_ids:
                    skipped.append({"id": cid, "reason": "not found"})
        return {"queued": queued, "skipped": skipped}

    @router.get("/api/queue")
    def api_queue():
        projects: dict = {}
        unqueued: list = []
        with db() as c:
            rows = c.execute(
                "SELECT * FROM comments WHERE status = 'queued' ORDER BY created_at, id"
            ).fetchall()
            for row in rows:
                repo = resolve_for_comment(c, row)
                if repo is None:  # mapping removed after queueing: surface it, never drop silently
                    try:
                        change_status(c, row["id"], StatusChange(
                            to="new", expected_from="queued", actor=fs.USER, note="unqueued: no repo mapped"))
                        unqueued.append({"id": row["id"], "reason": "no repo mapped"})
                    except HTTPException as e:  # a concurrent change must not abort the read
                        unqueued.append({"id": row["id"], "reason": f"changed concurrently: {e.detail}"})
                    continue
                key = (row["project_id"], repo["repo"])
                proj = projects.setdefault(key, {
                    "project_id": row["project_id"], "repo": repo["repo"], "local_path": repo["local_path"],
                    "repo_source": repo["source"], "lanes": {}})
                page = fs.normalize_path(row["page_url"])
                proj["lanes"].setdefault(page, []).append({
                    "id": row["id"], "batch_id": row["batch_id"], "page_url": row["page_url"],
                    "selector": row["selector"], "viewport_w": row["viewport_w"],
                    "comment": row["comment"], "expected_result": row["expected_result"],
                    "screenshot_path": row["screenshot_path"], "repeat_of": row["repeat_of"],
                    "repeat_kind": row["repeat_kind"]})
        out = [{**{k: v for k, v in p.items() if k != "lanes"},
                "lanes": [{"page": pg, "comments": cms} for pg, cms in p["lanes"].items()]}
               for p in projects.values()]
        return {"projects": out, "total": sum(len(l["comments"]) for p in out for l in p["lanes"]),
                "unqueued": unqueued}

    @router.post("/api/runs")
    def api_run_start(body: RunStart):
        rid = str(uuid.uuid4())
        with db() as c:
            c.execute("INSERT INTO runs (id, started_at, heartbeat_at, cap, status) VALUES (?, ?, ?, ?, 'running')",
                      (rid, now_iso(), now_iso(), body.cap))
        return {"id": rid}

    @router.post("/api/runs/{rid}/heartbeat")
    def api_run_heartbeat(rid: str):
        with db() as c:
            if c.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ?", (now_iso(), rid)).rowcount == 0:
                raise HTTPException(404, f"run {rid} not found")
        return {"ok": True}

    @router.post("/api/runs/{rid}/end")
    def api_run_end(rid: str, body: RunEnd):
        with db() as c:
            n = c.execute("UPDATE runs SET ended_at = ?, status = ?, summary = ? WHERE id = ?",
                          (now_iso(), body.status, json.dumps(body.summary), rid)).rowcount
            if n == 0:
                raise HTTPException(404, f"run {rid} not found")
        return {"ok": True}

    @router.get("/api/runs/latest")
    def api_run_latest():
        with db() as c:
            r = c.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        if r is None:
            return None
        d = dict(r)
        d["summary"] = json.loads(d["summary"] or "{}")
        return d

    @router.get("/api/stale")
    def api_stale(minutes: int = 30):
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        with db() as c:
            rows = c.execute(
                """SELECT cm.id, cm.run_id, cm.lane, r.heartbeat_at, r.ended_at
                   FROM comments cm LEFT JOIN runs r ON r.id = cm.run_id
                   WHERE cm.status = 'doing'
                     AND (r.id IS NULL OR r.ended_at IS NOT NULL OR r.heartbeat_at < ?)""",
                (cutoff,),
            ).fetchall()
        return {"stale": [dict(r) for r in rows]}

    @router.post("/api/projects/{pid}/issues")
    def api_direct_issue(pid: str, body: DirectIssue):
        if not body.title.strip() or not body.page.strip():
            raise HTTPException(422, "title and page are required")
        if body.recipient not in ("llm", "human"):
            raise HTTPException(422, "recipient must be llm or human")
        with db() as c:
            project = load_project(c, pid)
            if project is None:
                raise HTTPException(404, f"project {pid} not found")
            if body.queue:
                if body.recipient != "llm":
                    raise HTTPException(422, "human issues are never queued")
                if not (body.expected_result or "").strip():
                    raise HTTPException(422, "expected_result is required to queue")
                if fs.resolve_repo(project, body.page, None) is None:
                    raise HTTPException(422, "project has no repo mapped")
            pin = None
            if body.pin_comment_id:
                pin = c.execute("SELECT selector, bbox_json, screenshot_path, project_id FROM comments WHERE id = ?",
                                (body.pin_comment_id,)).fetchone()
                if pin is None:
                    raise HTTPException(404, "pin comment not found")
                if pin["project_id"] is not None and pin["project_id"] != pid:
                    raise HTTPException(422, "pin belongs to another project")
            b = c.execute("SELECT id FROM batches WHERE project_id = ? AND kind = 'direct'", (pid,)).fetchone()
            if b is None:
                bid = str(uuid.uuid4())
                c.execute("""INSERT INTO batches (id, ingested_at, reviewer, project, project_id, kind, comment_count)
                             VALUES (?, ?, 'You', ?, ?, 'direct', 0)""", (bid, now_iso(), project["name"], pid))
            else:
                bid = b["id"]
            cid = str(uuid.uuid4())
            text = body.title if not body.details else f"{body.title}\n\n{body.details}"
            c.execute(
                """INSERT INTO comments (id, batch_id, created_at, page_url, comment, reviewer, project, project_id,
                                         recipient, viewport_w, expected_result, selector, bbox_json, screenshot_path)
                   VALUES (?, ?, ?, ?, ?, 'You', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cid, bid, now_iso(), body.page, text, project["name"], pid, body.recipient, body.viewport_w,
                 body.expected_result, pin["selector"] if pin else None,
                 pin["bbox_json"] if pin else "{}", pin["screenshot_path"] if pin else None),
            )
            c.execute("UPDATE batches SET comment_count = comment_count + 1 WHERE id = ?", (bid,))
            status = "new"
            if body.queue:
                change_status(c, cid, StatusChange(to="queued", expected_from="new", actor=fs.USER))
                status = "queued"
        return {"id": cid, "batch_id": bid, "status": status}

    @router.post("/api/rounds/{bid}/mark")
    def api_round_mark(bid: str, body: RoundMark):
        col = {"shipped": "shipped_at", "rereview_sent": "rereview_sent_at"}.get(body.what)
        if col is None:
            raise HTTPException(422, "what must be shipped or rereview_sent")
        with db() as c:
            b = c.execute("SELECT kind FROM batches WHERE id = ?", (bid,)).fetchone()
            if b is None:
                raise HTTPException(404, f"round {bid} not found")
            if b["kind"] == "direct":
                raise HTTPException(422, "the direct stream is never shipped as a round")
            counts = {r["status"]: r["n"] for r in c.execute(
                "SELECT status, COUNT(*) AS n FROM comments WHERE batch_id = ? AND recipient = 'llm' GROUP BY status",
                (bid,))}
            if not fs.can_ship(counts):
                raise HTTPException(409, {"error": "round not finished", "counts": counts})
            c.execute(f"UPDATE batches SET {col} = ? WHERE id = ?", (now_iso(), bid))
        return {"ok": True}

    @router.get("/api/overview")
    def api_overview():
        out, needs = [], {"blocked": 0, "to_confirm": 0, "human_open": 0, "unmapped_projects": []}
        with db() as c:
            for p in c.execute("SELECT * FROM projects WHERE archived = 0 ORDER BY name"):
                project = load_project(c, p["id"])
                batches = c.execute(
                    """SELECT * FROM batches b
                       WHERE b.project_id = :pid
                          OR EXISTS (SELECT 1 FROM comments cm WHERE cm.batch_id = b.id AND cm.project_id = :pid)
                       ORDER BY CASE b.kind WHEN 'direct' THEN 0 ELSE 1 END, b.ingested_at DESC""",
                    {"pid": p["id"]}).fetchall()
                rounds, trend, waiting_unmapped = [], [], 0
                for b in batches:
                    rows = c.execute(
                        """SELECT * FROM comments
                           WHERE batch_id = ? AND (project_id = ? OR (project_id IS NULL AND ? = ?))""",
                        (b["id"], p["id"], b["project_id"], p["id"])).fetchall()
                    llm = [r for r in rows if r["recipient"] == "llm"]
                    counts: dict = {}
                    for r in llm:
                        counts[r["status"]] = counts.get(r["status"], 0) + 1
                    hints = json.loads(b["repo_hints_json"] or "{}")
                    unmapped_new = sum(1 for r in llm if r["status"] == "new"
                                       and fs.resolve_repo(project, r["page_url"], hints) is None)
                    waiting_unmapped += unmapped_new
                    human_open = sum(1 for r in rows if r["recipient"] == "human" and r["status"] not in ("done", "skipped"))
                    rounds.append({
                        "id": b["id"], "kind": b["kind"], "reviewer": b["reviewer"], "ingested_at": b["ingested_at"],
                        "counts": counts, "human_open": human_open,
                        "state": fs.round_state(counts, unmapped_new > 0, bool(b["shipped_at"]), bool(b["rereview_sent_at"])),
                        "repeats": sum(1 for r in rows if r["repeat_kind"]),
                    })
                    if b["kind"] == "export":
                        trend.append(len(rows))
                    needs["blocked"] += counts.get("blocked", 0)
                    needs["to_confirm"] += counts.get("verified", 0)
                    needs["human_open"] += human_open
                if waiting_unmapped:
                    needs["unmapped_projects"].append({"project_id": p["id"], "name": p["name"], "waiting": waiting_unmapped})
                out.append({"id": p["id"], "name": p["name"], "repo": p["repo"], "rounds": rounds,
                            "trend": list(reversed(trend))})
        latest = None
        with db() as c:
            run = c.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        if run is not None:
            latest = dict(run)
            latest["summary"] = json.loads(latest["summary"] or "{}")
        return {"projects": out, "needs_you": needs, "last_run": latest}

    router.change_status = change_status  # reused by later endpoints in this module
    return router
