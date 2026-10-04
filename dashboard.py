"""Server-rendered dashboard pages (Plan B). Built by make_dashboard_router(db, templates,
now_iso) so it never imports main."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

import feedback_api as fa
import feedback_state as fs
import feedback_views as fv

ET = ZoneInfo("America/New_York")


def needs_detail(c, now_iso_str: str) -> dict:
    cutoff = (datetime.fromisoformat(now_iso_str) - timedelta(days=5)).isoformat()

    def q(sql, *args):
        return [dict(r) for r in c.execute(sql, args).fetchall()]

    return {
        "blocked": q("""SELECT cm.id, cm.comment, cm.status_note FROM comments cm
                        JOIN projects p ON p.id = cm.project_id AND p.archived = 0
                        WHERE cm.status = 'blocked' ORDER BY cm.updated_at DESC LIMIT 3"""),
        "to_confirm": q("""SELECT cm.id, cm.comment FROM comments cm
                           JOIN projects p ON p.id = cm.project_id AND p.archived = 0
                           WHERE cm.status = 'verified' ORDER BY cm.updated_at DESC LIMIT 3"""),
        "human": q("""SELECT cm.id, cm.comment FROM comments cm
                      JOIN projects p ON p.id = cm.project_id AND p.archived = 0
                      WHERE cm.recipient = 'human' AND cm.status NOT IN ('done', 'skipped')
                      ORDER BY cm.created_at DESC LIMIT 3"""),
        "silent": q("""SELECT pr.email, pr.name, pr.invited_at, p.id AS project_id, p.name AS project
                       FROM project_reviewers pr JOIN projects p ON p.id = pr.project_id
                       WHERE pr.status = 'invited' AND pr.invited_at IS NOT NULL
                         AND pr.invited_at < ? AND p.archived = 0""", cutoff),
    }


def make_dashboard_router(db: Callable, templates, now_iso: Callable[[], str]) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with db() as c:
            ov = fa.build_overview(c)
            needs = needs_detail(c, now_iso())
        for p in ov["projects"]:
            p["rounds"] = fv.number_rounds(p["rounds"])
            total: dict = {}
            for r in p["rounds"]:
                r["segments"] = fv.progress_segments(r["counts"])
                r["summary"] = fv.summary(r["counts"])
                r["state_class"] = fv.STATE_CLASS.get(r["state"], "plain")
                r["queueable"] = 0 if r["state"] == "Needs repo" else r["counts"].get("new", 0)
                for k, v in r["counts"].items():
                    total[k] = total.get(k, 0) + v
            p["segments"] = fv.progress_segments(total)
            p["summary"] = fv.summary(total)
            p["repeats"] = sum(r["repeats"] for r in p["rounds"])
        cap_note = fv.lane_cap_note(datetime.fromisoformat(now_iso()).astimezone(ET))
        return templates.TemplateResponse("dashboard.html", {
            "request": request, "ov": ov, "needs": needs, "cap_note": cap_note})

    @router.get("/round/{bid}", response_class=HTMLResponse)
    def round_view(request: Request, bid: str):
        with db() as c:
            b = c.execute("SELECT * FROM batches WHERE id = ?", (bid,)).fetchone()
            if b is None:
                raise HTTPException(404, f"round {bid} not found")
            pid: Optional[str] = b["project_id"]
            if pid is None:
                hit = c.execute("SELECT project_id FROM comments WHERE batch_id = ? AND project_id IS NOT NULL "
                                "LIMIT 1", (bid,)).fetchone()
                pid = hit["project_id"] if hit else None
            project = fa.load_project(c, pid)
            if pid:
                rows = c.execute("""SELECT * FROM comments WHERE batch_id = ?
                                    AND (project_id = ? OR (project_id IS NULL AND ? = ?))
                                    ORDER BY created_at, id""", (bid, pid, b["project_id"], pid)).fetchall()
            else:
                rows = c.execute("SELECT * FROM comments WHERE batch_id = ? ORDER BY created_at, id",
                                 (bid,)).fetchall()
            if b["kind"] == "direct":
                label = "Your issues"
            elif pid:
                n = c.execute(
                    """SELECT COUNT(*) FROM batches b WHERE b.kind = 'export' AND b.ingested_at <= ?
                       AND (b.project_id = ? OR EXISTS (SELECT 1 FROM comments cm
                            WHERE cm.batch_id = b.id AND cm.project_id = ?))""",
                    (b["ingested_at"], pid, pid)).fetchone()[0]
                label = f"Round {n}"
            else:
                label = "Round"
        hints = json.loads(b["repo_hints_json"] or "{}")
        comments = [{**dict(r), "actions": fv.actions_for(r["status"], r["recipient"])} for r in rows]
        llm = [r for r in comments if r["recipient"] == "llm"]
        human = [r for r in comments if r["recipient"] == "human"]
        counts: dict = {}
        for r in llm:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        needs_repo = any(r["status"] == "new" and fs.resolve_repo(project, r["page_url"], hints) is None
                         for r in llm)
        state = fs.round_state(counts, needs_repo, bool(b["shipped_at"]), bool(b["rereview_sent_at"]))
        return templates.TemplateResponse("round.html", {
            "request": request, "batch": dict(b), "project": project, "label": label,
            "lanes": fv.lanes(llm), "human": human, "llm_count": len(llm), "state": state,
            "state_class": fv.STATE_CLASS.get(state, "plain"), "can_ship": fs.can_ship(counts)})

    return router
