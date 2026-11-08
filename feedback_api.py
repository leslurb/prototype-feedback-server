"""Work-queue API. Built by make_router(db, now_iso) so this module never imports
main (no circular import, no second init_db())."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
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


def build_overview(c) -> dict:
    """Everything the dashboard's main screen needs, from one connection."""
    out, needs = [], {"blocked": 0, "to_confirm": 0, "human_open": 0, "unmapped_projects": []}
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
    run = c.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    latest = None
    if run is not None:
        latest = dict(run)
        latest["summary"] = json.loads(latest["summary"] or "{}")
    return {"projects": out, "needs_you": needs, "last_run": latest}


def done_priors(c, project_id: str, exclude_id: Optional[str] = None) -> list[dict]:
    """The project's earlier DONE comments, most recent first, shaped for
    feedback_state.classify_repeat. Shared by ingest and the composer's repeat check."""
    rows = c.execute(
        """SELECT cm.id, cm.page_url, cm.selector, cm.bbox_json, b.shipped_at
           FROM comments cm JOIN batches b ON b.id = cm.batch_id
           WHERE cm.project_id = ? AND cm.status = 'done' AND cm.id IS NOT ?
           ORDER BY cm.updated_at DESC, cm.created_at DESC""",
        (project_id, exclude_id),
    )
    return [{"id": r["id"], "page": fs.normalize_path(r["page_url"]), "selector": r["selector"],
             "bbox": json.loads(r["bbox_json"] or "{}"), "shipped": bool(r["shipped_at"])} for r in rows]


def build_responses(rows, batch_id, responded_at: str) -> dict:
    """prototype-feedback-response/v2 from comment rows (spec 2026-11-08 §6)."""
    out = []
    for r in rows:
        status = fs.response_status(r["status"], r["recipient"])
        entry = {"comment_id": r["id"], "status": status, "acted_at": r["updated_at"],
                 "confirmed": r["status"] == "done"}
        note = r["status_note"]
        if status == "needs_input":
            entry["question_back"] = note
        elif status == "answered":
            entry["answer"] = note
        else:
            entry["summary"] = note
        if r["commit_sha"]:
            entry["commit_sha"] = r["commit_sha"]
        out.append(entry)
    return {"schema": "prototype-feedback-response/v2", "batch_id": batch_id,
            "export_id": None, "responded_at": responded_at, "responses": out}


