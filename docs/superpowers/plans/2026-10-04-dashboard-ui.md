# Feedback Dashboard — Plan B: Dashboard UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the four screens from the design canvas (all projects, round view, confirm-a-fix panel, new issue) as server-rendered pages on top of Plan A's work-queue API. Every action calls an existing endpoint.

**Non-goals:**
- **"Send for re-review"**: rendered disabled, labelled "needs Plan D (resolutions export)".
- **After-screenshots / Playwright verify**: Plan C. The after panel shows a labelled placeholder.
- **Extension pin push**: Plan D. The composer's pin picker lists the project's existing pinned comments.
- **"Draft it"** (an LLM-drafted expected result): rendered disabled. The server has no LLM wiring.
- The `/feedback` skill and the logon task: Plan E.
- No JS framework and no build step.

**Architecture:**
- Pages are Jinja templates extending `layout.html`, rendered by a new `dashboard.py` router (`make_dashboard_router(db, templates, now_iso)`, which never imports `main`).
- View-model logic lives in a pure `feedback_views.py` and is unit-tested.
- One vanilla file, `static/dashboard.js`, makes every state change through Plan A's JSON API and shows the server's 409/422 text verbatim.
- `/` becomes the dashboard. The old batch list and ingest drop-zone move to `/batches`.
- CORS narrows from `*` to the published extension plus `PFB_EXTRA_ORIGINS`.

**Tech Stack:** FastAPI 0.115, Jinja2 3.1, sqlite3, vanilla JS, pytest + httpx TestClient, `tzdata` (Windows has no system tz database).

**Assumptions / Preconditions:**
- Repo `C:\Users\sebas\prototype-feedback-server`, on `main` at Plan A's merge (`f2f0e8a`) or later, with a clean tree. Create branch `feature/dashboard-ui` first. The baseline suite is `84 passed`.
- Tests run only through pytest (`tests/conftest.py` sets `PFB_DATA_DIR` before importing `main`, and refuses to wipe a non-test DB). Never import `main` outside pytest, and never start the server on the real `data/`.
- Plan A API available:
  - `POST /api/comment/{id}/status` (`{to, expected_from, actor}`)
  - `POST /api/comments/queue` (`{comment_ids, batch_ids, project_ids}` → `{queued, skipped}`)
  - `POST /api/rounds/{bid}/mark` (`{what}`)
  - `POST /api/projects/{pid}/issues`
  - `POST /api/projects/{pid}` (repo mapping; `url_prefixes` must be a list)
  - `GET /api/overview`
- Existing routes stay: `/projects`, `/project/{id}`, `/comments`, `/comment/{id}` (extended, not replaced), `/file/{path}`, `/api/projects/{pid}/remind` (`{draft_only}`; 401 without Google auth).
- `feedback_state` provides `normalize_path`, `classify_repeat`, `round_state`, `can_ship`, `can_transition`, `USER`. The human-recipient `new → done` move is allowed by `feedback_api.change_status` for actor `user`.
- Design: canvas https://claude.ai/artifact/7f7Qyh1sXG5fn9snG2xbpw; memory `project_prototype_feedback_dashboard_design.md`.

**Rollback:** Code-only, no schema change. `git revert` the branch's commits, or don't merge. `/batches` returns to `/` on revert.

---

## File structure

| File | Responsibility |
|---|---|
| `feedback_views.py` (new) | Pure view models: progress segments, summary text, round labels, page lanes, allowed actions, fleet-window lane note |
| `dashboard.py` (new) | Page routes `/`, `/round/{bid}`, `/new-issue` and the `needs_detail(c, now)` query |
| `feedback_api.py` (modify) | Extract module-level `build_overview(c)` and `done_priors(c, pid, exclude_id)`; add `GET /api/projects/{pid}/repeat-check` |
| `main.py` (modify) | CORS allowlist; `/` → `/batches`; ingest uses `done_priors`; comment page gets actions + history; project page gets `url_prefixes`; mount the dashboard router |
| `templates/dashboard.html`, `round.html`, `new_issue.html`, `partials/comment_card.html`, `partials/comment_queue.html` (new) | The screens |
| `templates/index.html` → `templates/batches.html` (git mv) | The old batch list + drop-zone, unchanged |
| `templates/layout.html`, `comment.html`, `project.html` (modify) | Nav; include the queue panel; repo-mapping form |
| `static/dashboard.js` (new), `static/style.css` (append) | Interactions; styles |
| `requirements.txt` (modify) | `tzdata` |
| `tests/test_views.py`, `test_cors.py`, `test_pages.py`, `test_repeat_check.py` (new) | Tests |

---

### Task 0: Branch, baseline, tzdata

- [ ] **Step 1: Branch and baseline**

Run: `git checkout -b feature/dashboard-ui && .venv\Scripts\python.exe -m pytest tests -q`
Expected: `84 passed`.

- [ ] **Step 2: Add tzdata**

Append `tzdata==2024.2` to `requirements.txt`, then run `.venv\Scripts\python.exe -m pip install tzdata==2024.2`.
Verify: `.venv\Scripts\python.exe -c "from zoneinfo import ZoneInfo; print(ZoneInfo('America/New_York'))"` → `America/New_York` (on 2026-10-04 this raised `ZoneInfoNotFoundError`).

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "deps: tzdata (Windows has no system tz database; dashboard shows ET)"
```

---

### Task 1: Extract `build_overview` and `done_priors` (behaviour-preserving)

**Files:** `feedback_api.py`, `main.py` (ingest priors block, ~lines 375-386). Test: the existing suite. This is a pure refactor; `tests/test_api_overview.py` and `tests/test_ingest_queue_fields.py` are the guard.

- [ ] **Step 1: Move the body of `api_overview` to a module-level function**

In `feedback_api.py`, add above `make_router`:
```python
def build_overview(c) -> dict:
    """Everything the dashboard's main screen needs, from one connection."""
    out, needs = [], {"blocked": 0, "to_confirm": 0, "human_open": 0, "unmapped_projects": []}
    # <move the existing body of api_overview here verbatim, from `for p in c.execute(...)`
    #  through the `runs` lookup, replacing the two inner `with db() as c:` blocks with the
    #  passed-in `c`, and returning {"projects": out, "needs_you": needs, "last_run": latest}>
```
The text in angle brackets describes a move, not new code: cut lines 307-353 of the current `api_overview`, paste them here, and delete the two `with db() as c:` lines (dedenting their bodies by one level). Then the route becomes:
```python
    @router.get("/api/overview")
    def api_overview():
        with db() as c:
            return build_overview(c)
```

- [ ] **Step 2: Extract the ingest priors query**

Add to `feedback_api.py` (module level):
```python
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
```
In `main.py` ingest, replace the `priors = [ ... ]` list comprehension with `priors = feedback_api.done_priors(c, project_id)`. `main.py` already imports `feedback_api`.

- [ ] **Step 3: Verify**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: `84 passed`, with no test edits.

- [ ] **Step 4: Commit**

```bash
git add feedback_api.py main.py
git commit -m "refactor: build_overview(c) and done_priors() reusable by the dashboard"
```

---

### Task 2: CORS allowlist

**Files:** `main.py` (CORS middleware block, ~lines 51-57). Test: `tests/test_cors.py`.

- [ ] **Step 1: Failing test**

```python
import main

EXT = "chrome-extension://lkpciiloidfgdhlgaefcbggajlfljmbp"


def _preflight(client, origin):
    return client.options("/api/comments/queue", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})


def test_foreign_origin_is_refused(client):
    r = _preflight(client, "https://evil.example")
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers


def test_published_extension_is_allowed(client):
    r = _preflight(client, EXT)
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == EXT


