"""Local ingest + browse server for prototype-feedback exports.

Run: uvicorn main:app --host 127.0.0.1 --port 8090 --reload
or:  run.bat
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import dashboard
import feedback_api
import feedback_schema
import feedback_state
import feedback_views

def _get_bundle_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).parent

def _get_data_dir():
    override = os.environ.get("PFB_DATA_DIR")
    if override:
        return Path(override)
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent / "data"
    return Path(__file__).parent / "data"

BUNDLE_DIR = _get_bundle_dir()
ROOT = Path(__file__).parent if not getattr(sys, 'frozen', False) else Path(sys.executable).parent
DATA = _get_data_dir()
BATCHES = DATA / "batches"
DB_PATH = DATA / "feedback.db"
DATA.mkdir(exist_ok=True)
BATCHES.mkdir(exist_ok=True)

app = FastAPI(title="Prototype Feedback")
app.mount("/static", StaticFiles(directory=BUNDLE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BUNDLE_DIR / "templates")
templates.env.filters["et"] = feedback_views.to_et  # shared with dashboard.py's router

PUBLISHED_EXTENSION_ORIGIN = "chrome-extension://lkpciiloidfgdhlgaefcbggajlfljmbp"


def cors_origins(extra: Optional[str]) -> list[str]:
    """The published extension, plus PFB_EXTRA_ORIGINS (comma-separated, e.g. an unpacked
    dev build's chrome-extension:// origin). The dashboard itself is same-origin."""
    return [PUBLISHED_EXTENSION_ORIGIN] + [o.strip() for o in (extra or "").split(",") if o.strip()]


# The allowlist only governs CORS-enabled reads and preflighted requests (e.g. JSON POSTs):
# a foreign page can't read our responses or pass a preflight. It does NOT stop "simple"
# cross-site POSTs (text/plain or form bodies), which browsers send without a preflight.
# A separate Sec-Fetch-Site/Origin check is planned before Plan D adds extension writes.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(os.environ.get("PFB_EXTRA_ORIGINS")),
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- DB --------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    ingested_at TEXT NOT NULL,
    schema_version TEXT,
    extension_version TEXT,
    exported_at TEXT,
    reviewer TEXT,
    project TEXT,
    source_filename TEXT,
    comment_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    created_at TEXT,
    edited_at TEXT,
    page_url TEXT,
    page_title TEXT,
    region_type TEXT,
    selector TEXT,
    bbox_json TEXT,
    viewport_json TEXT,
    screenshot_path TEXT,
    comment TEXT,
    reviewer TEXT,
    project TEXT
);
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    comment_id TEXT NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
    type TEXT,
    name TEXT,
    mime TEXT,
    size INTEGER,
    data_path TEXT,
    url TEXT,
    kind TEXT,
    title TEXT
);
CREATE INDEX IF NOT EXISTS idx_comments_batch ON comments(batch_id);
CREATE INDEX IF NOT EXISTS idx_comments_reviewer ON comments(reviewer);
CREATE INDEX IF NOT EXISTS idx_comments_project ON comments(project);
CREATE INDEX IF NOT EXISTS idx_attachments_comment ON attachments(comment_id);

