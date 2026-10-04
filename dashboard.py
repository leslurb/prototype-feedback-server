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
        "blocked": q("SELECT id, comment, status_note FROM comments WHERE status = 'blocked' "
                     "ORDER BY updated_at DESC LIMIT 3"),
        "to_confirm": q("SELECT id, comment FROM comments WHERE status = 'verified' "
                        "ORDER BY updated_at DESC LIMIT 3"),
        "human": q("SELECT id, comment FROM comments WHERE recipient = 'human' "
                   "AND status NOT IN ('done', 'skipped') ORDER BY created_at DESC LIMIT 3"),
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

    return router