def test_extra_origins_parsed_from_env():
    assert main.cors_origins("chrome-extension://dev1, http://x.test ,") == [
        EXT, "chrome-extension://dev1", "http://x.test"]
```
Run: `.venv\Scripts\python.exe -m pytest tests/test_cors.py -q` → FAIL (with `*`, the evil origin gets 200, and `cors_origins` doesn't exist).

- [ ] **Step 2: Implement**

Replace the middleware block with:
```python
PUBLISHED_EXTENSION_ORIGIN = "chrome-extension://lkpciiloidfgdhlgaefcbggajlfljmbp"


def cors_origins(extra: Optional[str]) -> list[str]:
    """The published extension, plus PFB_EXTRA_ORIGINS (comma-separated, e.g. an unpacked
    dev build's chrome-extension:// origin). The dashboard itself is same-origin."""
    return [PUBLISHED_EXTENSION_ORIGIN] + [o.strip() for o in (extra or "").split(",") if o.strip()]


# Only the extension may call /api/* cross-origin; every JSON POST needs a preflight,
# which a foreign page now fails.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(os.environ.get("PFB_EXTRA_ORIGINS")),
    allow_methods=["*"],
    allow_headers=["*"],
)
```

- [ ] **Step 3: GREEN + full suite.** `pytest tests/test_cors.py -q` → `3 passed`; `pytest tests -q` → 0 failed.

- [ ] **Step 4: Commit** — `git add main.py tests/test_cors.py && git commit -m "security: CORS allowlist (published extension + PFB_EXTRA_ORIGINS) instead of *"`

---

### Task 3: Move the batch list to `/batches`

**Files:** `git mv templates/index.html templates/batches.html`; `main.py` (`home` route); `templates/layout.html`. Test: `tests/test_pages.py` (created here).

- [ ] **Step 1: Failing test**

`tests/test_pages.py`:
```python
import re

import main


def test_batches_page_keeps_ingest_and_list(client):
    html = client.get("/batches").text
    assert "Drop a feedback export to ingest" in html
    assert "Recent batches" in html


def test_nav_links_dashboard_batches_new_issue(client):
    html = client.get("/batches").text
    for href in ('href="/"', 'href="/batches"', 'href="/new-issue"', 'href="/projects"'):
        assert href in html
```
Run → FAIL (`/batches` returns 404).

- [ ] **Step 2: Implement**

`git mv templates/index.html templates/batches.html`. In `main.py`, change `@app.get("/", response_class=HTMLResponse)` above `def home` to `@app.get("/batches", response_class=HTMLResponse)`, and `"index.html"` to `"batches.html"` in its `TemplateResponse`. In `layout.html`, replace the `<nav>` with:
```html
  <nav>
    <a href="/">Dashboard</a>
    <a href="/new-issue">New issue</a>
    <a href="/projects">Projects</a>
    <a href="/batches">Batches</a>
    <a href="/comments">All comments</a>
  </nav>
```

- [ ] **Step 3: GREEN** — `pytest tests/test_pages.py -q` → `2 passed`. **Step 4: Commit** — `git add -A templates main.py tests/test_pages.py && git commit -m "ui: batch list + ingest move to /batches; nav"` (check `git status` first: only these paths).

---

### Task 4: Pure view models

**Files:** create `feedback_views.py`, `tests/test_views.py`.

- [ ] **Step 1: Failing tests**

```python
from datetime import datetime

import pytest

import feedback_state as fs
import feedback_views as fv


def test_segments_follow_fixed_order_and_skip_zeros():
    assert fv.progress_segments({"new": 2, "done": 3, "queued": 0}) == [
        {"status": "done", "n": 3}, {"status": "new", "n": 2}]


def test_summary_counts_skipped_in_the_denominator():
    assert fv.summary({"done": 3, "skipped": 3}) == "3/6 done · 3 skipped"
    assert fv.summary({}) == "0/0 done"


def test_number_rounds_labels_direct_and_counts_exports_oldest_first():
    rounds = [{"id": "d", "kind": "direct"}, {"id": "r3", "kind": "export"},
              {"id": "r2", "kind": "export"}, {"id": "r1", "kind": "export"}]
    assert [r["label"] for r in fv.number_rounds(rounds)] == ["Your issues", "Round 3", "Round 2", "Round 1"]


def test_lanes_group_by_normalised_page_in_first_seen_order():
    rows = [{"id": 1, "page_url": "https://x.dev/products?a=1"}, {"id": 2, "page_url": "https://x.dev/"},
            {"id": 3, "page_url": "https://x.dev/products/"}]
    assert [(l["page"], [c["id"] for c in l["comments"]]) for l in fv.lanes(rows)] == [
        ("/products", [1, 3]), ("/", [2])]


@pytest.mark.parametrize("status", fs.STATUSES)
@pytest.mark.parametrize("recipient", ["llm", "human"])
def test_every_offered_action_is_one_the_server_allows(status, recipient):
    for a in fv.actions_for(status, recipient):
        assert a["from"] == status
        human_close = recipient == "human" and (a["from"], a["to"]) == ("new", "done")
        assert human_close or fs.can_transition(a["from"], a["to"], fs.USER), a
        assert not (recipient == "human" and a["to"] == "queued")


def test_key_actions_present():
    assert ("verified", "done") in {(a["from"], a["to"]) for a in fv.actions_for("verified", "llm")}
    assert ("new", "queued") in {(a["from"], a["to"]) for a in fv.actions_for("new", "llm")}
    assert ("new", "done") in {(a["from"], a["to"]) for a in fv.actions_for("new", "human")}
    assert ("new", "done") not in {(a["from"], a["to"]) for a in fv.actions_for("new", "llm")}


@pytest.mark.parametrize("hm,in_window", [((7, 59), False), ((8, 0), True), ((8, 44), True), ((8, 45), False),
                                          ((10, 0), True), ((10, 44), True), ((10, 45), False), ((14, 0), False)])
def test_lane_cap_note_tracks_fleet_job_windows(hm, in_window):
    note = fv.lane_cap_note(datetime(2026, 10, 5, hm[0], hm[1]))
    assert note.startswith("1 lane now") is in_window
```
Run → FAIL (`ModuleNotFoundError: feedback_views`).

- [ ] **Step 2: Implement `feedback_views.py`**

```python
"""View models for the dashboard pages. Pure: plain data in, plain data out."""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

import feedback_state as fs

SEG_ORDER = ("done", "verified", "doing", "queued", "new", "blocked", "skipped")
STATE_CLASS = {
    "Needs repo": "bad", "Running": "warn", "Queued": "info", "Blocked": "bad",
    "Open": "plain", "To confirm": "info", "Ready to ship": "good",
    "Re-review sent": "purple", "Shipped": "plain", "Human only": "plain",
}
# (from, to, label, button kind). Every entry must be a move feedback_api allows for the
# user (the human-recipient new->done close is the one rule outside the table).
ACTIONS = [
    ("verified", "done", "Looks fixed: mark Done", "primary"),
    ("verified", "queued", "Not fixed: re-queue", ""),
    ("verified", "skipped", "Skip", ""),
    ("new", "queued", "Queue", "primary"),
    ("new", "done", "Mark done", "primary"),
    ("new", "skipped", "Skip", ""),
    ("queued", "new", "Unqueue", ""),
    ("queued", "skipped", "Skip", ""),
    ("doing", "skipped", "Skip (cancels at merge)", ""),
    ("blocked", "queued", "Re-queue", "primary"),
    ("blocked", "skipped", "Skip", ""),
    ("done", "queued", "Reopen", ""),
    ("skipped", "new", "Restore", ""),
]


def progress_segments(counts: dict) -> list[dict]:
    return [{"status": s, "n": counts[s]} for s in SEG_ORDER if counts.get(s)]