-- Project tables (added v0.2)
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    target_url TEXT,
    owner_email TEXT,
    owner_name TEXT,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS project_reviewers (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    name TEXT,
    invited_at TEXT,
    last_reminded_at TEXT,
    status TEXT DEFAULT 'not_invited',
    UNIQUE (project_id, email)
);
CREATE INDEX IF NOT EXISTS idx_pr_project ON project_reviewers(project_id);
CREATE TABLE IF NOT EXISTS email_log (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    reviewer_email TEXT,
    kind TEXT,
    subject TEXT,
    body_html TEXT,
    delivery TEXT,
    gmail_id TEXT,
    gmail_thread_id TEXT,
    web_url TEXT,
    in_reply_to_comment_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_email_project ON email_log(project_id);
"""


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _column_exists(c: sqlite3.Connection, table: str, col: str) -> bool:
    rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == col for r in rows)


def init_db() -> None:
    with db() as c:
        c.executescript(SCHEMA)
        # Idempotent migrations
        if not _column_exists(c, "comments", "project_id"):
            c.execute("ALTER TABLE comments ADD COLUMN project_id TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_comments_project_id ON comments(project_id)")
        if not _column_exists(c, "comments", "reviewer_email"):
            c.execute("ALTER TABLE comments ADD COLUMN reviewer_email TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_comments_reviewer_email ON comments(reviewer_email)")
        if not _column_exists(c, "comments", "viewport_path"):
            c.execute("ALTER TABLE comments ADD COLUMN viewport_path TEXT")
        if not _column_exists(c, "projects", "drive_folder_id"):
            c.execute("ALTER TABLE projects ADD COLUMN drive_folder_id TEXT")
        feedback_schema.migrate(c)


init_db()


# ---- Helpers ---------------------------------------------------------------

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def safe_filename(name: str) -> str:
    cleaned = SAFE_NAME.sub("_", name or "file")
    return cleaned[:120]


def write_b64(b64: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(b64))


KIND_ICON = {
    "google_doc": "📄", "google_sheet": "📊", "google_slide": "📑",
    "google_form": "📝", "google_docs": "📁", "google_drive": "🗂",
    "figma": "🎨", "notion": "📓", "github": "💻", "loom": "🎥",
    "miro": "🧩", "link": "🔗",
}


def _format_report_ts(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        date_part = d.strftime("%b ") + str(d.day) + d.strftime(", %Y")
        hour12 = d.hour % 12 or 12
        ampm = "AM" if d.hour < 12 else "PM"
        time_part = f"{hour12}:{d.minute:02d} {ampm}"
        return f"{date_part} · {time_part}"
    except Exception:
        return iso


def _format_report_clock(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        return iso[11:19]
    except Exception:
        return ""


def _parse_page_url(url: Optional[str]) -> tuple[str, str, bool]:
    if not url:
        return "", "", False
    try:
        from urllib.parse import urlparse, parse_qsl
        u = urlparse(url)
        pathname = u.path or "/"
        params = ", ".join(f"{k}={v}" for k, v in parse_qsl(u.query, keep_blank_values=True))
        return pathname, params, True
    except Exception:
        return url, "", False


def _file_to_data_url(rel_path: Optional[str], mime: str = "image/png") -> Optional[str]:
    if not rel_path:
        return None
    p = DATA / rel_path
    if not p.exists():
        return None
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- Ingest ----------------------------------------------------------------


def ingest_payload(payload: dict, source_filename: str, source_dir: Optional[Path] = None) -> str:
    """Insert a batch + its comments + attachments. Returns batch_id.

    For v1/v2 payloads, screenshots/attachments come from inline base64.
    For v3 payloads, they come from files inside source_dir (the unzipped temp dir).
    """
    if not isinstance(payload, dict) or "comments" not in payload:
        raise HTTPException(400, "JSON missing 'comments' array")

    schema = payload.get("schema") or "unknown"
    if not str(schema).startswith("prototype-feedback/"):
        raise HTTPException(400, f"Unsupported schema: {schema}")

    batch_id = str(uuid.uuid4())
    batch_dir = BATCHES / batch_id
    (batch_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    (batch_dir / "attachments").mkdir(parents=True, exist_ok=True)
    (batch_dir / "viewport-screenshots").mkdir(parents=True, exist_ok=True)

    if source_dir is not None:
        src_json = source_dir / "feedback.json"
        if src_json.exists():
            shutil.copyfile(src_json, batch_dir / "feedback.json")

    comments = payload.get("comments", []) or []

    with db() as c:
        c.execute(
            """INSERT INTO batches
               (id, ingested_at, schema_version, extension_version, exported_at,
                reviewer, project, source_filename, comment_count, repo_hints_json, project_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                batch_id,
                now_iso(),
                schema,
                payload.get("extension_version"),
                payload.get("exported_at"),
                payload.get("reviewer"),
                payload.get("project"),
                source_filename,
                len(comments),
                json.dumps(payload.get("repo_hints") or {}),
                payload.get("project_id"),
            ),
        )

        comment_project_ids = set()
        for cm in comments:
            cid = cm.get("id") or str(uuid.uuid4())
            screenshot_path = None

            # v3: screenshot_path points at a file inside source_dir
            if cm.get("screenshot_path") and source_dir is not None:
                src = (source_dir / cm["screenshot_path"]).resolve()
                if not str(src).startswith(str(source_dir.resolve())):
                    raise HTTPException(400, f"screenshot_path escapes archive: {cm['screenshot_path']}")
                if src.exists():
                    rel = f"batches/{batch_id}/screenshots/{cid}.png"
                    dst = DATA / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)
                    screenshot_path = rel
            # v1/v2: screenshot_png_b64 inline
            elif cm.get("screenshot_png_b64"):
                rel = f"batches/{batch_id}/screenshots/{cid}.png"
                write_b64(cm["screenshot_png_b64"], DATA / rel)
                screenshot_path = rel

            viewport_path = None
            if cm.get("viewport_screenshot_path") and source_dir is not None:
                src = (source_dir / cm["viewport_screenshot_path"]).resolve()
                if not str(src).startswith(str(source_dir.resolve())):
                    raise HTTPException(400, f"viewport_screenshot_path escapes archive: {cm['viewport_screenshot_path']}")
                if src.exists():
                    rel = f"batches/{batch_id}/viewport-screenshots/{cid}.png"
                    dst = DATA / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)
                    viewport_path = rel

            reviewer_name = cm.get("reviewer") or payload.get("reviewer")
            reviewer_email = cm.get("reviewer_email") or payload.get("reviewer_email")
            project_label = cm.get("project") or payload.get("project")
            project_id = cm.get("project_id") or payload.get("project_id")

            # Auto-tag: if no project_id but the reviewer's email matches a single
            # project's reviewer roster, attach to that project.
            if not project_id and reviewer_email:
                hits = c.execute(
                    """SELECT DISTINCT project_id FROM project_reviewers
                       WHERE email = ?""",
                    (reviewer_email,),
                ).fetchall()
                if len(hits) == 1:
                    project_id = hits[0]["project_id"]

            comment_project_ids.add(project_id)
            recipient = str(cm.get("recipient_resolved") or "llm").strip().lower()
            if recipient not in ("llm", "human"):
                print(f"warning: comment {cid}: unknown recipient_resolved "
                      f"{cm.get('recipient_resolved')!r}, using 'llm'", file=sys.stderr)
                recipient = "llm"
            viewport_w = (cm.get("viewport") or {}).get("w")
            repeat = None
            if project_id:
                priors = feedback_api.done_priors(c, project_id)
                repeat = feedback_state.classify_repeat(
                    {"page": feedback_state.normalize_path(cm.get("page_url")),
                     "selector": cm.get("selector"), "bbox": cm.get("bbox") or {}},
                    priors,
                )

            c.execute(
                """INSERT INTO comments
                   (id, batch_id, created_at, edited_at, page_url, page_title,
                    region_type, selector, bbox_json, viewport_json, screenshot_path,
                    viewport_path, comment, reviewer, reviewer_email, project, project_id,
                    recipient, viewport_w, repeat_of, repeat_kind)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cid,
                    batch_id,
                    cm.get("created_at"),
                    cm.get("edited_at"),
                    cm.get("page_url"),
                    cm.get("page_title"),
                    cm.get("region_type"),
                    cm.get("selector"),
                    json.dumps(cm.get("bbox") or {}),
                    json.dumps(cm.get("viewport") or {}),
                    screenshot_path,
                    viewport_path,
                    cm.get("comment"),
                    reviewer_name,
                    reviewer_email,
                    project_label,
                    project_id,
                    recipient,
                    viewport_w,
                    repeat[0] if repeat else None,
                    repeat[1] if repeat else None,
                ),
            )

            # Update reviewer status on the project if matched.
            if project_id and reviewer_email:
                c.execute(
                    """UPDATE project_reviewers
                       SET status = 'commented'
                       WHERE project_id = ? AND email = ?""",
                    (project_id, reviewer_email),
                )

            for at in cm.get("attachments") or []:
                aid = str(uuid.uuid4())
                rel: Optional[str] = None

                # v3: attachment.path points at a file inside source_dir
                if at.get("type") in ("image", "file") and at.get("path") and source_dir is not None:
                    src = (source_dir / at["path"]).resolve()
                    if not str(src).startswith(str(source_dir.resolve())):
                        raise HTTPException(400, f"attachment path escapes archive: {at['path']}")
                    if src.exists():
                        fn = f"{aid}__{safe_filename(at.get('name', 'file'))}"
                        rel = f"batches/{batch_id}/attachments/{fn}"
                        dst = DATA / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(src, dst)
                # v1/v2: data_b64 inline
                elif at.get("type") in ("image", "file") and at.get("data_b64"):
                    fn = f"{aid}__{safe_filename(at.get('name', 'file'))}"
                    rel = f"batches/{batch_id}/attachments/{fn}"
                    write_b64(at["data_b64"], DATA / rel)
                c.execute(
                    """INSERT INTO attachments
                       (id, comment_id, type, name, mime, size, data_path, url, kind, title)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        aid,
                        cid,
                        at.get("type"),
                        at.get("name"),
                        at.get("mime"),
                        at.get("size"),
                        rel,
                        at.get("url"),
                        at.get("kind"),
                        at.get("title"),
                    ),
                )

        # No top-level project_id: if every comment landed in one project, the round is that project's.
        if not payload.get("project_id") and len(comment_project_ids) == 1 and None not in comment_project_ids:
            c.execute("UPDATE batches SET project_id = ? WHERE id = ?", (comment_project_ids.pop(), batch_id))

    # Write a slim "claude_input.json" + "claude_prompt.md" per batch.
    write_claude_artifacts(batch_id)
    return batch_id