def make_router(db: Callable, now_iso: Callable[[], str]) -> APIRouter:
    router = APIRouter()

    def change_status(c, cid: str, body: StatusChange) -> dict:
        row = c.execute("SELECT status, recipient FROM comments WHERE id = ?", (cid,)).fetchone()
        if row is None:
            raise HTTPException(404, f"comment {cid} not found")
        # A human-recipient comment has no "handled" state: the user closes it straight from new.
        human_close = (body.expected_from, body.to, body.actor) == ("new", "done", fs.USER) \
            and row["recipient"] == "human"
        # ...and undoes that close straight back to new (it is never re-queued).
        human_reopen = (body.expected_from, body.to, body.actor) == ("done", "new", fs.USER) \
            and row["recipient"] == "human"
        if body.to not in fs.STATUSES or not (human_close or human_reopen
                                              or fs.can_transition(body.expected_from, body.to, body.actor)):
            raise HTTPException(422, f"{body.actor} may not move {body.expected_from} -> {body.to}")
        if body.to == "queued" and row["recipient"] != "llm":
            raise HTTPException(422, "human-recipient comments are never queued")
        extra = {k: getattr(body, k) for k in UPDATABLE if getattr(body, k) is not None}
        if body.to in ("queued", "new"):  # back in the queue: the old run's claim no longer applies
            for k in ("run_id", "lane", "commit_sha"):
                extra.setdefault(k, None)
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

    def run_missing_or_ended(c, rid: str) -> HTTPException:
        if c.execute("SELECT 1 FROM runs WHERE id = ?", (rid,)).fetchone() is None:
            return HTTPException(404, f"run {rid} not found")
        return HTTPException(409, f"run {rid} has already ended")

    @router.post("/api/runs/{rid}/heartbeat")
    def api_run_heartbeat(rid: str):
        with db() as c:
            if c.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ? AND ended_at IS NULL",
                         (now_iso(), rid)).rowcount == 0:
                raise run_missing_or_ended(c, rid)
        return {"ok": True}

    @router.post("/api/runs/{rid}/end")
    def api_run_end(rid: str, body: RunEnd):
        with db() as c:
            n = c.execute("UPDATE runs SET ended_at = ?, status = ?, summary = ? WHERE id = ? AND ended_at IS NULL",
                          (now_iso(), body.status, json.dumps(body.summary), rid)).rowcount
            if n == 0:
                raise run_missing_or_ended(c, rid)
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
        cutoff = (datetime.fromisoformat(now_iso()) - timedelta(minutes=minutes)).isoformat()
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
                    raise HTTPException(422, "no repo mapped for this page (map the project's repo, or check url_prefixes)")
            pin = None
            if body.pin_comment_id:
                pin = c.execute("SELECT page_url, selector, bbox_json, screenshot_path, project_id FROM comments "
                                "WHERE id = ?", (body.pin_comment_id,)).fetchone()
                if pin is None:
                    raise HTTPException(404, "pin comment not found")
                if pin["project_id"] is not None and pin["project_id"] != pid:
                    raise HTTPException(422, "pin belongs to another project")
            repeat = None
            if pin is not None:  # same rule as the composer's repeat check, persisted like ingest does
                repeat = fs.classify_repeat(
                    {"page": fs.normalize_path(pin["page_url"]), "selector": pin["selector"],
                     "bbox": json.loads(pin["bbox_json"] or "{}")},
                    done_priors(c, pid, exclude_id=body.pin_comment_id))
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
                                         recipient, viewport_w, expected_result, selector, bbox_json, screenshot_path,
                                         repeat_of, repeat_kind)
                   VALUES (?, ?, ?, ?, ?, 'You', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cid, bid, now_iso(), body.page, text, project["name"], pid, body.recipient, body.viewport_w,
                 body.expected_result, pin["selector"] if pin else None,
                 pin["bbox_json"] if pin else "{}", pin["screenshot_path"] if pin else None,
                 repeat[0] if repeat else None, repeat[1] if repeat else None),
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
        with db() as c:
            return build_overview(c)

    @router.get("/api/projects/{pid}/repeat-check")
    def api_repeat_check(pid: str, pin_comment_id: str):
        with db() as c:
            if load_project(c, pid) is None:
                raise HTTPException(404, f"project {pid} not found")
            pin = c.execute("SELECT page_url, selector, bbox_json, project_id FROM comments WHERE id = ?",
                            (pin_comment_id,)).fetchone()
            if pin is None:
                raise HTTPException(404, "pin comment not found")
            if pin["project_id"] is not None and pin["project_id"] != pid:
                raise HTTPException(422, "pin belongs to another project")
            hit = fs.classify_repeat(
                {"page": fs.normalize_path(pin["page_url"]), "selector": pin["selector"],
                 "bbox": json.loads(pin["bbox_json"] or "{}")},
                done_priors(c, pid, exclude_id=pin_comment_id))
            if hit is None:
                return {"repeat": None}
            prior = c.execute("SELECT comment FROM comments WHERE id = ?", (hit[0],)).fetchone()
        return {"repeat": {"id": hit[0], "kind": hit[1], "comment": prior["comment"] if prior else None}}

    RESP_COLS = "id, status, recipient, status_note, commit_sha, updated_at"

    @router.get("/api/rounds/{bid}/responses.json")
    def api_round_responses(bid: str):
        with db() as c:
            if c.execute("SELECT 1 FROM batches WHERE id = ?", (bid,)).fetchone() is None:
                raise HTTPException(404, f"round {bid} not found")
            rows = c.execute(f"SELECT {RESP_COLS} FROM comments WHERE batch_id = ? ORDER BY created_at, id",
                             (bid,)).fetchall()
        return build_responses(rows, bid, now_iso())

    @router.get("/api/projects/{pid}/responses.json")
    def api_project_responses(pid: str):
        with db() as c:
            if load_project(c, pid) is None:
                raise HTTPException(404, f"project {pid} not found")
            rows = c.execute(f"SELECT {RESP_COLS} FROM comments WHERE project_id = ? ORDER BY created_at, id",
                             (pid,)).fetchall()
        return build_responses(rows, None, now_iso())

    router.change_status = change_status  # reused by later endpoints in this module
    return router