def summary(counts: dict) -> str:
    total = sum(counts.get(s, 0) for s in SEG_ORDER)
    parts = [f"{counts.get('done', 0)}/{total} done"]
    parts += [f"{counts[s]} {s}" for s in SEG_ORDER[1:] if counts.get(s)]
    return " · ".join(parts)


def number_rounds(rounds: list[dict]) -> list[dict]:
    """Input order is build_overview's: direct first, then exports newest first."""
    exports = [r["id"] for r in rounds if r["kind"] != "direct"]
    return [{**r, "label": "Your issues" if r["kind"] == "direct"
             else f"Round {len(exports) - exports.index(r['id'])}"} for r in rounds]


def lanes(rows: Iterable) -> list[dict]:
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(fs.normalize_path(r["page_url"]), []).append(r)
    return [{"page": p, "comments": cs} for p, cs in grouped.items()]


def actions_for(status: str, recipient: str) -> list[dict]:
    out = []
    for frm, to, label, kind in ACTIONS:
        if frm != status:
            continue
        if recipient == "human" and to == "queued":
            continue
        if recipient != "human" and (frm, to) == ("new", "done"):
            continue
        out.append({"from": frm, "to": to, "label": label, "kind": kind})
    return out


def lane_cap_note(now_et: datetime) -> str:
    """Mirrors /queue run's cap rule: 1 lane inside the fleet's Claude-job windows."""
    hm = now_et.hour * 60 + now_et.minute
    if 8 * 60 <= hm < 8 * 60 + 45 or 10 * 60 <= hm < 10 * 60 + 45:
        return "1 lane now — fleet job window"
    return "3 lanes now · 1 during 08:00–08:45 and 10:00–10:45 ET"
```

- [ ] **Step 3: GREEN** — `pytest tests/test_views.py -q` → 0 failed. Then prove the action guard can fail: temporarily add `("new", "verified", "x", "")` to `ACTIONS`, see `test_every_offered_action_is_one_the_server_allows` fail, and remove it again.
- [ ] **Step 4: Commit** — `git add feedback_views.py tests/test_views.py && git commit -m "feat(ui): pure view models for the dashboard pages"`

---

### Task 5: Dashboard page `/`

**Files:** create `dashboard.py`, `templates/dashboard.html`; append to `static/style.css`; `main.py` (mount). Test: `tests/test_pages.py`.

- [ ] **Step 1: Failing tests (append to `tests/test_pages.py`)**

```python
def _seed():
    with main.db() as c:
        c.execute("INSERT INTO projects (id, created_at, updated_at, name, repo) VALUES ('p1','t','t','abdry','r/a')")
        c.execute("INSERT INTO projects (id, created_at, updated_at, name) VALUES ('p2','t','t','sunsip')")
        for bid, at, who, pid in [("r1", "2026-09-09", "Mike", "p1"), ("r2", "2026-09-18", "Mike", "p1"),
                                  ("s1", "2026-09-24", "Cailyn", "p2")]:
            c.execute("INSERT INTO batches (id, ingested_at, reviewer, project, project_id, kind) "
                      "VALUES (?,?,?,?,?,'export')", (bid, at, who, pid, pid))
        rows = [("a", "r1", "done", "llm", "p1", "https://x.dev/"),
                ("b", "r2", "verified", "llm", "p1", "https://x.dev/products"),
                ("c2", "r2", "new", "llm", "p1", "https://x.dev/"),
                ("h", "r2", "new", "human", "p1", "https://x.dev/"),
                ("s", "s1", "new", "llm", "p2", "https://y.dev/")]
        c.executemany("INSERT INTO comments (id, batch_id, status, recipient, project_id, page_url, comment) "
                      "VALUES (?,?,?,?,?,?, 'text ' || ?)", [(*r, r[0]) for r in rows])


def test_dashboard_lists_projects_rounds_and_states(client):
    _seed()
    html = client.get("/").text
    assert "abdry" in html and "sunsip" in html
    assert "Round 2" in html and "Round 1" in html
    assert "Ready to ship" in html            # r1: all done
    assert "Needs repo" in html               # s1: no repo
    assert "Drop a feedback export" not in html   # the drop-zone moved to /batches


def test_unmapped_round_checkbox_is_disabled_and_mapped_one_is_queueable(client):
    _seed()
    html = client.get("/").text
    assert re.search(r'value="s1"[^>]*data-new="0"[^>]*disabled', html)
    assert re.search(r'value="r2"[^>]*data-new="1"(?![^>]*disabled)', html)


def test_needs_you_strip(client):
    _seed()
    html = client.get("/").text
    assert "1 to confirm" in html and 'href="/comment/b"' in html
    assert "1 for you, not Claude" in html and 'href="/comment/h"' in html
    assert "No repo mapped" in html and 'href="/project/p2#mapping"' in html
```
Run → FAIL (`/` returns 404 after Task 3).

- [ ] **Step 2: Implement `dashboard.py`**

```python
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
```
In `main.py`, add `import dashboard` next to `import feedback_api`, and directly below the existing `app.include_router(feedback_api.make_router(db, now_iso))` line add:
```python
app.include_router(dashboard.make_dashboard_router(db, templates, now_iso))
```

- [ ] **Step 3: `templates/dashboard.html`**

```html
{% extends "layout.html" %}
{% block title %}Dashboard — Prototype Feedback{% endblock %}
{% block content %}
<div class="dash-head">
  <div>
    <h1>Prototype Feedback</h1>
    <p class="muted">{{ ov.projects|length }} projects · two producers: reviewer exports and your own issues</p>
  </div>
  <div class="dash-actions">
    <a class="btn" href="/new-issue">+ New issue</a>
    <div class="queue-box">
      <button class="btn primary" data-action="queue-selected" disabled>Select to queue</button>
      <div class="small">Copies <code>/feedback run</code> · drains everything queued</div>
      <div class="small muted">{{ cap_note }}</div>
      <div class="small" data-msg="queue"></div>
    </div>
  </div>
</div>

<section class="needs" aria-label="Needs you">
  {% if ov.last_run %}
  <div class="card"><div class="card-k">Last run</div>
    <div>{{ ov.last_run.started_at[:16].replace("T", " ") }} · {{ ov.last_run.status }}</div>
    <div class="small muted">{% for k, v in ov.last_run.summary.items() %}{{ v }} {{ k }}{% if not loop.last %} · {% endif %}{% endfor %}</div>
  </div>
  {% endif %}
  {% if needs.to_confirm %}
  <div class="card"><div class="card-k info">{{ ov.needs_you.to_confirm }} to confirm</div>
    {% for c in needs.to_confirm %}<div><a href="/comment/{{ c.id }}">{{ (c.comment or c.id)|truncate(60) }}</a></div>{% endfor %}
  </div>
  {% endif %}
  {% if needs.blocked %}
  <div class="card"><div class="card-k bad">{{ ov.needs_you.blocked }} blocked</div>
    {% for c in needs.blocked %}<div><a href="/comment/{{ c.id }}">{{ (c.comment or c.id)|truncate(60) }}</a><div class="small muted">{{ c.status_note or "" }}</div></div>{% endfor %}
  </div>
  {% endif %}
  {% if needs.human %}
  <div class="card"><div class="card-k">{{ ov.needs_you.human_open }} for you, not Claude</div>
    {% for c in needs.human %}<div><a href="/comment/{{ c.id }}">{{ (c.comment or c.id)|truncate(60) }}</a></div>{% endfor %}
  </div>
  {% endif %}
  {% for u in ov.needs_you.unmapped_projects %}
  <div class="card"><div class="card-k bad">No repo mapped</div>
    <div>{{ u.name }} · {{ u.waiting }} waiting</div>
    <a class="btn small" href="/project/{{ u.project_id }}#mapping">Map repo</a>
  </div>
  {% endfor %}
  {% for s in needs.silent %}
  <div class="card"><div class="card-k">Reviewer silent</div>
    <div>{{ s.name or s.email }} · {{ s.project }}</div>
    <div class="small muted">invited {{ s.invited_at[:10] }}</div>
    <button class="btn small" data-action="remind" data-project="{{ s.project_id }}">Send reminder</button>
    <div class="small" data-msg="remind-{{ s.project_id }}"></div>
  </div>
  {% endfor %}
  {% if not (needs.to_confirm or needs.blocked or needs.human or ov.needs_you.unmapped_projects or needs.silent) %}
  <div class="card muted">Nothing needs you.</div>
  {% endif %}