def write_claude_artifacts(batch_id: str) -> None:
    """Emit slim JSON (paths instead of base64) + a one-line prompt for Claude Code."""
    with db() as c:
        b = c.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if not b:
            return
        rows = c.execute(
            "SELECT * FROM comments WHERE batch_id = ? ORDER BY created_at",
            (batch_id,),
        ).fetchall()
        att_rows = c.execute(
            """SELECT a.* FROM attachments a
               JOIN comments c ON a.comment_id = c.id
               WHERE c.batch_id = ?""",
            (batch_id,),
        ).fetchall()

    atts_by_comment: dict[str, list[dict]] = {}
    for a in att_rows:
        atts_by_comment.setdefault(a["comment_id"], []).append(
            {
                "type": a["type"],
                "name": a["name"],
                "mime": a["mime"],
                "size": a["size"],
                "file_path": a["data_path"],
                "url": a["url"],
                "kind": a["kind"],
                "title": a["title"],
            }
        )

    comments = []
    for r in rows:
        comments.append(
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "edited_at": r["edited_at"],
                "page_url": r["page_url"],
                "page_title": r["page_title"],
                "viewport": json.loads(r["viewport_json"] or "{}"),
                "region_type": r["region_type"],
                "selector": r["selector"],
                "bbox": json.loads(r["bbox_json"] or "{}"),
                "screenshot_file": r["screenshot_path"],
                "comment": r["comment"],
                "attachments": atts_by_comment.get(r["id"], []),
            }
        )

    payload = {
        "schema": "prototype-feedback/v2-slim",
        "exported_at": b["exported_at"],
        "reviewer": b["reviewer"],
        "project": b["project"],
        "comment_count": len(comments),
        "_llm_guide": {
            "purpose": "Pinned feedback from a human reviewer. Each comment is anchored to a DOM element OR viewport region with a cropped screenshot. Apply the changes the reviewer requests.",
            "how_to_apply": [
                "Iterate comments[] in order.",
                "For each comment, view screenshot_file (PNG cropped to bbox at the reviewer's devicePixelRatio) using the Read tool to see exactly what the reviewer was looking at.",
                "Locate the source file from page_url (ask the user if uncertain) and find the element via selector. The selector was valid at capture time; if it no longer matches, fall back to nearby visible text from the screenshot.",
                "For region_type='region' there is no selector; use bbox + screenshot to identify the target element(s).",
                "For attachments: type='image' or 'file' has file_path -- Read it; type='link' has url -- WebFetch it (Google Docs/Drive may require auth).",
                "Do not silently merge conflicting comments. Surface conflicts and ask the user.",
                "After applying, summarize per-comment what changed (or why skipped).",
            ],
            "field_reference": {
                "comments[].bbox": "{x,y,w,h} in CSS viewport pixels (NOT device pixels)",
                "comments[].viewport": "{w,h,scrollX,scrollY} in CSS px",
                "comments[].screenshot_file": "Relative path to a PNG cropped to bbox.",
                "comments[].attachments[].file_path": "Present for image/file. Read with the Read tool.",
                "comments[].attachments[].url": "Present for link. Use WebFetch.",
            },
        },
        "comments": comments,
    }

    out_dir = BATCHES / batch_id
    (out_dir / "claude_input.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    prompt = (
        f"Read `{(out_dir / 'claude_input.json').as_posix()}`. "
        f"Follow `_llm_guide.how_to_apply`. The screenshots and attachment files "
        f"referenced by `screenshot_file` / `file_path` live alongside it under "
        f"`{(out_dir).as_posix()}/`."
    )
    (out_dir / "claude_prompt.md").write_text(prompt + "\n", encoding="utf-8")