</section>

<div class="legend">{% for s in ["done", "verified", "doing", "queued", "new", "blocked", "skipped"] %}<span><i class="seg seg-{{ s }}"></i>{{ s }}</span>{% endfor %}</div>

{% for p in ov.projects %}
<article class="proj">
  <div class="proj-row">
    <input type="checkbox" data-project-toggle="{{ p.id }}" aria-label="Select all queueable rounds in {{ p.name }}">
    <div><a href="/project/{{ p.id }}" class="proj-name">{{ p.name }}</a>
      <div class="mono small muted">{{ p.repo or "no repo mapped" }}</div></div>
    <div><div class="bar">{% for s in p.segments %}<i class="seg seg-{{ s.status }}" style="flex-grow: {{ s.n }}"></i>{% endfor %}</div>
      <div class="small">{{ p.summary }}</div></div>
    <div class="small"><span class="mono">{{ p.trend|join(" → ") or "—" }}</span><div class="muted">reviewer comments / round</div></div>
    <div>{% if p.repeats %}<span class="chip bad">{{ p.repeats }} repeat{{ "s" if p.repeats != 1 else "" }}</span>{% else %}<span class="chip plain">no repeats</span>{% endif %}</div>
  </div>
  {% for r in p.rounds %}
  <div class="round-row">
    <input type="checkbox" name="round" value="{{ r.id }}" data-project="{{ p.id }}" data-new="{{ r.queueable }}" {% if not r.queueable %}disabled{% endif %} aria-label="Select {{ r.label }}">
    <div><strong>{{ r.label }}</strong> <span class="muted small">{{ r.reviewer or "" }} · {{ r.ingested_at[:10] }}</span></div>
    <div class="small">{{ r.summary }}{% if r.human_open %} · {{ r.human_open }} human open{% endif %}</div>
    <span class="chip {{ r.state_class }}">{{ r.state }}</span>
    <a href="/round/{{ r.id }}">Open</a>
  </div>
  {% endfor %}
</article>
{% else %}
<p class="muted">No projects yet. Create one under <a href="/projects">Projects</a>.</p>
{% endfor %}
<script src="/static/dashboard.js"></script>
{% endblock %}
```

- [ ] **Step 4: Append styles to `static/style.css`**

```css
/* ---- dashboard (Plan B) ---- */
.dash-head { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }
.dash-actions { display: flex; gap: 12px; align-items: flex-start; }
.queue-box { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; }
.needs { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; margin: 16px 0; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.card-k { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
.card-k.bad, .err { color: var(--danger); }
.card-k.info { color: var(--primary); }
.ok { color: var(--success); }
.legend { display: flex; gap: 14px; font-size: 12px; color: var(--muted); margin: 8px 0; }
.legend span { display: inline-flex; align-items: center; gap: 4px; }
.legend .seg { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
.proj { background: var(--card); border: 1px solid var(--border); border-radius: 12px; margin: 12px 0; overflow: hidden; }
.proj-row, .round-row { display: grid; grid-template-columns: 24px 240px 1fr 160px 120px; gap: 14px; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--border); }
.round-row { grid-template-columns: 24px 240px 1fr 120px 60px; padding-left: 36px; }
.proj-name { font-size: 16px; font-weight: 600; text-decoration: none; color: var(--text); }
.bar { display: flex; height: 8px; border-radius: 4px; overflow: hidden; background: #eef2f7; }
.seg-done { background: #15803d; } .seg-verified { background: #86c79a; } .seg-doing { background: #d4a017; }
.seg-queued { background: #2563eb; } .seg-new { background: #94a3b8; } .seg-blocked { background: #c2410c; }
.seg-skipped { background: repeating-linear-gradient(45deg, #64748b 0 3px, #eef2f7 3px 6px); }
.chip { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; background: #f1f5f9; white-space: nowrap; }
.chip.good, .chip.st-done { background: #dcfce7; color: #166534; }
.chip.warn, .chip.st-doing { background: #fef3c7; color: #92400e; }
.chip.info, .chip.st-verified, .chip.st-queued { background: #dbeafe; color: #1e40af; }
.chip.bad, .chip.st-blocked { background: #ffedd5; color: #9a3412; }
.chip.purple { background: #ede9fe; color: #5b21b6; }
.chip.dark { background: #0f172a; color: #fff; }
.lanes { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; }
.lane h2 { font-size: 14px; margin: 12px 0 8px; }
.ccard { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px; margin-bottom: 10px; display: flex; flex-direction: column; gap: 6px; }
.ccard.status-blocked { box-shadow: inset 0 0 0 2px #c2410c; }
.ccard-top { display: flex; justify-content: space-between; align-items: center; gap: 6px; }
.ccard-actions { display: flex; flex-wrap: wrap; gap: 6px; }
.queue-panel { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; margin-bottom: 20px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 12px 0; }
.grid2 img { max-width: 100%; border: 1px solid var(--border); border-radius: 8px; }
.placeholder { border: 2px dashed var(--border); border-radius: 8px; padding: 40px 12px; text-align: center; color: var(--muted); }
.sheet { display: flex; flex-direction: column; gap: 12px; max-width: 680px; }
.sheet label { display: flex; flex-direction: column; gap: 4px; font-weight: 600; font-size: 13px; }
.sheet input, .sheet textarea, .sheet select { font: inherit; padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px; font-weight: 400; }
.sheet fieldset { border: 1px solid var(--border); border-radius: 8px; display: flex; flex-direction: column; gap: 6px; }
.sheet label.pin { flex-direction: row; align-items: center; gap: 8px; font-weight: 400; }
.checks { list-style: none; padding: 0; display: flex; gap: 14px; font-size: 13px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.btn { white-space: nowrap; }
```

- [ ] **Step 5: GREEN** — `pytest tests/test_pages.py -q` → 0 failed. Prove the disabled-checkbox test can fail: temporarily change `r["queueable"] = 0 if ...` to `r["counts"].get("new", 0)`, see `test_unmapped_round_checkbox_is_disabled...` fail, then restore.
- [ ] **Step 6: Commit** — `git add dashboard.py templates/dashboard.html static/style.css main.py tests/test_pages.py && git commit -m "feat(ui): dashboard main screen at /"`

---

### Task 6: Round view `/round/{bid}`

**Files:** `dashboard.py`, `templates/round.html`, `templates/partials/comment_card.html`. Test: `tests/test_pages.py`.

- [ ] **Step 1: Failing tests (append)**

```python
def test_round_view_lanes_humans_and_locked_ship(client):
    _seed()
    html = client.get("/round/r2").text
    assert '<h2 class="mono">/products</h2>' in html and '<h2 class="mono">/</h2>' in html
    assert "For you, not for Claude · 1" in html
    assert re.search(r'data-action="ship"[^>]*disabled', html)
    assert 'data-id="b" data-from="verified" data-to="done"' in html
    assert "Send for re-review" in html and "Plan D" in html


def test_ship_unlocks_when_every_llm_item_is_done_or_skipped(client):
    _seed()
    with main.db() as c:
        c.execute("UPDATE comments SET status = 'done' WHERE id = 'b'")
        c.execute("UPDATE comments SET status = 'skipped' WHERE id = 'c2'")
    html = client.get("/round/r2").text
    assert re.search(r'data-action="ship"(?![^>]*disabled)', html)


def test_direct_stream_has_no_ship_button(client):
    _seed()
    bid = client.post("/api/projects/p1/issues", json={"title": "x", "page": "/"}).json()["batch_id"]
    html = client.get(f"/round/{bid}").text
    assert "Your issues" in html and "Standing stream" in html
    assert 'data-action="ship"' not in html


def test_unknown_round_is_404(client):
    assert client.get("/round/nope").status_code == 404
```
Run → FAIL (404 on `/round/...`).

- [ ] **Step 2: Implement the route (inside `make_dashboard_router`, before `return router`)**

```python
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
            label = "Your issues" if b["kind"] == "direct" else "Round " + str(c.execute(
                "SELECT COUNT(*) FROM batches WHERE kind = 'export' AND ingested_at <= ? "
                "AND (project_id IS ? OR id = ?)", (b["ingested_at"], b["project_id"], bid)).fetchone()[0])
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
```

- [ ] **Step 3: Templates**

`templates/partials/comment_card.html`:
```html
<div class="ccard status-{{ c.status }}">
  <div class="ccard-top">
    <a class="mono small" href="/comment/{{ c.id }}">#{{ c.id[:8] }}</a>
    {% if c.reviewer == "You" %}<span class="chip dark">Your issue</span>{% endif %}
    <span class="chip st-{{ c.status }}">{{ c.status }}</span>
  </div>
  <div>{{ c.comment }}</div>
  {% if c.repeat_kind %}<div class="chip bad">{{ "Regression" if c.repeat_kind == "regression" else "Fix didn't take" }} of <a href="/comment/{{ c.repeat_of }}">#{{ c.repeat_of[:8] }}</a></div>{% endif %}
  {% if c.status_note %}<div class="small muted">{{ c.status_note }}</div>{% endif %}
  <div class="small mono muted">{{ c.selector or "no pin" }}{% if c.viewport_w %} · {{ c.viewport_w }}px{% endif %}{% if c.commit_sha %} · {{ c.commit_sha[:7] }}{% endif %}</div>
  <div class="ccard-actions">{% for a in c.actions %}<button class="btn small {{ a.kind }}" data-action="status" data-id="{{ c.id }}" data-from="{{ a.from }}" data-to="{{ a.to }}">{{ a.label }}</button>{% endfor %}</div>
  <div class="small" data-msg="{{ c.id }}"></div>
</div>
```
`templates/round.html`:
```html
{% extends "layout.html" %}
{% block title %}{{ label }} — Prototype Feedback{% endblock %}
{% block content %}
<p class="breadcrumb"><a href="/">Dashboard</a> → {{ project.name if project else "Unassigned" }} → {{ label }}</p>
<div class="dash-head">
  <div>
    <h1>{{ project.name if project else "Unassigned" }} · {{ label }} <span class="chip {{ state_class }}">{{ state }}</span></h1>
    <p class="muted small">{{ batch.reviewer or "—" }} · {{ batch.ingested_at[:10] }} · {{ llm_count }} LLM, {{ human|length }} human{% if project and project.preview_url %} · preview <a href="{{ project.preview_url }}" target="_blank" class="mono">{{ project.preview_url }}</a>{% endif %}</p>
  </div>
  <div class="dash-actions">
    {% if batch.kind == "direct" %}
      <span class="small muted">Standing stream: your issues ship with a round, never on their own.</span>
    {% else %}
      <button class="btn" disabled title="Needs Plan D (resolutions export)">Send for re-review</button>
      <button class="btn primary" data-action="ship" data-batch="{{ batch.id }}" {% if not can_ship or batch.shipped_at %}disabled{% endif %}>{% if batch.shipped_at %}Shipped{% else %}Ship to main{% endif %}</button>
      <div class="small muted">{% if not can_ship and not batch.shipped_at %}Unlocks when every LLM item is Done or Skipped · {% endif %}re-review needs Plan D</div>
      <div class="small" data-msg="ship"></div>
    {% endif %}
  </div>
</div>
<section class="lanes">
{% for lane in lanes %}
  <div class="lane"><h2 class="mono">{{ lane.page }}</h2>
  {% for c in lane.comments %}{% include "partials/comment_card.html" %}{% endfor %}
  </div>
{% else %}
  <p class="muted">No LLM items in this round.</p>
{% endfor %}
</section>
{% if human %}
<h2>For you, not for Claude · {{ human|length }}</h2>
<section class="lanes">{% for c in human %}{% include "partials/comment_card.html" %}{% endfor %}</section>
{% endif %}
<script src="/static/dashboard.js"></script>
{% endblock %}
```

- [ ] **Step 4: GREEN + full suite** → 0 failed. **Step 5: Commit** — `git add dashboard.py templates/round.html templates/partials/comment_card.html tests/test_pages.py && git commit -m "feat(ui): round view with page lanes, human section, gated ship"`

---

### Task 7: Confirm-a-fix panel on the comment page

**Files:** `main.py` (`comment_detail`), `templates/comment.html` (one include line), `templates/partials/comment_queue.html`. Test: `tests/test_pages.py`.

- [ ] **Step 1: Failing tests (append)**

```python
def test_comment_page_offers_confirm_actions_and_keeps_old_content(client):
    _seed()
    html = client.get("/comment/b").text
    assert 'data-id="b" data-from="verified" data-to="done"' in html
    assert "no after-shot yet — verify arrives in Plan C" in html
    assert "Claude prompt for the whole batch" in html      # pre-existing section kept


def test_comment_page_history_and_human_actions(client):
    _seed()
    with main.db() as c:
        c.execute("INSERT INTO status_events (id, comment_id, at, from_status, to_status, actor, note) "
                  "VALUES ('e1', 'h', '2026-10-01T10:00:00+00:00', NULL, 'new', 'user', 'ingested')")
    html = client.get("/comment/h").text
    assert "History" in html and "ingested" in html
    assert 'data-from="new" data-to="done"' in html
    assert 'data-to="queued"' not in html                  # human items are never queued
```
Run → FAIL.

- [ ] **Step 2: Implement**

In `main.py`, add `import feedback_views` next to the other feedback imports. In `comment_detail`, inside the `with db() as c:` block after `batch = ...`, add:
```python
        history = c.execute(
            "SELECT * FROM status_events WHERE comment_id = ? ORDER BY at", (comment_id,)
        ).fetchall()
```
and add `"history": history, "actions": feedback_views.actions_for(row["status"], row["recipient"]),` to the template context dict.

In `templates/comment.html`, insert directly after the closing `</p>` of the breadcrumb (after line 9):
```html
{% include "partials/comment_queue.html" %}
```
`templates/partials/comment_queue.html`:
```html
<section class="queue-panel">
  <div class="dash-head">
    <div>
      <span class="chip st-{{ row.status }}">{{ row.status }}</span>
      <span class="small muted">recipient: {{ row.recipient }}</span>
      {% if row.repeat_kind %}<span class="chip bad">{{ "Regression" if row.repeat_kind == "regression" else "Fix didn't take" }} of <a href="/comment/{{ row.repeat_of }}">#{{ row.repeat_of[:8] }}</a></span>{% endif %}
    </div>
    <div class="ccard-actions">{% for a in actions %}<button class="btn {{ a.kind }}" data-action="status" data-id="{{ row.id }}" data-from="{{ a.from }}" data-to="{{ a.to }}">{{ a.label }}</button>{% endfor %}</div>
  </div>
  <div class="small" data-msg="{{ row.id }}"></div>
  {% if row.recipient == "llm" %}
  <div class="grid2">
    <div><div class="card-k">Expected result</div><div>{{ row.expected_result or "—" }}</div></div>
    <div><div class="card-k">Automatic check</div>
      <div class="small">{% if row.verified_at %}verified {{ row.verified_at[:16].replace("T", " ") }}{% if row.verify_selector %} · found by <code>{{ row.verify_selector }}</code>{% endif %}{% else %}not run yet{% endif %}</div></div>
  </div>
  <div class="grid2">
    <figure><figcaption class="small">Before · reviewer's crop</figcaption>
      {% if row.screenshot_path %}<img src="/file/{{ row.screenshot_path }}" alt="before">{% else %}<div class="placeholder">no before-shot (no pin)</div>{% endif %}</figure>
    <figure><figcaption class="small">After · preview capture</figcaption>
      {% if row.after_path %}<img src="/file/{{ row.after_path }}" alt="after">{% else %}<div class="placeholder">no after-shot yet — verify arrives in Plan C</div>{% endif %}</figure>
  </div>
  {% endif %}
  {% if history %}
  <h3>History</h3>
  <ol class="history">{% for h in history %}<li><span class="mono small">{{ h.at[:16].replace("T", " ") }}</span> {{ h.from_status or "—" }} → <strong>{{ h.to_status }}</strong> <span class="muted small">{{ h.actor }}{% if h.note %} · {{ h.note }}{% endif %}</span></li>{% endfor %}</ol>
  {% endif %}
</section>
<script src="/static/dashboard.js"></script>
```

- [ ] **Step 3: GREEN + full suite.** **Step 4: Commit** — `git add main.py templates/comment.html templates/partials/comment_queue.html tests/test_pages.py && git commit -m "feat(ui): confirm-a-fix panel on the comment page"`

---

### Task 8: New-issue composer + repeat check

**Files:** `feedback_api.py` (endpoint), `dashboard.py` (`/new-issue`), `templates/new_issue.html`. Tests: `tests/test_repeat_check.py`, `tests/test_pages.py`.

- [ ] **Step 1: Failing tests**

`tests/test_repeat_check.py`:
```python
import main


def _seed():
    with main.db() as c:
        c.execute("INSERT INTO projects (id, created_at, updated_at, name, repo) VALUES ('p1','t','t','P','r/a')")
        c.execute("INSERT INTO batches (id, ingested_at, project_id, kind, shipped_at) VALUES ('old','t','p1','export','2026-09-01')")
        c.execute("INSERT INTO batches (id, ingested_at, project_id, kind) VALUES ('new','t','p1','export')")
        c.execute("INSERT INTO comments (id, batch_id, project_id, status, page_url, selector, bbox_json) "
                  "VALUES ('prior','old','p1','done','https://x.dev/','footer img','{}')")
        c.execute("INSERT INTO comments (id, batch_id, project_id, status, page_url, selector, bbox_json) "
                  "VALUES ('pin','new','p1','new','https://x.dev/?a=1','footer img','{}')")


def test_pin_matching_a_shipped_done_comment_is_a_regression(client):
    _seed()
    r = client.get("/api/projects/p1/repeat-check", params={"pin_comment_id": "pin"}).json()
    assert r["repeat"]["id"] == "prior" and r["repeat"]["kind"] == "regression"


def test_a_done_pin_does_not_match_itself(client):
    _seed()
    r = client.get("/api/projects/p1/repeat-check", params={"pin_comment_id": "prior"}).json()
    assert r["repeat"] is None


def test_unknown_pin_is_404(client):
    _seed()
    assert client.get("/api/projects/p1/repeat-check", params={"pin_comment_id": "nope"}).status_code == 404
```
Append to `tests/test_pages.py`:
```python
def test_composer_lists_projects_pins_and_flags_unmapped(client):
    _seed()
    with main.db() as c:
        c.execute("UPDATE comments SET selector = 'button.queue' WHERE id = 'c2'")
    html = client.get("/new-issue?project=p1").text
    assert "abdry" in html and "sunsip" in html
    assert 'value="c2"' in html and "button.queue" in html        # pin picker
    assert 'data-ok="1"' in html
    assert re.search(r"<button[^>]*disabled[^>]*>Draft it</button>", html)
    assert 'data-ok="0"' in client.get("/new-issue?project=p2").text
```
Run → FAIL.

- [ ] **Step 2: Repeat-check endpoint (inside `make_router`, before `router.change_status = ...`)**

```python
    @router.get("/api/projects/{pid}/repeat-check")
    def api_repeat_check(pid: str, pin_comment_id: str):
        with db() as c:
            pin = c.execute("SELECT page_url, selector, bbox_json FROM comments WHERE id = ?",
                            (pin_comment_id,)).fetchone()
            if pin is None:
                raise HTTPException(404, "pin comment not found")
            hit = fs.classify_repeat(
                {"page": fs.normalize_path(pin["page_url"]), "selector": pin["selector"],
                 "bbox": json.loads(pin["bbox_json"] or "{}")},
                done_priors(c, pid, exclude_id=pin_comment_id))
            if hit is None:
                return {"repeat": None}
            prior = c.execute("SELECT comment FROM comments WHERE id = ?", (hit[0],)).fetchone()
        return {"repeat": {"id": hit[0], "kind": hit[1], "comment": prior["comment"] if prior else None}}
```

- [ ] **Step 3: Composer route (in `dashboard.py`)**

```python
    @router.get("/new-issue", response_class=HTMLResponse)
    def new_issue(request: Request, project: Optional[str] = None):
        with db() as c:
            projects = [dict(p) for p in c.execute(
                "SELECT id, name, repo FROM projects WHERE archived = 0 ORDER BY name")]
            pid = project if any(p["id"] == project for p in projects) else (projects[0]["id"] if projects else None)
            pins, pages = [], []
            if pid:
                pins = [dict(r) for r in c.execute(
                    """SELECT cm.id, cm.selector, cm.page_url FROM comments cm
                       JOIN batches b ON b.id = cm.batch_id
                       WHERE cm.project_id = ? AND cm.selector IS NOT NULL AND b.kind = 'export'
                       ORDER BY cm.created_at DESC LIMIT 10""", (pid,))]
                pages = sorted({fs.normalize_path(r["page_url"]) for r in c.execute(
                    "SELECT page_url FROM comments WHERE project_id = ?", (pid,))})[:8]
        current = next((p for p in projects if p["id"] == pid), {})
        return templates.TemplateResponse("new_issue.html", {
            "request": request, "projects": projects, "pid": pid, "current": current,
            "pins": pins, "pages": pages})
```

- [ ] **Step 4: `templates/new_issue.html`**

```html
{% extends "layout.html" %}
{% block title %}New issue — Prototype Feedback{% endblock %}
{% block content %}
<p class="breadcrumb"><a href="/">Dashboard</a> → New issue</p>
<h1>New issue</h1>
{% if not projects %}
<p class="muted">Create a project first under <a href="/projects">Projects</a>.</p>
{% else %}
<form id="new-issue" class="sheet" data-project="{{ pid }}">
  <label>Project
    <select name="project">{% for p in projects %}<option value="{{ p.id }}" {% if p.id == pid %}selected{% endif %}>{{ p.name }}</option>{% endfor %}</select>
    <span class="small {{ 'ok' if current.repo else 'err' }}" data-check-src="repo" data-ok="{{ 1 if current.repo else 0 }}">{{ ("repo mapped · " ~ current.repo) if current.repo else "no repo mapped: map it on the project page first" }}</span>
  </label>
  <label>What's wrong or what should change<input name="title"></label>
  <fieldset><legend>Where</legend>
    <input name="page" value="/" class="mono" aria-label="Page path">
    <select name="viewport_w" aria-label="Viewport">
      <option value="1440">Desktop · 1440</option><option value="768">Tablet · 768</option><option value="390">Phone · 390</option>
    </select>
    <div class="chips">{% for pg in pages %}<button type="button" class="btn small mono" data-action="pick-page" data-page="{{ pg }}">{{ pg }}</button>{% endfor %}</div>
    <div class="small muted">Attach one of your pins (pins from the extension arrive with Plan D)</div>
    {% for pin in pins %}<label class="pin"><input type="radio" name="pin" value="{{ pin.id }}"> <span class="mono small">{{ pin.selector }}</span> <span class="small muted">{{ pin.page_url }}</span></label>{% endfor %}
    <label class="pin"><input type="radio" name="pin" value="" checked> No pin <span class="small muted">full-page check only</span></label>
  </fieldset>
  <div class="small" data-msg="repeat"></div>
  <label>Details <span class="muted small">optional</span><textarea name="details" rows="2"></textarea></label>
  <label>Expected result <span class="muted small">required for Claude</span>
    <textarea name="expected_result" rows="2" placeholder="What you will see once it is fixed"></textarea>
  </label>
  <div><button type="button" class="btn small" disabled title="Needs an LLM call from the server; not wired yet">Draft it</button></div>
  <div class="card"><div class="card-k">What gets checked</div><div class="small" data-what-checked></div></div>
  <fieldset><legend>Who acts on it</legend>
    <label class="pin"><input type="radio" name="recipient" value="llm" checked> Claude <span class="small muted">queueable; fixed by /feedback run</span></label>
    <label class="pin"><input type="radio" name="recipient" value="human"> A person <span class="small muted">never queued; shows in Needs you</span></label>
  </fieldset>
  <ul class="checks" data-checks></ul>
  <div>
    <button type="submit" class="btn" name="mode" value="new">Save as New</button>
    <button type="submit" class="btn primary" name="mode" value="queue" data-queue-button disabled>Save &amp; queue</button>
  </div>
  <div class="small" data-msg="save"></div>
</form>
{% endif %}
<script src="/static/dashboard.js"></script>
{% endblock %}
```

- [ ] **Step 5: GREEN + full suite.** **Step 6: Commit** — `git add feedback_api.py dashboard.py templates/new_issue.html tests/test_repeat_check.py tests/test_pages.py && git commit -m "feat(ui): new-issue composer + pin repeat check"`

---

### Task 9: Repo-mapping form on the project page

**Files:** `main.py` (`project_detail` context), `templates/project.html`. Test: `tests/test_pages.py`.

- [ ] **Step 1: Failing test (append)**

```python
def test_project_page_has_mapping_form_with_current_values(client):
    _seed()
    with main.db() as c:
        c.execute("UPDATE projects SET url_prefixes_json = ? WHERE id = 'p1'", ('["https://x.dev", "https://y.dev"]',))
    html = client.get("/project/p1").text
    assert 'id="mapping"' in html and 'value="r/a"' in html
    assert "https://x.dev\nhttps://y.dev" in html
```
Run → FAIL.

- [ ] **Step 2: Implement**

In `project_detail`, add `"url_prefixes": json.loads((summary["project"]["url_prefixes_json"] if summary["project"]["url_prefixes_json"] else "[]")),` to the context. If `summary["project"]` is a plain dict without that key, read it with `.get`. Check `_project_summary`'s return shape first and adapt the one expression.

In `templates/project.html`, insert immediately before `<h2>Reviewers</h2>`:
```html
<h2 id="mapping">Repo mapping</h2>
<form id="repo-mapping" class="sheet" data-project="{{ summary.project.id }}">
  <label>Repo <input name="repo" value="{{ summary.project.repo or '' }}" placeholder="leslurb/my-prototype"></label>
  <label>Local path <input name="local_path" value="{{ summary.project.local_path or '' }}" class="mono"></label>
  <label>URL prefixes <span class="small muted">one per line; the target URL is always included</span>
    <textarea name="url_prefixes" rows="3" class="mono">{{ url_prefixes|join("\n") }}</textarea></label>
  <label>Preview command <input name="preview_cmd" value="{{ summary.project.preview_cmd or '' }}" class="mono"></label>
  <label>Preview URL <input name="preview_url" value="{{ summary.project.preview_url or '' }}" class="mono"></label>
  <div><button class="btn primary" type="submit">Save mapping</button> <span class="small" data-msg="mapping"></span></div>
</form>
<script src="/static/dashboard.js"></script>
```

- [ ] **Step 3: GREEN + full suite.** **Step 4: Commit** — `git add main.py templates/project.html tests/test_pages.py && git commit -m "feat(ui): repo-mapping form on the project page"`

---

### Task 10: `static/dashboard.js`

**Files:** create `static/dashboard.js`. There's no unit test surface (no JS toolchain, by decision Q1). Task 11 verifies it in a real browser.

- [ ] **Step 1: Write the file**

```js
// Dashboard interactions. Every action is one call to the work-queue API; the server
// enforces the rules (409 stale / 422 not allowed) and this file reports them verbatim.
(function () {
  if (window.__pfbDashboard) return;           // included by several partials on one page
  window.__pfbDashboard = true;

  async function api(method, url, body) {
    const opt = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opt.body = JSON.stringify(body);
    const r = await fetch(url, opt);
    let json = null;
    try { json = await r.json(); } catch (e) { /* non-JSON error body */ }
    return { ok: r.ok, status: r.status, json };
  }
  function say(key, text, isErr) {
    const el = document.querySelector(`[data-msg="${key}"]`);
    if (el) { el.textContent = text; el.className = "small " + (isErr ? "err" : "ok"); }
  }
  function detail(res) {
    const d = res.json && res.json.detail;
    if (!d) return `HTTP ${res.status}`;
    if (typeof d === "string") return d;
    if (d.current) return `Changed elsewhere: it is now ${d.current}. Reload.`;
    return JSON.stringify(d);
  }

  // ---- dashboard selection ------------------------------------------------
  function selectedRounds() { return [...document.querySelectorAll('input[name="round"]:checked')]; }
  function refreshQueueButton() {
    const btn = document.querySelector('[data-action="queue-selected"]');
    if (!btn) return;
    const n = selectedRounds().reduce((s, el) => s + Number(el.dataset.new || 0), 0);
    btn.disabled = n === 0;
    btn.textContent = n ? `Queue ${n} & copy` : "Select to queue";
  }

  // ---- composer -------------------------------------------------------------
  function composerState(f) {
    const llm = f.elements.recipient.value === "llm";
    const repo = f.querySelector('[data-check-src="repo"]').dataset.ok === "1";
    const checks = [
      ["repo mapped", repo],
      ["what", f.elements.title.value.trim() !== ""],
      ["where", f.elements.page.value.trim() !== ""],
      ["expected result", !llm || f.elements.expected_result.value.trim() !== ""],
    ];
    return { llm, checks, ready: llm && checks.every((c) => c[1]) };
  }
  function composerChanged(t) {
    const f = document.getElementById("new-issue");
    if (!f) return;
    if (t && t.name === "project") { location.href = `/new-issue?project=${encodeURIComponent(t.value)}`; return; }
    const st = composerState(f);
    f.querySelector("[data-checks]").innerHTML = st.checks
      .map(([label, ok]) => `<li class="${ok ? "ok" : "err"}">${ok ? "✓" : "✗"} ${label}</li>`).join("");
    f.querySelector("[data-queue-button]").disabled = !st.ready;
    const pin = f.elements.pin.value;
    const target = pin
      ? f.querySelector(`input[name="pin"][value="${CSS.escape(pin)}"]`).parentElement.querySelector(".mono").textContent
      : "the whole page";
    f.querySelector("[data-what-checked]").textContent = st.llm
      ? `Automatic: once the preview build matches the branch, open ${f.elements.page.value} at ${f.elements.viewport_w.value}px, find ${target}, capture an after-shot. You: compare against the expected result, then mark it Done.`
      : "Not checked by Claude. You mark it done yourself.";
    if (t && t.name === "pin") repeatCheck(f, pin);
  }
  async function repeatCheck(f, pin) {
    if (!pin) { say("repeat", ""); return; }
    const res = await api("GET", `/api/projects/${f.dataset.project}/repeat-check?pin_comment_id=${encodeURIComponent(pin)}`);
    if (!res.ok) { say("repeat", detail(res), true); return; }
    const r = res.json.repeat;
    say("repeat", r ? `${r.kind === "regression" ? "Regression" : "Fix didn't take"} of #${r.id.slice(0, 8)}: ${r.comment || ""}` : "", !!r);
  }

  document.addEventListener("change", (ev) => {
    const t = ev.target;
    if (t.dataset && t.dataset.projectToggle) {
      document.querySelectorAll(`input[name="round"][data-project="${CSS.escape(t.dataset.projectToggle)}"]:not(:disabled)`)
        .forEach((el) => { el.checked = t.checked; });
    }
    if (t.name === "round" || (t.dataset && t.dataset.projectToggle)) refreshQueueButton();
    if (t.closest && t.closest("#new-issue")) composerChanged(t);
  });
  document.addEventListener("input", (ev) => {
    if (ev.target.closest && ev.target.closest("#new-issue")) composerChanged(ev.target);
  });

  // ---- click actions ----------------------------------------------------------
  document.addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-action]");
    if (!b) return;
    const a = b.dataset.action;
    if (a === "status") {
      b.disabled = true;
      const res = await api("POST", `/api/comment/${b.dataset.id}/status`,
        { to: b.dataset.to, expected_from: b.dataset.from, actor: "user" });
      if (res.ok) location.reload(); else { say(b.dataset.id, detail(res), true); b.disabled = false; }
    } else if (a === "queue-selected") {
      b.disabled = true;
      const res = await api("POST", "/api/comments/queue", { batch_ids: selectedRounds().map((el) => el.value) });
      if (!res.ok) { say("queue", detail(res), true); b.disabled = false; return; }
      let copied = true;
      try { await navigator.clipboard.writeText("/feedback run"); } catch (e) { copied = false; }
      const sk = res.json.skipped || [];
      say("queue", `Queued ${res.json.queued.length}`
        + (sk.length ? `, skipped ${sk.length} (${[...new Set(sk.map((s) => s.reason))].join(", ")})` : "")
        + (copied ? " · /feedback run copied" : " · copy failed: type /feedback run"), sk.length > 0);
      setTimeout(() => location.reload(), 1500);
    } else if (a === "ship") {
      const res = await api("POST", `/api/rounds/${b.dataset.batch}/mark`, { what: "shipped" });
      if (res.ok) location.reload(); else say("ship", detail(res), true);
    } else if (a === "remind") {
      const res = await api("POST", `/api/projects/${b.dataset.project}/remind`, { draft_only: true });
      say(`remind-${b.dataset.project}`, res.ok ? "Reminder drafted in Gmail" : detail(res), !res.ok);
    } else if (a === "pick-page") {
      const f = document.getElementById("new-issue");
      f.elements.page.value = b.dataset.page;
      composerChanged(f.elements.page);
    }
  });

  const form = document.getElementById("new-issue");
  if (form) {
    composerChanged(null);
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const e = form.elements;
      const body = {
        title: e.title.value, page: e.page.value, recipient: e.recipient.value,
        details: e.details.value || null, viewport_w: Number(e.viewport_w.value),
        expected_result: e.expected_result.value || null, pin_comment_id: e.pin.value || null,
        queue: !!(ev.submitter && ev.submitter.value === "queue"),
      };
      const res = await api("POST", `/api/projects/${form.dataset.project}/issues`, body);
      if (res.ok) location.href = `/round/${res.json.batch_id}`; else say("save", detail(res), true);
    });
  }
  const mapping = document.getElementById("repo-mapping");
  if (mapping) {
    mapping.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const e = mapping.elements;
      const res = await api("POST", `/api/projects/${mapping.dataset.project}`, {
        repo: e.repo.value || null, local_path: e.local_path.value || null,
        url_prefixes: e.url_prefixes.value.split("\n").map((s) => s.trim()).filter(Boolean),
        preview_cmd: e.preview_cmd.value || null, preview_url: e.preview_url.value || null,
      });
      say("mapping", res.ok ? "Saved" : detail(res), !res.ok);
    });
  }
  refreshQueueButton();
})();
```

- [ ] **Step 2: Verify it is served** — add to `tests/test_pages.py`:
```python
def test_dashboard_js_is_served(client):
    r = client.get("/static/dashboard.js")
    assert r.status_code == 200 and "data-action" in r.text
```
Run the full suite → 0 failed.

- [ ] **Step 3: Commit** — `git add static/dashboard.js tests/test_pages.py && git commit -m "feat(ui): dashboard.js (selection, queue & copy, status actions, composer, mapping)"`

---

### Task 11: End-to-end in a real browser

The controller does this task with Chrome automation (claude-in-chrome) on a scratch data dir. The real `data/` is never used.

- [ ] **Step 1: Start a scratch server** — PowerShell: `$env:PFB_DATA_DIR = "$env:TEMP\pfb-ui-e2e"; .venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8093` (in the background). Wait for `GET http://127.0.0.1:8093/` to return 200.

- [ ] **Step 2: Seed through the API (curl)**
  - Create project `demo` and map it (`repo: leslurb/prototype-feedback-server`, `local_path: C:/Users/sebas/prototype-feedback-server`).
  - Create project `unmapped`.
  - Ingest one JSON export into `demo` with 2 LLM comments (pages `/` and `/products`) and 1 human comment.
  - Ingest one export into `unmapped`.
  - Mark one of demo's LLM comments `verified` through `POST /api/comment/{id}/status` (queued → doing → verified, actor `drain`).

- [ ] **Step 3: Golden path in Chrome** (record a GIF, `pfb_dashboard_e2e.gif`):
  1. `/`: demo's round is listed. Ticking it changes the button to **Queue 1 & copy**. Clicking it shows "Queued 1 · /feedback run copied", and after the reload the round chip reads **Queued**. The `unmapped` round's checkbox is disabled and a **No repo mapped** card is shown.
  2. Open the round: two lanes (`/`, `/products`) and the human section are shown, and **Ship to main** is disabled.
  3. Open the verified comment: **Looks fixed: mark Done** works, the history shows the move, and the after panel shows the Plan C placeholder.
  4. Concurrency: click a stale action (e.g. **Unqueue** on a comment changed in another tab). The 409 text "Changed elsewhere: it is now …" appears inline, and nothing breaks.
  5. `/new-issue?project=<demo>`: the checklist turns ✓ as fields fill. **Save & queue** stays disabled until Expected result is filled, then saves and lands on the **Your issues** round with the issue **Queued**.
  6. Project page: saving the mapping with an empty URL-prefix box succeeds, and the dashboard reflects the mapping.
  7. The browser console shows no errors.

- [ ] **Step 4: Stop the server** (exact PID), confirm port 8093 is free, and run the full suite once more → 0 failed.