# ---- Routes ----------------------------------------------------------------


@app.get("/batches", response_class=HTMLResponse)
def home(request: Request):
    with db() as c:
        batches = c.execute(
            """SELECT b.*, COUNT(c.id) AS actual_count
               FROM batches b LEFT JOIN comments c ON c.batch_id = b.id
               GROUP BY b.id
               ORDER BY b.ingested_at DESC LIMIT 50"""
        ).fetchall()
        total_comments = c.execute("SELECT COUNT(*) AS n FROM comments").fetchone()["n"]
        reviewers = [
            r["reviewer"]
            for r in c.execute(
                "SELECT DISTINCT reviewer FROM comments WHERE reviewer IS NOT NULL"
            ).fetchall()
        ]
        projects = [
            r["project"]
            for r in c.execute(
                "SELECT DISTINCT project FROM comments WHERE project IS NOT NULL"
            ).fetchall()
        ]
    return templates.TemplateResponse(
        "batches.html",
        {
            "request": request,
            "batches": batches,
            "total_comments": total_comments,
            "reviewers": reviewers,
            "projects": projects,
        },
    )


@app.get("/comments", response_class=HTMLResponse)
def comments_view(
    request: Request,
    reviewer: Optional[str] = None,
    project: Optional[str] = None,
    batch: Optional[str] = None,
    q: Optional[str] = None,
):
    where = []
    params: list = []
    if reviewer:
        where.append("c.reviewer = ?")
        params.append(reviewer)
    if project:
        where.append("c.project = ?")
        params.append(project)
    if batch:
        where.append("c.batch_id = ?")
        params.append(batch)
    if q:
        where.append("(c.comment LIKE ? OR c.page_url LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    sql = "SELECT c.* FROM comments c"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY c.created_at DESC LIMIT 500"
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
        att_counts = {
            r["comment_id"]: r["n"]
            for r in conn.execute(
                "SELECT comment_id, COUNT(*) AS n FROM attachments GROUP BY comment_id"
            ).fetchall()
        }
    return templates.TemplateResponse(
        "comments.html",
        {
            "request": request,
            "rows": rows,
            "att_counts": att_counts,
            "filters": {"reviewer": reviewer, "project": project, "batch": batch, "q": q},
        },
    )


@app.get("/comment/{comment_id}", response_class=HTMLResponse)
def comment_detail(request: Request, comment_id: str):
    with db() as c:
        row = c.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        atts = c.execute(
            "SELECT * FROM attachments WHERE comment_id = ?", (comment_id,)
        ).fetchall()
        batch = c.execute("SELECT * FROM batches WHERE id = ?", (row["batch_id"],)).fetchone()
        history = c.execute(
            "SELECT * FROM status_events WHERE comment_id = ? ORDER BY at", (comment_id,)
        ).fetchall()
    bbox = json.loads(row["bbox_json"] or "{}")
    viewport = json.loads(row["viewport_json"] or "{}")
    return templates.TemplateResponse(
        "comment.html",
        {
            "request": request,
            "row": row,
            "atts": atts,
            "batch": batch,
            "bbox": bbox,
            "viewport": viewport,
            "history": history,
            "actions": feedback_views.actions_for(row["status"], row["recipient"]),
        },
    )


@app.get("/file/{path:path}")
def serve_file(path: str):
    p = (DATA / path).resolve()
    if not p.is_relative_to(DATA.resolve()):
        raise HTTPException(403)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.post("/api/ingest")
async def api_ingest(files: list[UploadFile] = File(...)):
    results = []
    for f in files:
        try:
            raw = await f.read()
            fname = f.filename or "uploaded"
            is_zip = fname.lower().endswith(".zip") or (raw[:4] == b"PK\x03\x04")
            if is_zip:
                with tempfile.TemporaryDirectory(prefix="pfb-ingest-") as tmpdir:
                    tmp = Path(tmpdir)
                    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                        for n in zf.namelist():
                            np = (tmp / n).resolve()
                            if not str(np).startswith(str(tmp.resolve())):
                                raise HTTPException(400, f"zip entry escapes archive: {n}")
                        zf.extractall(tmp)
                    fj = tmp / "feedback.json"
                    if not fj.exists():
                        raise HTTPException(400, "ZIP missing feedback.json at root")
                    payload = json.loads(fj.read_text(encoding="utf-8"))
                    batch_id = ingest_payload(payload, fname, source_dir=tmp)
            else:
                payload = json.loads(raw.decode("utf-8"))
                batch_id = ingest_payload(payload, fname)
            results.append({"ok": True, "batch_id": batch_id, "filename": fname})
        except HTTPException as e:
            results.append({"ok": False, "error": e.detail, "filename": f.filename})
        except Exception as e:
            results.append({"ok": False, "error": str(e), "filename": f.filename})
    return JSONResponse({"results": results})


@app.post("/api/batch/{batch_id}/delete")
def delete_batch(batch_id: str):
    with db() as c:
        c.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
    bd = BATCHES / batch_id
    if bd.exists():
        for p in sorted(bd.rglob("*"), reverse=True):
            try:
                p.unlink() if p.is_file() else p.rmdir()
            except OSError:
                pass
        try:
            bd.rmdir()
        except OSError:
            pass
    return RedirectResponse("/batches", status_code=303)


@app.post("/api/comment/{comment_id}/delete")
def delete_comment(comment_id: str):
    with db() as c:
        row = c.execute("SELECT batch_id, screenshot_path FROM comments WHERE id = ?", (comment_id,)).fetchone()
        if row and row["screenshot_path"]:
            p = DATA / row["screenshot_path"]
            if p.exists():
                p.unlink()
        c.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
        if row:
            write_claude_artifacts(row["batch_id"])
    return RedirectResponse("/comments", status_code=303)


@app.get("/batch/{batch_id}/claude", response_class=PlainTextResponse)
def batch_claude_prompt(batch_id: str):
    p = BATCHES / batch_id / "claude_prompt.md"
    if not p.exists():
        raise HTTPException(404, "Claude prompt not generated yet for this batch.")
    return p.read_text(encoding="utf-8")


@app.get("/batch/{batch_id}/json")
def batch_claude_json(batch_id: str):
    p = BATCHES / batch_id / "claude_input.json"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/json")


@app.get("/batch/{batch_id}/report.html", response_class=HTMLResponse)
def batch_report_html(batch_id: str, request: Request):
    with db() as c:
        b = c.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if not b:
            raise HTTPException(404, "Batch not found")
        comment_rows = c.execute(
            "SELECT * FROM comments WHERE batch_id = ? ORDER BY created_at", (batch_id,)
        ).fetchall()
        attachments_by_comment: dict[str, list] = {}
        for cm in comment_rows:
            ats = c.execute(
                "SELECT * FROM attachments WHERE comment_id = ? ORDER BY id", (cm["id"],)
            ).fetchall()
            attachments_by_comment[cm["id"]] = [dict(a) for a in ats]

    comments: list[dict] = []
    pages = set()
    element_pins = 0
    region_pins = 0
    for cm in comment_rows:
        bbox = json.loads(cm["bbox_json"] or "{}")
        pathname, params, url_ok = _parse_page_url(cm["page_url"])
        if url_ok:
            pages.add(pathname)
        if cm["region_type"] == "element":
            element_pins += 1
        else:
            region_pins += 1

        viewport_data = _file_to_data_url(cm["viewport_path"]) if "viewport_path" in cm.keys() else None
        crop_data = _file_to_data_url(cm["screenshot_path"])
        left_image = viewport_data or crop_data

        ats_out: list[dict] = []
        for at in attachments_by_comment.get(cm["id"], []):
            if at.get("type") == "link":
                ats_out.append({
                    "type": "link",
                    "url": at.get("url"),
                    "title": at.get("title"),
                    "icon": KIND_ICON.get(at.get("kind"), KIND_ICON["link"]),
                })
            else:
                mime = at.get("mime") or ("image/png" if at.get("type") == "image" else "application/octet-stream")
                ats_out.append({
                    "type": at.get("type"),
                    "name": at.get("name"),
                    "data_url": _file_to_data_url(at.get("data_path"), mime),
                })

        comments.append({
            "id": cm["id"],
            "page_url": cm["page_url"],
            "pathname": pathname,
            "params": params,
            "url_ok": url_ok,
            "region_type": cm["region_type"],
            "selector": cm["selector"],
            "bbox": bbox,
            "comment": cm["comment"],
            "left_image_data_url": left_image,
            "crop_data_url": crop_data,
            "formatted_ts": _format_report_ts(cm["created_at"]),
            "attachments": ats_out,
        })

    console_log: list[dict] = []
    src_json = BATCHES / batch_id / "feedback.json"
    if src_json.exists():
        try:
            raw = json.loads(src_json.read_text(encoding="utf-8"))
            for e in (raw.get("console_log") or []):
                console_log.append({
                    "ts": e.get("ts"),
                    "clock": _format_report_clock(e.get("ts")),
                    "level": e.get("level", "error"),
                    "message": e.get("message", ""),
                    "source": e.get("source"),
                    "line": e.get("line"),
                })
        except Exception:
            pass

    project_label = b["project"] or "Prototype"
    title = f"{project_label} — Feedback Report"

    return templates.TemplateResponse(
        "report.html.j2",
        {
            "request": request,
            "title": title,
            "reviewer": b["reviewer"],
            "generated_at": _format_report_ts(now_iso()),
            "comments": comments,
            "console_log": console_log,
            "pages_reviewed": len(pages),
            "element_pins": element_pins,
            "region_pins": region_pins,
        },
    )


# ---- Projects --------------------------------------------------------------


def _project_row(c, pid: str) -> sqlite3.Row:
    row = c.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    if not row:
        raise HTTPException(404, "Project not found")
    return row


def _project_summary(c, pid: str) -> dict:
    p = _project_row(c, pid)
    rev = c.execute(
        "SELECT * FROM project_reviewers WHERE project_id = ? ORDER BY email", (pid,)
    ).fetchall()
    counts = c.execute(
        "SELECT reviewer_email, COUNT(*) AS n FROM comments WHERE project_id = ? GROUP BY reviewer_email",
        (pid,),
    ).fetchall()
    by_email = {r["reviewer_email"]: r["n"] for r in counts if r["reviewer_email"]}
    total_comments = c.execute(
        "SELECT COUNT(*) AS n FROM comments WHERE project_id = ?", (pid,)
    ).fetchone()["n"]
    return {
        "project": dict(p),
        "reviewers": [
            {**dict(r), "comments_count": by_email.get(r["email"], 0)} for r in rev
        ],
        "comment_count": total_comments,
    }


@app.get("/projects", response_class=HTMLResponse)
def projects_view(request: Request):
    with db() as c:
        rows = c.execute(
            """SELECT p.*,
                      (SELECT COUNT(*) FROM project_reviewers WHERE project_id = p.id) AS reviewer_count,
                      (SELECT COUNT(*) FROM comments WHERE project_id = p.id) AS comment_count
               FROM projects p
               WHERE p.archived = 0
               ORDER BY p.updated_at DESC"""
        ).fetchall()
    g_status = _google_status()
    return templates.TemplateResponse(
        "projects.html",
        {"request": request, "projects": rows, "g_status": g_status},
    )


@app.get("/project/{pid}", response_class=HTMLResponse)
def project_detail(request: Request, pid: str):
    with db() as c:
        summary = _project_summary(c, pid)
        comments = c.execute(
            """SELECT c.*,
                      (SELECT COUNT(*) FROM attachments WHERE comment_id = c.id) AS att_count
               FROM comments c
               WHERE c.project_id = ?
               ORDER BY c.created_at DESC""",
            (pid,),
        ).fetchall()
    g_status = _google_status()
    return templates.TemplateResponse(
        "project.html",
        {
            "request": request,
            "summary": summary,
            "comments": comments,
            "g_status": g_status,
            "url_prefixes": json.loads(summary["project"]["url_prefixes_json"] if summary["project"]["url_prefixes_json"] else "[]"),
        },
    )


@app.get("/api/projects")
def api_projects_list():
    """Used by the extension popup."""
    with db() as c:
        rows = c.execute(
            """SELECT id, name, description, target_url, owner_email, owner_name, drive_folder_id
               FROM projects WHERE archived = 0 ORDER BY updated_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/projects")
async def api_projects_create(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    pid = str(uuid.uuid4())
    now = now_iso()
    with db() as c:
        c.execute(
            """INSERT INTO projects (id, created_at, updated_at, name, description,
                                     target_url, owner_email, owner_name, drive_folder_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pid, now, now, name,
                body.get("description"),
                body.get("target_url"),
                body.get("owner_email"),
                body.get("owner_name"),
                body.get("drive_folder_id"),
            ),
        )
    return {"id": pid}


@app.post("/api/projects/{pid}")
async def api_projects_update(pid: str, request: Request):
    body = await request.json()
    fields = {}
    for f in ("name", "description", "target_url", "owner_email", "owner_name", "drive_folder_id",
              "repo", "local_path", "preview_cmd", "preview_url"):
        if f in body:
            fields[f] = body[f]
    if "url_prefixes" in body:
        prefixes = body["url_prefixes"] or []
        # A bare string would be iterated char by char by resolve_repo and disable the prefix gate.
        if not isinstance(prefixes, list) or not all(isinstance(p, str) and p.strip() for p in prefixes):
            raise HTTPException(422, "url_prefixes must be a list of non-empty strings")
        fields["url_prefixes_json"] = json.dumps(prefixes)
    if not fields:
        return {"ok": True}
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE projects SET {cols} WHERE id = ?", (*fields.values(), pid))
    return {"ok": True}


@app.post("/api/projects/{pid}/archive")
def api_projects_archive(pid: str):
    with db() as c:
        c.execute("UPDATE projects SET archived = 1, updated_at = ? WHERE id = ?", (now_iso(), pid))
    return RedirectResponse("/projects", status_code=303)


@app.post("/api/projects/{pid}/reviewers")
async def api_reviewers_add(pid: str, request: Request):
    """Add one or more reviewers. Body: {reviewers: [{email, name}, ...]} or {email, name}."""
    body = await request.json()
    items = body.get("reviewers")
    if items is None:
        items = [body]
    added = []
    with db() as c:
        _project_row(c, pid)
        for it in items:
            email = (it.get("email") or "").strip().lower()
            if not email:
                continue
            try:
                rid = str(uuid.uuid4())
                c.execute(
                    """INSERT INTO project_reviewers (id, project_id, email, name, status)
                       VALUES (?, ?, ?, ?, 'not_invited')""",
                    (rid, pid, email, it.get("name")),
                )
                added.append({"id": rid, "email": email, "name": it.get("name")})
            except sqlite3.IntegrityError:
                # Duplicate; update name if provided.
                if it.get("name"):
                    c.execute(
                        "UPDATE project_reviewers SET name = ? WHERE project_id = ? AND email = ?",
                        (it.get("name"), pid, email),
                    )
    return {"added": added}


@app.post("/api/projects/{pid}/reviewers/{rid}/delete")
def api_reviewers_delete(pid: str, rid: str):
    with db() as c:
        c.execute("DELETE FROM project_reviewers WHERE id = ? AND project_id = ?", (rid, pid))
    return RedirectResponse(f"/project/{pid}", status_code=303)


# ---- Email -----------------------------------------------------------------


def _render_email(template: str, ctx: dict) -> tuple[str, str]:
    """Render <subject>\\n\\n<body html> from a template."""
    rendered = templates.get_template(template).render(**ctx)
    # Convention: first line "Subject: ...", blank line, then HTML.
    lines = rendered.lstrip().splitlines()
    if lines and lines[0].lower().startswith("subject:"):
        subject = lines[0].split(":", 1)[1].strip()
        body = "\n".join(lines[1:]).lstrip()
    else:
        subject = ctx.get("default_subject", "Message")
        body = rendered
    return subject, body


def _gmail_lazy_import():
    import google_gmail
    import google_oauth
    return google_gmail, google_oauth


def _google_status() -> dict:
    try:
        import google_oauth
        ok = google_oauth.is_authorized()
        return {"authorized": ok}
    except Exception as e:
        return {"authorized": False, "error": str(e)}


@app.post("/api/projects/{pid}/invite")
async def api_invite(pid: str, request: Request):
    body = await request.json()
    reviewer_ids: list[str] = body.get("reviewer_ids") or []
    draft_only: bool = body.get("draft_only", True)
    extra_message: str = body.get("extra_message") or ""

    google_gmail, google_oauth = _gmail_lazy_import()
    if not google_oauth.is_authorized():
        raise HTTPException(401, "Not authenticated to Google. Visit /auth/google first.")

    with db() as c:
        proj = _project_row(c, pid)
        rev_rows = c.execute(
            f"SELECT * FROM project_reviewers WHERE project_id = ? AND id IN ({','.join('?'*len(reviewer_ids))})"
            if reviewer_ids
            else "SELECT * FROM project_reviewers WHERE project_id = ?",
            (pid, *reviewer_ids) if reviewer_ids else (pid,),
        ).fetchall()

    results = []
    for r in rev_rows:
        subject, html = _render_email(
            "email_invite.html",
            {
                "project": dict(proj),
                "reviewer": dict(r),
                "extra_message": extra_message,
                "default_subject": f"Please review: {proj['name']}",
            },
        )
        try:
            res = google_gmail.send_or_draft(
                draft_only=draft_only,
                to=r["email"],
                subject=subject,
                body_html=html,
            )
            with db() as c:
                c.execute(
                    """INSERT INTO email_log (id, created_at, project_id, reviewer_email,
                       kind, subject, body_html, delivery, gmail_id, gmail_thread_id, web_url)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()), now_iso(), pid, r["email"],
                        "invite", subject, html, res["kind"],
                        res.get("id"), res.get("thread_id"), res.get("web_url"),
                    ),
                )
                c.execute(
                    """UPDATE project_reviewers SET status = 'invited', invited_at = ?
                       WHERE id = ?""",
                    (now_iso(), r["id"]),
                )
            results.append({"reviewer": r["email"], "ok": True, **res})
        except Exception as e:
            results.append({"reviewer": r["email"], "ok": False, "error": str(e)})
    return {"results": results}


@app.post("/api/projects/{pid}/remind")
async def api_remind(pid: str, request: Request):
    body = await request.json()
    draft_only = body.get("draft_only", True)
    google_gmail, google_oauth = _gmail_lazy_import()
    if not google_oauth.is_authorized():
        raise HTTPException(401, "Not authenticated to Google.")

    with db() as c:
        proj = _project_row(c, pid)
        # Reviewers who were invited but haven't commented.
        rows = c.execute(
            """SELECT pr.* FROM project_reviewers pr
               WHERE pr.project_id = ? AND pr.status = 'invited'""",
            (pid,),
        ).fetchall()

    results = []
    for r in rows:
        subject, html = _render_email(
            "email_reminder.html",
            {
                "project": dict(proj),
                "reviewer": dict(r),
                "default_subject": f"Reminder: please review {proj['name']}",
            },
        )
        try:
            res = google_gmail.send_or_draft(
                draft_only=draft_only,
                to=r["email"],
                subject=subject,
                body_html=html,
            )
            with db() as c:
                c.execute(
                    """INSERT INTO email_log (id, created_at, project_id, reviewer_email,
                       kind, subject, body_html, delivery, gmail_id, gmail_thread_id, web_url)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()), now_iso(), pid, r["email"],
                        "reminder", subject, html, res["kind"],
                        res.get("id"), res.get("thread_id"), res.get("web_url"),
                    ),
                )
                c.execute(
                    "UPDATE project_reviewers SET last_reminded_at = ? WHERE id = ?",
                    (now_iso(), r["id"]),
                )
            results.append({"reviewer": r["email"], "ok": True, **res})
        except Exception as e:
            results.append({"reviewer": r["email"], "ok": False, "error": str(e)})
    return {"results": results}


@app.post("/api/comment/{cid}/reply")
async def api_comment_reply(cid: str, request: Request):
    body = await request.json()
    message = (body.get("message") or "").strip()
    draft_only = body.get("draft_only", True)
    if not message:
        raise HTTPException(400, "message required")

    google_gmail, google_oauth = _gmail_lazy_import()
    if not google_oauth.is_authorized():
        raise HTTPException(401, "Not authenticated to Google.")

    with db() as c:
        cmt = c.execute("SELECT * FROM comments WHERE id = ?", (cid,)).fetchone()
        if not cmt:
            raise HTTPException(404)
        if not cmt["reviewer_email"]:
            raise HTTPException(400, "Comment has no reviewer_email; cannot reply.")
        proj = None
        if cmt["project_id"]:
            proj = c.execute("SELECT * FROM projects WHERE id = ?", (cmt["project_id"],)).fetchone()

    proj_name = proj["name"] if proj else (cmt["project"] or "your feedback")
    subject = f"Re: {proj_name} — {(cmt['comment'] or '')[:60]}"
    html = (
        f"<p>Hi {cmt['reviewer'] or 'there'},</p>"
        f"<p>About your comment on <a href=\"{cmt['page_url']}\">{cmt['page_url']}</a>:</p>"
        f"<blockquote style=\"border-left:3px solid #ccc;padding-left:10px;color:#555\">{cmt['comment']}</blockquote>"
        f"<p>{message.replace(chr(10), '<br>')}</p>"
    )
    res = google_gmail.send_or_draft(
        draft_only=draft_only,
        to=cmt["reviewer_email"],
        subject=subject,
        body_html=html,
    )
    with db() as c:
        c.execute(
            """INSERT INTO email_log (id, created_at, project_id, reviewer_email,
               kind, subject, body_html, delivery, gmail_id, gmail_thread_id, web_url, in_reply_to_comment_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()), now_iso(), cmt["project_id"], cmt["reviewer_email"],
                "reply", subject, html, res["kind"],
                res.get("id"), res.get("thread_id"), res.get("web_url"), cid,
            ),
        )
    return res


# ---- People (directory) ----------------------------------------------------


@app.get("/api/people/search")
def api_people_search(q: str = Query(..., min_length=2)):
    import google_oauth
    if not google_oauth.is_authorized():
        return {"authorized": False, "results": []}
    import google_people
    try:
        return {"authorized": True, "results": google_people.search_directory(q)}
    except Exception as e:
        return {"authorized": True, "error": str(e), "results": []}


# ---- OAuth -----------------------------------------------------------------


@app.get("/auth/google")
def auth_google_start():
    """Run the InstalledAppFlow on the server. Opens the user's browser."""
    import google_oauth
    try:
        google_oauth.get_credentials(allow_browser=True)
    except google_oauth.OAuthNotConfigured as e:
        return HTMLResponse(
            f"<h2>OAuth setup required</h2><pre>{e}</pre>"
            f"<p>See README, section 'Google Workspace setup'.</p>",
            status_code=400,
        )
    return RedirectResponse("/projects?auth=ok", status_code=303)


@app.post("/auth/google/revoke")
def auth_google_revoke():
    import google_oauth
    google_oauth.revoke()
    return RedirectResponse("/projects?auth=revoked", status_code=303)


@app.get("/api/auth/status")
def auth_status():
    return _google_status()


app.include_router(feedback_api.make_router(db, now_iso))
app.include_router(dashboard.make_dashboard_router(db, templates, now_iso))


def _cli() -> None:
    """Entry point for the `prototype-feedback-server` console script.

    Honors PFB_HOST and PFB_PORT environment variables; defaults to
    127.0.0.1:8090. Use `--reload` for dev.
    """
    import argparse
    import os

    import uvicorn

    parser = argparse.ArgumentParser(prog="prototype-feedback-server")
    parser.add_argument("--host", default=os.environ.get("PFB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PFB_PORT", "8090")))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run("main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    _cli()
