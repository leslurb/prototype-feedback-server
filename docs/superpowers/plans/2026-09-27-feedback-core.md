# Feedback Work Queue — Plan A: Server Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give prototype-feedback-server a per-comment work-queue model (statuses, allowed transitions, repo mapping, repeat detection, runs, direct issues) and the HTTP API that the dashboard (Plan B), verify (Plan C) and `/feedback run` (Plan E) are built on.

**Non-goals:**
- No dashboard HTML/templates (Plan B). The existing `/` page is untouched.
- No Playwright and no verify endpoint (Plan C). This plan only stores what verify will write (`after_path`, `verified_at`, `verify_json`).
- No extension changes (Plan D) and no `/feedback` skill or Scheduled Task (Plan E).
- No change to the existing ingest file handling, reports, email or OAuth code paths.

**Architecture:** Pure rules live in `feedback_state.py` (no I/O, unit-tested directly). Schema migrations live in `feedback_schema.py`, called from the existing `init_db()`. Endpoints live in `feedback_api.py` as `make_router(db, now_iso)`. `main.py` passes in its own helpers and mounts the router, so nothing imports `main` (this avoids a circular import and a second `init_db()` when run as `__main__`). Every status write is conditional (`expected_from`) and goes through the transition table. A stale write returns 409 and a disallowed move returns 422.

**Tech Stack:** Python 3.13, FastAPI 0.115, sqlite3, pytest + httpx (FastAPI TestClient).

**Assumptions / Preconditions:**
- Repo `C:\Users\sebas\prototype-feedback-server`, branch `main`, clean working tree (checked 2026-09-27). Use its `.venv` (`.venv\Scripts\python.exe`, Python 3.13.14).
- `main.py` calls `init_db()` AT IMPORT against `DATA = <repo>/data`, so importing `main` from a test would migrate the REAL `data/feedback.db`. Task 0 makes `DATA` overridable with `PFB_DATA_DIR`, and every test sets it BEFORE importing `main`. (memory: feedback_module_scope_init_db_migrates_production)
- The extension exports `recipient_resolved` ∈ {`llm`,`human`} (default `llm`, background.js:397), `bbox` = `{x,y,w,h}`, `viewport` = `{w,h,scrollX,scrollY}`, and top-level `repo_hints` = `{url_prefix: {repo, local_path?}}`. None of these except bbox/viewport are stored today.
- "Round" = one `batches` row. Direct issues (the user's own) go into ONE batch per project with `kind='direct'`.
- Design source: memory `project_prototype_feedback_dashboard_design.md` and canvas https://claude.ai/artifact/7f7Qyh1sXG5fn9snG2xbpw.

**Rollback:** Code-only plus additive schema: new columns and tables are only added, never dropped or rewritten, so an old `main.py` still runs against a migrated DB. To undo: `git revert` the plan's commits. Task 0 step 0 backs up `data\feedback.db` before any test runs, and the test harness refuses to wipe any DB outside its temp dir.

---

## File structure

| File | Responsibility |
|---|---|
| `feedback_state.py` (new) | Pure rules: statuses, transition table, round state, path normalisation, repo resolution, bbox IoU, repeat classification |
| `feedback_schema.py` (new) | Additive migrations for the new columns and tables |
| `feedback_api.py` (new) | `make_router(db, now_iso)`: queue, status, runs, overview, direct issues, round marks, project mapping |
| `main.py` (modify) | `PFB_DATA_DIR` override; call `migrate()`; ingest stores recipient/hints/repeat; mount the router; mapping fields on project update |
| `requirements-dev.txt` (new) | pytest, httpx |
| `tests/conftest.py` (new) | Temp data dir BEFORE import, per-test table wipe, `client` fixture |
| `tests/test_*.py` (new) | One file per task group |

---

### Task 0: Isolated test harness

**Files:**
- Modify: `main.py:34-37`
- Create: `requirements-dev.txt`, `tests/__init__.py`, `tests/conftest.py`, `tests/test_isolation.py`

- [ ] **Step 0: Back up the real DB (before any test ever runs)**

Run (cmd): `copy data\feedback.db data\feedback.db.pre-plan-a`
Expected: `1 file(s) copied.`

- [ ] **Step 1: Install dev deps**

`requirements-dev.txt`:
```
-r requirements.txt
pytest==8.3.3
httpx==0.27.2
```
Run: `.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`
Expected: ends with `Successfully installed ... httpx-0.27.2 ... pytest-8.3.3` (or "Requirement already satisfied").

- [ ] **Step 2: Write the failing isolation test**

`tests/__init__.py`: empty file.

`tests/conftest.py`:
```python
import os
import tempfile
from pathlib import Path

# MUST run before anything imports main: main.init_db() runs at import time.
_TMP = Path(tempfile.mkdtemp(prefix="pfb-test-"))
os.environ["PFB_DATA_DIR"] = str(_TMP)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

TABLES = ["status_events", "attachments", "comments", "batches", "project_reviewers",
          "email_log", "projects", "runs"]


@pytest.fixture(autouse=True)
def _clean_db():
    # Never wipe a database that is not the throwaway test one.
    assert "pfb-test-" in str(main.DB_PATH), f"refusing to wipe non-test DB {main.DB_PATH}"
    with main.db() as c:
        existing = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in TABLES:
            if t in existing:
                c.execute(f"DELETE FROM {t}")
    yield


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def tmp_data_dir():
    return _TMP
```

`tests/test_isolation.py`:
```python
import main


def test_tests_never_touch_the_real_db(tmp_data_dir):
    # Regression: main.init_db() runs at import; without PFB_DATA_DIR the
    # suite would migrate and wipe the production data/feedback.db.
    assert main.DB_PATH.parent.resolve() == tmp_data_dir.resolve()
    assert "pfb-test-" in str(main.DB_PATH)
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_isolation.py -q`
Expected: ERROR in fixture `_clean_db`: `refusing to wipe non-test DB ...\data\feedback.db`. That is the guard working. Nothing is deleted, and importing `main` only runs the existing idempotent `CREATE TABLE IF NOT EXISTS` statements.

- [ ] **Step 4: Make DATA overridable**

`main.py`, replace `_get_data_dir`:
```python
def _get_data_dir():
    override = os.environ.get("PFB_DATA_DIR")
    if override:
        return Path(override)
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent / "data"
    return Path(__file__).parent / "data"
```
and add `import os` to the stdlib imports (after `import json`).

- [ ] **Step 5: Run it to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_isolation.py -q`
Expected: `1 passed`.

- [ ] **Step 6: Commit**

```bash
git add main.py requirements-dev.txt tests/__init__.py tests/conftest.py tests/test_isolation.py
git commit -m "test: isolated pytest harness (PFB_DATA_DIR) so tests never touch data/feedback.db"
```

---

### Task 1: Schema migration

**Files:**
- Create: `feedback_schema.py`, `tests/test_schema.py`
- Modify: `main.py` `init_db()` (currently lines 165-178)

- [ ] **Step 1: Write the failing test**

`tests/test_schema.py`:
```python
import main

COMMENT_COLS = {"status", "recipient", "expected_result", "verify_selector", "viewport_w",
                "lane", "run_id", "commit_sha", "after_path", "verified_at", "verify_json",
                "repeat_of", "repeat_kind", "status_note", "updated_at"}
BATCH_COLS = {"kind", "repo_hints_json", "shipped_at", "rereview_sent_at"}
PROJECT_COLS = {"repo", "local_path", "url_prefixes_json", "preview_cmd", "preview_url"}


def _cols(c, table):
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}


def test_migration_adds_columns_and_tables():
    with main.db() as c:
        assert COMMENT_COLS <= _cols(c, "comments")
        assert BATCH_COLS <= _cols(c, "batches")
        assert PROJECT_COLS <= _cols(c, "projects")
        assert {"id", "started_at", "heartbeat_at", "ended_at", "cap", "status", "summary"} <= _cols(c, "runs")
        assert {"id", "comment_id", "at", "from_status", "to_status", "actor", "note", "run_id"} <= _cols(c, "status_events")


def test_migration_is_idempotent_and_defaults_are_safe():
    main.init_db()  # second run must not raise
    with main.db() as c:
        c.execute("INSERT INTO batches (id, ingested_at) VALUES ('b1', 'now')")
        c.execute("INSERT INTO comments (id, batch_id) VALUES ('c1', 'b1')")
        row = c.execute("SELECT status, recipient FROM comments WHERE id='c1'").fetchone()
        kind = c.execute("SELECT kind FROM batches WHERE id='b1'").fetchone()["kind"]
    assert (row["status"], row["recipient"], kind) == ("new", "llm", "export")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_schema.py -q`
Expected: FAIL on `assert COMMENT_COLS <= ...`.

- [ ] **Step 3: Implement**

`feedback_schema.py`:
```python
"""Additive migrations for the feedback work queue. Only ADD columns/tables:
an older main.py must keep running against a migrated database."""
from __future__ import annotations

import sqlite3

NEW_COLUMNS = {
    "comments": [
        ("status", "TEXT NOT NULL DEFAULT 'new'"),
        ("recipient", "TEXT NOT NULL DEFAULT 'llm'"),
        ("expected_result", "TEXT"),
        ("verify_selector", "TEXT"),
        ("viewport_w", "INTEGER"),
        ("lane", "TEXT"),
        ("run_id", "TEXT"),
        ("commit_sha", "TEXT"),
        ("after_path", "TEXT"),
        ("verified_at", "TEXT"),
        ("verify_json", "TEXT"),
        ("repeat_of", "TEXT"),
        ("repeat_kind", "TEXT"),
        ("status_note", "TEXT"),
        ("updated_at", "TEXT"),
    ],
    "batches": [
        ("kind", "TEXT NOT NULL DEFAULT 'export'"),
        ("repo_hints_json", "TEXT"),
        ("shipped_at", "TEXT"),
        ("rereview_sent_at", "TEXT"),
    ],
    "projects": [
        ("repo", "TEXT"),
        ("local_path", "TEXT"),
        ("url_prefixes_json", "TEXT"),
        ("preview_cmd", "TEXT"),
        ("preview_url", "TEXT"),
    ],
}

NEW_TABLES = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    ended_at TEXT,
    cap INTEGER,
    status TEXT NOT NULL DEFAULT 'running',
    summary TEXT
);
CREATE TABLE IF NOT EXISTS status_events (
    id TEXT PRIMARY KEY,
    comment_id TEXT NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT,
    run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_status_events_comment ON status_events(comment_id);
CREATE INDEX IF NOT EXISTS idx_comments_status ON comments(status);
"""


def migrate(c: sqlite3.Connection) -> None:
    for table, cols in NEW_COLUMNS.items():
        existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, ddl in cols:
            if name not in existing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    c.executescript(NEW_TABLES)
```

`main.py`: add `import feedback_schema` after the fastapi imports, and as the LAST line inside `init_db()`'s `with` block (after the `drive_folder_id` migration):
```python
        feedback_schema.migrate(c)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add feedback_schema.py main.py tests/test_schema.py
git commit -m "feat(schema): additive work-queue columns, runs and status_events tables"
```

---

### Task 2: Transition table and round state

**Files:**
- Create: `feedback_state.py`, `tests/test_state_rules.py`

- [ ] **Step 1: Write the failing test**

`tests/test_state_rules.py`:
```python
import pytest

from feedback_state import DRAIN, USER, can_ship, can_transition, round_state


@pytest.mark.parametrize("frm,to,actor,ok", [
    ("new", "queued", USER, True),
    ("queued", "doing", DRAIN, True),
    ("queued", "doing", USER, False),       # only the drain claims work
    ("doing", "verified", DRAIN, True),
    ("doing", "done", DRAIN, False),        # B2: the drain can never mark Done
    ("verified", "done", DRAIN, False),
    ("verified", "done", USER, True),
    ("doing", "skipped", USER, True),       # skip mid-run; drain drops the commit at merge
    ("doing", "queued", DRAIN, True),       # lane conflict re-queue
    ("done", "queued", USER, True),         # reopen
    ("new", "done", USER, False),           # no skipping the pipeline
    ("blocked", "doing", DRAIN, False),     # a blocked item needs the user first
])
def test_transitions(frm, to, actor, ok):
    assert can_transition(frm, to, actor) is ok


@pytest.mark.parametrize("counts,needs_repo,shipped,resent,expected", [
    ({"new": 7}, True, False, False, "Needs repo"),
    ({"doing": 1, "queued": 2, "done": 3}, False, False, False, "Running"),
    ({"queued": 2}, False, False, False, "Queued"),
    ({"blocked": 1, "done": 4}, False, False, False, "Blocked"),
    ({"new": 2, "done": 3}, False, False, False, "Open"),
    ({"verified": 2, "done": 3}, False, False, False, "To confirm"),
    ({"done": 5, "skipped": 1}, False, False, False, "Ready to ship"),
    ({"done": 5}, False, False, True, "Re-review sent"),
    ({"done": 5}, False, True, True, "Shipped"),
])
def test_round_state(counts, needs_repo, shipped, resent, expected):
    assert round_state(counts, needs_repo, shipped, resent) == expected


def test_can_ship_counts_skipped_as_finished_but_not_verified():
    assert can_ship({"done": 3, "skipped": 2}) is True
    assert can_ship({"done": 3, "verified": 1}) is False   # a verified item still needs the user's look
    assert can_ship({}) is False                           # nothing to ship
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_state_rules.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'feedback_state'`.

- [ ] **Step 3: Implement**

`feedback_state.py`:
```python
"""Pure rules for the feedback work queue. No I/O, so it is tested directly."""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

STATUSES = ("new", "queued", "doing", "verified", "done", "skipped", "blocked")
USER, DRAIN = "user", "drain"

# (from, to) -> actors allowed to make that move. The drain can never reach
# "done": Done is the user's judgement on the before/after (design B2).
TRANSITIONS: dict[tuple[str, str], set[str]] = {
    ("new", "queued"): {USER},
    ("new", "skipped"): {USER},
    ("queued", "new"): {USER},
    ("queued", "doing"): {DRAIN},
    ("queued", "skipped"): {USER},
    ("doing", "verified"): {DRAIN},
    ("doing", "blocked"): {DRAIN},
    ("doing", "queued"): {DRAIN, USER},
    ("doing", "skipped"): {USER},
    ("verified", "done"): {USER},
    ("verified", "queued"): {USER},
    ("verified", "skipped"): {USER},
    ("verified", "blocked"): {DRAIN},
    ("blocked", "queued"): {USER},
    ("blocked", "skipped"): {USER},
    ("done", "queued"): {USER},
    ("skipped", "new"): {USER},
}


def can_transition(frm: str, to: str, actor: str) -> bool:
    return actor in TRANSITIONS.get((frm, to), set())


def round_state(counts: dict, needs_repo: bool, shipped: bool, rereview_sent: bool) -> str:
    n = lambda k: counts.get(k, 0)  # noqa: E731
    if needs_repo:
        return "Needs repo"
    if n("doing"):
        return "Running"
    if n("queued"):
        return "Queued"
    if n("blocked"):
        return "Blocked"
    if n("new"):
        return "Open"
    if n("verified"):
        return "To confirm"
    if shipped:
        return "Shipped"
    if rereview_sent:
        return "Re-review sent"
    return "Ready to ship"


def can_ship(counts: dict) -> bool:
    finished = counts.get("done", 0) + counts.get("skipped", 0)
    total = sum(counts.get(s, 0) for s in STATUSES)
    return total > 0 and finished == total
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_state_rules.py -q`
Expected: `22 passed`.

- [ ] **Step 5: Commit**

```bash
git add feedback_state.py tests/test_state_rules.py
git commit -m "feat(state): transition table (drain can never mark Done) and computed round state"
```

---

### Task 3: Repo resolution, path normalisation, repeat classification

**Files:**
- Modify: `feedback_state.py` (append)
- Create: `tests/test_state_matching.py`

- [ ] **Step 1: Write the failing test**

`tests/test_state_matching.py`:
```python
from feedback_state import classify_repeat, iou, normalize_path, resolve_repo

PROJ = {"repo": "leslurb/abdry-rebuild", "local_path": r"C:\Users\sebas\abdry-rebuild",
        "url_prefixes": ["https://abdry-staging.pages.dev"], "target_url": None}


def test_normalize_path():
    assert normalize_path("https://x.dev/products/?a=1#top") == "/products"
    assert normalize_path("https://x.dev") == "/"
    assert normalize_path("/contact/") == "/contact"
    assert normalize_path(None) == "/"


def test_project_mapping_wins_over_reviewer_hint():
    hints = {"https://abdry-staging.pages.dev": {"repo": "someone/else"}}
    got = resolve_repo(PROJ, "https://abdry-staging.pages.dev/products", hints)
    assert got == {"repo": "leslurb/abdry-rebuild", "local_path": PROJ["local_path"], "source": "project"}


def test_url_outside_project_prefixes_is_not_trusted():
    assert resolve_repo(PROJ, "https://evil.example/products", None) is None


def test_direct_issue_path_resolves_to_project():
    assert resolve_repo(PROJ, "/products", None)["source"] == "project"


def test_reviewer_hint_fallback_uses_longest_prefix():
    hints = {"https://a.dev": {"repo": "r/short"}, "https://a.dev/app": {"repo": "r/long", "local_path": "C:/x"}}
    got = resolve_repo({"repo": None}, "https://a.dev/app/page", hints)
    assert got == {"repo": "r/long", "local_path": "C:/x", "source": "reviewer_hint"}


def test_unmapped_returns_none():
    assert resolve_repo(None, "https://a.dev/x", {}) is None


def test_iou():
    assert iou({"x": 0, "y": 0, "w": 10, "h": 10}, {"x": 0, "y": 0, "w": 10, "h": 10}) == 1.0
    assert iou({"x": 0, "y": 0, "w": 10, "h": 10}, {"x": 20, "y": 20, "w": 5, "h": 5}) == 0.0
    assert iou({}, {"x": 0, "y": 0, "w": 1, "h": 1}) == 0.0


def test_classify_repeat_regression_vs_didnt_take():
    new = {"page": "/", "selector": "footer .logo img", "bbox": {}}
    shipped_prior = {"id": "p1", "page": "/", "selector": "footer .logo img", "bbox": {}, "shipped": True}
    unshipped_prior = {"id": "p2", "page": "/", "selector": "footer .logo img", "bbox": {}, "shipped": False}
    assert classify_repeat(new, [shipped_prior]) == ("p1", "regression")
    assert classify_repeat(new, [unshipped_prior]) == ("p2", "didnt_take")


def test_classify_repeat_region_pins_use_overlap_and_ignore_other_pages():
    new = {"page": "/", "selector": None, "bbox": {"x": 0, "y": 0, "w": 10, "h": 10}}
    near = {"id": "p3", "page": "/", "selector": None, "bbox": {"x": 1, "y": 1, "w": 10, "h": 10}, "shipped": True}
    other_page = {"id": "p4", "page": "/x", "selector": None, "bbox": new["bbox"], "shipped": True}
    assert classify_repeat(new, [other_page]) is None
    assert classify_repeat(new, [near]) == ("p3", "regression")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_state_matching.py -q`
Expected: FAIL, `ImportError: cannot import name 'classify_repeat'`.

- [ ] **Step 3: Implement (append to `feedback_state.py`)**

```python
def normalize_path(u: Optional[str]) -> str:
    """Page identity for lanes and repeat detection: path only, no query/hash/trailing slash."""
    if not u:
        return "/"
    path = urlsplit(u).path if "://" in u else u.split("?", 1)[0].split("#", 1)[0]
    return "/" + path.strip("/")


def resolve_repo(project: Optional[dict], page_url: Optional[str], batch_hints: Optional[dict]) -> Optional[dict]:
    """Project mapping first (owner-set, works for every reviewer); the reviewer's
    own repo_hints only as a fallback. A full URL outside the project's prefixes
    is not trusted. A bare path (direct issue) always belongs to its project."""
    if project and project.get("repo"):
        prefixes = [p for p in (project.get("url_prefixes") or []) if p]
        if project.get("target_url"):
            prefixes.append(project["target_url"])
        is_url = bool(page_url) and "://" in page_url
        if not is_url or not prefixes or any(page_url.startswith(p) for p in prefixes):
            return {"repo": project["repo"], "local_path": project.get("local_path"), "source": "project"}
    best = None
    for prefix, info in (batch_hints or {}).items():
        if page_url and page_url.startswith(prefix) and isinstance(info, dict) and info.get("repo"):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, info)
    if best:
        return {"repo": best[1]["repo"], "local_path": best[1].get("local_path"), "source": "reviewer_hint"}
    return None


def iou(a: dict, b: dict) -> float:
    try:
        ax, ay, aw, ah = a["x"], a["y"], a["w"], a["h"]
        bx, by, bw, bh = b["x"], b["y"], b["w"], b["h"]
    except (KeyError, TypeError):
        return 0.0
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def classify_repeat(new: dict, priors: list[dict]) -> Optional[tuple[str, str]]:
    """new/priors: {page, selector, bbox} (+ id, shipped on priors, which are the
    project's earlier DONE comments). Same page AND (same selector, or bbox
    overlap > 0.5 when either side has no selector). Shipped prior =
    regression; unshipped prior = the fix didn't take."""
    for p in priors:
        if p["page"] != new["page"]:
            continue
        if new.get("selector") and p.get("selector"):
            same = new["selector"] == p["selector"]
        else:
            same = iou(new.get("bbox") or {}, p.get("bbox") or {}) > 0.5
        if same:
            return p["id"], ("regression" if p.get("shipped") else "didnt_take")
    return None
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_state_matching.py -q`
Expected: `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add feedback_state.py tests/test_state_matching.py
git commit -m "feat(state): repo resolution, page normalisation, repeat classification"
```

---

### Task 4: Ingest stores recipient, repo hints, viewport, repeats

**Files:**
- Modify: `main.py` `ingest_payload()` (lines ~260-430)
- Create: `tests/test_ingest_queue_fields.py`

- [ ] **Step 1: Write the failing test**

`tests/test_ingest_queue_fields.py`:
```python
import json

import main


def _payload(**over):
    p = {
        "schema": "prototype-feedback/v5",
        "reviewer": "Mike",
        "project_id": "proj1",
        "repo_hints": {"https://a.dev": {"repo": "r/a"}},
        "comments": [{
            "id": "c-new", "page_url": "https://a.dev/?x=1", "selector": "footer .logo img",
            "bbox": {"x": 0, "y": 0, "w": 10, "h": 10}, "viewport": {"w": 390, "h": 844},
            "comment": "logo blurry", "recipient_resolved": "human",
        }],
    }
    p.update(over)
    return p


def _project():
    with main.db() as c:
        c.execute("INSERT INTO projects (id, created_at, updated_at, name, repo) VALUES ('proj1','t','t','P','r/a')")


def test_ingest_stores_recipient_hints_and_viewport():
    _project()
    bid = main.ingest_payload(_payload(), "x.json")
    with main.db() as c:
        cm = c.execute("SELECT * FROM comments WHERE id='c-new'").fetchone()
        b = c.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
    assert (cm["recipient"], cm["viewport_w"], cm["status"]) == ("human", 390, "new")
    assert json.loads(b["repo_hints_json"]) == {"https://a.dev": {"repo": "r/a"}}
    assert b["kind"] == "export"


def test_missing_recipient_defaults_to_llm_like_the_extension():
    _project()
    p = _payload()
    del p["comments"][0]["recipient_resolved"]
    main.ingest_payload(p, "x.json")
    with main.db() as c:
        assert c.execute("SELECT recipient FROM comments WHERE id='c-new'").fetchone()[0] == "llm"


def test_ingest_flags_regression_of_a_shipped_done_comment():
    _project()
    with main.db() as c:
        c.execute("INSERT INTO batches (id, ingested_at, shipped_at) VALUES ('old','2026-09-01','2026-09-02')")
        c.execute("""INSERT INTO comments (id, batch_id, page_url, selector, bbox_json, status, project_id)
                     VALUES ('c-old','old','https://a.dev/','footer .logo img','{}','done','proj1')""")
    main.ingest_payload(_payload(), "x.json")
    with main.db() as c:
        cm = c.execute("SELECT repeat_of, repeat_kind FROM comments WHERE id='c-new'").fetchone()
    assert (cm["repeat_of"], cm["repeat_kind"]) == ("c-old", "regression")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_ingest_queue_fields.py -q`
Expected: FAIL, `recipient` is `llm` for the first test (not stored) and `repo_hints_json` is None.

- [ ] **Step 3: Implement**

In `main.py` add `import feedback_state` next to `import feedback_schema`.

In `ingest_payload`, change the batches INSERT to also write `repo_hints_json`:
```python
        c.execute(
            """INSERT INTO batches
               (id, ingested_at, schema_version, extension_version, exported_at,
                reviewer, project, source_filename, comment_count, repo_hints_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ),
        )
```

Immediately after the existing auto-tag block (`if not project_id and reviewer_email: ...`) and before the comments INSERT, add:
```python
            recipient = cm.get("recipient_resolved") or "llm"
            viewport_w = (cm.get("viewport") or {}).get("w")
            repeat = None
            if project_id:
                priors = [
                    {"id": r["id"], "page": feedback_state.normalize_path(r["page_url"]),
                     "selector": r["selector"], "bbox": json.loads(r["bbox_json"] or "{}"),
                     "shipped": bool(r["shipped_at"])}
                    for r in c.execute(
                        """SELECT cm.id, cm.page_url, cm.selector, cm.bbox_json, b.shipped_at
                           FROM comments cm JOIN batches b ON b.id = cm.batch_id
                           WHERE cm.project_id = ? AND cm.status = 'done'""",
                        (project_id,),
                    )
                ]
                repeat = feedback_state.classify_repeat(
                    {"page": feedback_state.normalize_path(cm.get("page_url")),
                     "selector": cm.get("selector"), "bbox": cm.get("bbox") or {}},
                    priors,
                )
```

Change the comments INSERT to add the four new columns:
```python
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: all pass (`37 passed`).

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_ingest_queue_fields.py
git commit -m "feat(ingest): store recipient, repo hints, viewport width and repeat-of on ingest"
```

---

### Task 5: Router skeleton + conditional status endpoint

**Files:**
- Create: `feedback_api.py`, `tests/test_api_status.py`
- Modify: `main.py` (mount router after `app` and CORS are set up)

- [ ] **Step 1: Write the failing test**

`tests/test_api_status.py`:
```python
import main


def _comment(status="queued", recipient="llm"):
    with main.db() as c:
        c.execute("INSERT INTO batches (id, ingested_at) VALUES ('b1','t')")
        c.execute("INSERT INTO comments (id, batch_id, status, recipient) VALUES ('c1','b1',?,?)", (status, recipient))


def test_conditional_transition_writes_event(client):
    _comment("queued")
    r = client.post("/api/comment/c1/status",
                    json={"to": "doing", "expected_from": "queued", "actor": "drain", "run_id": "r1", "lane": "/"})
    assert r.status_code == 200, r.text
    with main.db() as c:
        row = c.execute("SELECT status, run_id, lane FROM comments WHERE id='c1'").fetchone()
        ev = c.execute("SELECT from_status, to_status, actor FROM status_events WHERE comment_id='c1'").fetchone()
    assert tuple(row) == ("doing", "r1", "/")
    assert tuple(ev) == ("queued", "doing", "drain")


def test_stale_expected_from_is_409_and_changes_nothing(client):
    _comment("skipped")          # user skipped it while the drain was working
    r = client.post("/api/comment/c1/status", json={"to": "verified", "expected_from": "doing", "actor": "drain"})
    assert r.status_code == 409
    assert r.json()["detail"]["current"] == "skipped"
    with main.db() as c:
        assert c.execute("SELECT status FROM comments WHERE id='c1'").fetchone()[0] == "skipped"


def test_drain_cannot_mark_done_is_422(client):
    _comment("verified")
    r = client.post("/api/comment/c1/status", json={"to": "done", "expected_from": "verified", "actor": "drain"})
    assert r.status_code == 422


def test_human_recipient_cannot_be_queued(client):
    _comment("new", recipient="human")
    r = client.post("/api/comment/c1/status", json={"to": "queued", "expected_from": "new", "actor": "user"})
    assert r.status_code == 422


def test_unknown_comment_is_404(client):
    r = client.post("/api/comment/nope/status", json={"to": "queued", "expected_from": "new", "actor": "user"})
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api_status.py -q`
Expected: FAIL, 404/405 on `/api/comment/c1/status`.

- [ ] **Step 3: Implement**

`feedback_api.py`:
```python
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
```

`main.py`: add `import feedback_api` next to the other feedback imports. Then, just above `def _cli()` near the end of the file (where `db` and `now_iso` already exist), add:
```python
app.include_router(feedback_api.make_router(db, now_iso))
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: all pass (`42 passed`).

- [ ] **Step 5: Commit**

```bash
git add feedback_api.py main.py tests/test_api_status.py
git commit -m "feat(api): conditional status transitions with 409 on stale writes and event log"
```

---

### Task 6: Bulk queue from the dashboard

**Files:**
- Modify: `feedback_api.py` (add endpoint inside `make_router`, before `return router`)
- Create: `tests/test_api_bulk_queue.py`

- [ ] **Step 1: Write the failing test**

`tests/test_api_bulk_queue.py`:
```python
import main


def _seed():
    with main.db() as c:
        c.execute("INSERT INTO projects (id, created_at, updated_at, name, repo) VALUES ('p1','t','t','P','r/a')")
        c.execute("INSERT INTO projects (id, created_at, updated_at, name) VALUES ('p2','t','t','Unmapped')")
        c.execute("INSERT INTO batches (id, ingested_at) VALUES ('b1','t')")
        c.execute("INSERT INTO batches (id, ingested_at) VALUES ('b2','t')")
        rows = [("c1", "b1", "new", "llm", "p1"), ("c2", "b1", "new", "human", "p1"),
                ("c3", "b1", "done", "llm", "p1"), ("c4", "b2", "new", "llm", "p2")]
        c.executemany("INSERT INTO comments (id, batch_id, status, recipient, project_id, page_url) VALUES (?,?,?,?,?,'/')", rows)


def test_queue_by_batch_only_takes_new_llm_mapped(client):
    _seed()
    r = client.post("/api/comments/queue", json={"batch_ids": ["b1", "b2"]})
    body = r.json()
    assert body["queued"] == ["c1"]
    reasons = {s["id"]: s["reason"] for s in body["skipped"]}
    assert reasons == {"c2": "human recipient", "c3": "status done", "c4": "no repo mapped"}


def test_queue_by_project_and_by_comment(client):
    _seed()
    assert client.post("/api/comments/queue", json={"project_ids": ["p1"]}).json()["queued"] == ["c1"]
    with main.db() as c:
        c.execute("UPDATE comments SET status='new' WHERE id='c1'")
    assert client.post("/api/comments/queue", json={"comment_ids": ["c1"]}).json()["queued"] == ["c1"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api_bulk_queue.py -q`
Expected: FAIL, 404/405.

- [ ] **Step 3: Implement**

Add to `feedback_api.py`, module level (below `StatusChange`):
```python
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
```

Inside `make_router`, before `router.change_status = ...`:
```python
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: all pass (`44 passed`).

- [ ] **Step 5: Commit**

```bash
git add feedback_api.py tests/test_api_bulk_queue.py
git commit -m "feat(api): bulk queue by comment/round/project, only New LLM comments with a repo"
```

---

### Task 7: The drain's queue read, grouped into page-lanes

**Files:**
- Modify: `feedback_api.py`
- Create: `tests/test_api_queue_read.py`

- [ ] **Step 1: Write the failing test**

`tests/test_api_queue_read.py`:
```python
import main


def test_queue_groups_by_project_then_page_lane(client):
    with main.db() as c:
        c.execute("""INSERT INTO projects (id, created_at, updated_at, name, repo, local_path)
                     VALUES ('p1','t','t','abdry','r/a','C:/abdry')""")
        c.execute("INSERT INTO batches (id, ingested_at) VALUES ('b1','t')")
        rows = [("c1", "https://x.dev/products?a=1", "queued"), ("c2", "https://x.dev/products/", "queued"),
                ("c3", "https://x.dev/", "queued"), ("c4", "https://x.dev/", "new")]
        c.executemany("""INSERT INTO comments (id, batch_id, page_url, status, recipient, project_id, comment, expected_result)
                         VALUES (?, 'b1', ?, ?, 'llm', 'p1', 'fix', 'fixed')""", rows)
    body = client.get("/api/queue").json()
    assert len(body["projects"]) == 1
    p = body["projects"][0]
    assert (p["repo"], p["local_path"]) == ("r/a", "C:/abdry")
    lanes = {lane["page"]: [cm["id"] for cm in lane["comments"]] for lane in p["lanes"]}
    assert lanes == {"/products": ["c1", "c2"], "/": ["c3"]}
    assert body["total"] == 3


def test_queue_is_empty_list_not_error_when_nothing_queued(client):
    body = client.get("/api/queue").json()
    assert body == {"projects": [], "total": 0}
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api_queue_read.py -q`
Expected: FAIL, 404.

- [ ] **Step 3: Implement (inside `make_router`)**

```python
    @router.get("/api/queue")
    def api_queue():
        projects: dict = {}
        with db() as c:
            rows = c.execute(
                "SELECT * FROM comments WHERE status = 'queued' ORDER BY created_at, id"
            ).fetchall()
            for row in rows:
                repo = resolve_for_comment(c, row)
                if repo is None:  # mapping removed after queueing: surface it, never drop silently
                    change_status(c, row["id"], StatusChange(
                        to="new", expected_from="queued", actor=fs.USER, note="unqueued: no repo mapped"))
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
        return {"projects": out, "total": sum(len(l["comments"]) for p in out for l in p["lanes"])}
```

Note: the "unqueued: no repo mapped" move uses actor `user` because it's a correction on the user's behalf, and `queued → new` is a user transition. It's written to `status_events`, so the dashboard can show it.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: all pass (`46 passed`).

- [ ] **Step 5: Commit**

```bash
git add feedback_api.py tests/test_api_queue_read.py
git commit -m "feat(api): /api/queue groups queued comments by project and page lane"
```

---

### Task 8: Runs, heartbeat and stale-Doing detection

**Files:**
- Modify: `feedback_api.py`
- Create: `tests/test_api_runs.py`

- [ ] **Step 1: Write the failing test**

`tests/test_api_runs.py`:
```python
import main


def test_run_lifecycle(client):
    rid = client.post("/api/runs", json={"cap": 3}).json()["id"]
    assert client.post(f"/api/runs/{rid}/heartbeat").status_code == 200
    r = client.post(f"/api/runs/{rid}/end", json={"status": "finished", "summary": {"verified": 3, "blocked": 1}})
    assert r.status_code == 200
    latest = client.get("/api/runs/latest").json()
    assert (latest["id"], latest["status"], latest["summary"]["verified"]) == (rid, "finished", 3)


def test_stale_doing_is_reported_when_run_heartbeat_is_old_or_run_ended(client):
    with main.db() as c:
        c.execute("INSERT INTO runs (id, started_at, heartbeat_at, status) VALUES ('old','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','running')")
        c.execute("INSERT INTO runs (id, started_at, heartbeat_at, status) VALUES ('live', ?, ?, 'running')",
                  (main.now_iso(), main.now_iso()))
        c.execute("INSERT INTO batches (id, ingested_at) VALUES ('b1','t')")
        c.execute("INSERT INTO comments (id, batch_id, status, run_id) VALUES ('stuck','b1','doing','old')")
        c.execute("INSERT INTO comments (id, batch_id, status, run_id) VALUES ('fine','b1','doing','live')")
        c.execute("INSERT INTO comments (id, batch_id, status, run_id) VALUES ('orphan','b1','doing',NULL)")
    ids = sorted(s["id"] for s in client.get("/api/stale?minutes=30").json()["stale"])
    assert ids == ["orphan", "stuck"]


def test_heartbeat_on_unknown_run_is_404(client):
    assert client.post("/api/runs/nope/heartbeat").status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api_runs.py -q`
Expected: FAIL, 404/405.

- [ ] **Step 3: Implement**

Module level in `feedback_api.py`:
```python
from datetime import datetime, timedelta, timezone


class RunStart(BaseModel):
    cap: int


class RunEnd(BaseModel):
    status: str  # finished | stopped | failed
    summary: dict = {}
```

Inside `make_router`:
```python
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: all pass (`49 passed`).

- [ ] **Step 5: Commit**

```bash
git add feedback_api.py tests/test_api_runs.py
git commit -m "feat(api): runs with heartbeat, latest run, stale-Doing detection"
```

---

### Task 9: Direct issues, project mapping and round marks

**Files:**
- Modify: `feedback_api.py`, `main.py` (`api_projects_update` whitelist, currently ~line 932)
- Create: `tests/test_api_direct_and_rounds.py`

- [ ] **Step 1: Write the failing test**

`tests/test_api_direct_and_rounds.py`:
```python
import main


def _project(repo="r/a"):
    with main.db() as c:
        c.execute("INSERT INTO projects (id, created_at, updated_at, name, repo) VALUES ('p1','t','t','P',?)", (repo,))


def test_direct_issue_goes_to_one_direct_batch_per_project(client):
    _project()
    body = {"title": "Queue button stays enabled", "page": "/", "viewport_w": 1440,
            "expected_result": "Button is greyed out", "recipient": "llm"}
    a = client.post("/api/projects/p1/issues", json=body).json()
    b = client.post("/api/projects/p1/issues", json=body).json()
    assert a["batch_id"] == b["batch_id"] and a["status"] == "new"
    with main.db() as c:
        assert tuple(c.execute("SELECT kind, reviewer FROM batches WHERE id=?", (a["batch_id"],)).fetchone()) == ("direct", "You")


def test_save_and_queue_requires_ready(client):
    _project()
    no_expected = {"title": "x", "page": "/", "recipient": "llm", "queue": True}
    assert client.post("/api/projects/p1/issues", json=no_expected).status_code == 422
    ok = {**no_expected, "expected_result": "y"}
    assert client.post("/api/projects/p1/issues", json=ok).json()["status"] == "queued"


def test_save_and_queue_refused_without_repo(client):
    _project(repo=None)
    body = {"title": "x", "page": "/", "recipient": "llm", "expected_result": "y", "queue": True}
    assert client.post("/api/projects/p1/issues", json=body).status_code == 422


def test_project_mapping_update(client):
    _project(repo=None)
    client.post("/api/projects/p1", json={"repo": "r/b", "local_path": "C:/b",
                                          "url_prefixes": ["https://b.dev"], "preview_url": "https://pr.b.dev"})
    with main.db() as c:
        p = c.execute("SELECT repo, local_path, url_prefixes_json, preview_url FROM projects WHERE id='p1'").fetchone()
    assert tuple(p) == ("r/b", "C:/b", '["https://b.dev"]', "https://pr.b.dev")


def test_ship_is_gated_on_every_llm_item_done_or_skipped(client):
    with main.db() as c:
        c.execute("INSERT INTO batches (id, ingested_at) VALUES ('b1','t')")
        c.execute("INSERT INTO comments (id, batch_id, status, recipient) VALUES ('c1','b1','verified','llm')")
        c.execute("INSERT INTO comments (id, batch_id, status, recipient) VALUES ('c2','b1','new','human')")
    assert client.post("/api/rounds/b1/mark", json={"what": "shipped"}).status_code == 409
    with main.db() as c:
        c.execute("UPDATE comments SET status='done' WHERE id='c1'")
    assert client.post("/api/rounds/b1/mark", json={"what": "shipped"}).status_code == 200
    with main.db() as c:
        assert c.execute("SELECT shipped_at FROM batches WHERE id='b1'").fetchone()[0]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api_direct_and_rounds.py -q`
Expected: FAIL, 404/405 on the new routes, and the mapping assert fails.

- [ ] **Step 3: Implement**

Module level in `feedback_api.py`:
```python
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
```

Inside `make_router`:
```python
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
            b = c.execute("SELECT id FROM batches WHERE project = ? AND kind = 'direct'", (pid,)).fetchone()
            if b is None:
                bid = str(uuid.uuid4())
                c.execute("""INSERT INTO batches (id, ingested_at, reviewer, project, kind, comment_count)
                             VALUES (?, ?, 'You', ?, 'direct', 0)""", (bid, now_iso(), pid))
            else:
                bid = b["id"]
            pin = None
            if body.pin_comment_id:
                pin = c.execute("SELECT selector, bbox_json, screenshot_path FROM comments WHERE id = ?",
                                (body.pin_comment_id,)).fetchone()
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
            counts = {r["status"]: r["n"] for r in c.execute(
                "SELECT status, COUNT(*) AS n FROM comments WHERE batch_id = ? AND recipient = 'llm' GROUP BY status",
                (bid,))}
            if not fs.can_ship(counts):
                raise HTTPException(409, {"error": "round not finished", "counts": counts})
            c.execute(f"UPDATE batches SET {col} = ? WHERE id = ?", (now_iso(), bid))
        return {"ok": True}
```

`main.py` `api_projects_update`: extend the whitelist and serialise the prefix list:
```python
    for f in ("name", "description", "target_url", "owner_email", "owner_name", "drive_folder_id",
              "repo", "local_path", "preview_cmd", "preview_url"):
        if f in body:
            fields[f] = body[f]
    if "url_prefixes" in body:
        fields["url_prefixes_json"] = json.dumps(body["url_prefixes"] or [])
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: all pass (`54 passed`).

- [ ] **Step 5: Commit**

```bash
git add feedback_api.py main.py tests/test_api_direct_and_rounds.py
git commit -m "feat(api): direct issues with readiness gate, project repo mapping, gated round marks"
```

---

### Task 10: Overview for the dashboard

**Files:**
- Modify: `feedback_api.py`
- Create: `tests/test_api_overview.py`

- [ ] **Step 1: Write the failing test**

`tests/test_api_overview.py`:
```python
import main


def _seed():
    with main.db() as c:
        c.execute("INSERT INTO projects (id, created_at, updated_at, name, repo) VALUES ('p1','t','t','abdry','r/a')")
        c.execute("INSERT INTO projects (id, created_at, updated_at, name) VALUES ('p2','t','t','sunsip')")
        c.execute("INSERT INTO batches (id, ingested_at, reviewer, project, kind) VALUES ('r1','2026-09-09','Cailyn','p1','export')")
        c.execute("INSERT INTO batches (id, ingested_at, reviewer, project, kind) VALUES ('r2','2026-09-18','Mike','p1','export')")
        c.execute("INSERT INTO batches (id, ingested_at, reviewer, project, kind) VALUES ('d1','2026-09-20','You','p1','direct')")
        c.execute("INSERT INTO batches (id, ingested_at, reviewer, project, kind) VALUES ('s1','2026-09-24','Cailyn','p2','export')")
        rows = [
            ("a", "r1", "done", "llm", "p1"), ("b", "r1", "done", "llm", "p1"), ("h", "r1", "new", "human", "p1"),
            ("c", "r2", "verified", "llm", "p1"), ("d", "r2", "blocked", "llm", "p1"),
            ("e", "d1", "new", "llm", "p1"),
            ("f", "s1", "new", "llm", "p2"),
        ]
        c.executemany("INSERT INTO comments (id, batch_id, status, recipient, project_id, page_url) VALUES (?,?,?,?,?,'/')", rows)


def test_overview_rounds_states_trend_and_needs_you(client):
    _seed()
    ov = client.get("/api/overview").json()
    projects = {p["name"]: p for p in ov["projects"]}
    abdry = projects["abdry"]
    rounds = {r["id"]: r for r in abdry["rounds"]}
    assert abdry["rounds"][0]["id"] == "d1"                  # "Your issues" first
    assert rounds["r2"]["state"] == "Blocked"
    assert rounds["r1"]["counts"] == {"done": 2}              # human comments are not in LLM counts
    assert rounds["r1"]["human_open"] == 1
    assert abdry["trend"] == [3, 2]                          # reviewer exports only, all recipients, oldest first
    assert projects["sunsip"]["rounds"][0]["state"] == "Needs repo"
    ny = ov["needs_you"]
    assert (ny["blocked"], ny["to_confirm"], ny["human_open"]) == (1, 1, 1)
    assert ny["unmapped_projects"] == [{"project_id": "p2", "name": "sunsip", "waiting": 1}]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api_overview.py -q`
Expected: FAIL, 404.

- [ ] **Step 3: Implement (inside `make_router`)**

```python
    @router.get("/api/overview")
    def api_overview():
        out, needs = [], {"blocked": 0, "to_confirm": 0, "human_open": 0, "unmapped_projects": []}
        with db() as c:
            for p in c.execute("SELECT * FROM projects WHERE archived = 0 ORDER BY name"):
                project = load_project(c, p["id"])
                batches = c.execute(
                    """SELECT * FROM batches WHERE project = ?
                       ORDER BY CASE kind WHEN 'direct' THEN 0 ELSE 1 END, ingested_at DESC""",
                    (p["id"],)).fetchall()
                rounds, trend, waiting_unmapped = [], [], 0
                for b in batches:
                    rows = c.execute("SELECT * FROM comments WHERE batch_id = ?", (b["id"],)).fetchall()
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
```

Note: batches link to projects through `batches.project`. For exports, that holds whatever the extension sent (a label or an id). Rounds whose `batches.project` is not a project id won't appear under that project. **Plan B's first task must check this against real data** and, if needed, link rounds through `comments.project_id` instead. The test seeds `batches.project` with the id.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: all pass (`55 passed`).

- [ ] **Step 5: Commit**

```bash
git add feedback_api.py tests/test_api_overview.py
git commit -m "feat(api): /api/overview with computed round states, trend and needs-you counts"
```

---

### Task 11: End-to-end check against a running server

- [ ] **Step 1: Confirm the backup from Task 0 step 0 exists**

Run (cmd): `dir data\feedback.db.pre-plan-a`
Expected: one file listed. Don't start the real server (`run.bat`) during this plan, or step 4's comparison will show its writes.

- [ ] **Step 2: Start a server on a scratch data dir**

Run (PowerShell): `$env:PFB_DATA_DIR = "$env:TEMP\pfb-e2e"; .venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8091`
Expected: `Uvicorn running on http://127.0.0.1:8091`.

- [ ] **Step 3: Drive the golden path with curl (second terminal)**

```bash
curl -s -X POST http://127.0.0.1:8091/api/projects -H "Content-Type: application/json" -d "{\"name\":\"e2e\"}"
```
Take the returned `id` as `PID`, then:
```bash
curl -s -X POST http://127.0.0.1:8091/api/projects/PID -H "Content-Type: application/json" -d "{\"repo\":\"leslurb/prototype-feedback-server\",\"local_path\":\"C:/Users/sebas/prototype-feedback-server\"}"
curl -s -X POST http://127.0.0.1:8091/api/projects/PID/issues -H "Content-Type: application/json" -d "{\"title\":\"e2e issue\",\"page\":\"/\",\"expected_result\":\"works\",\"queue\":true}"
curl -s http://127.0.0.1:8091/api/queue
curl -s http://127.0.0.1:8091/api/overview
```
Expected:
- The issue call returns `"status":"queued"`.
- `/api/queue` returns `"total":1` with one lane `"page":"/"`.
- `/api/overview` shows project `e2e` whose first round has `"kind":"direct"` and `"state":"Queued"`.

(If `POST /api/projects` needs other fields, read `api_projects_create` and add them; don't change the route.)

- [ ] **Step 4: Prove the real DB was not touched**

Run (cmd): `fc /b data\feedback.db data\feedback.db.pre-plan-a`
Expected: `FC: no differences encountered` (the suite and the e2e run both used temp dirs).

- [ ] **Step 5: Stop the server; full suite once more**

Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: `55 passed`.

---

## Roadmap: Plans B–E (each written once the previous plan has landed)

| Plan | Builds | Depends on | First thing it must check |
|---|---|---|---|
| **B · Dashboard** | The 4 screens from the canvas, as Jinja pages on `/` (replacing the batch list), `/round/{id}`, `/comment/{id}`, plus the New-issue sheet. They read `/api/overview` and write through Plan A's endpoints. | A | How `batches.project` is filled by real exports (see Task 10 note) |
| **C · Verify** | `POST /api/comment/{id}/verify` with Playwright: preview build sha == branch head gate, element by `verify_selector` → `selector` → bbox, after-shot to `after_path`, `doing→verified` or `→blocked` | A | How each project's preview exposes its build sha |
| **D · Extension pin push** | "Send pin to server" in the extension (owner = reviewer → the project's direct batch), listed by the composer's "Attach a pin" | A, B | The existing "Connect to local server" code path in popup.js; privacy-site gate if anything new is captured |
| **E · `/feedback` skill + ops** | `~/.claude/skills/feedback/SKILL.md` (list + run: lanes, worktrees at `C:\Users\sebas\.queue-work\<repo>-fb-<run>`, cap and fleet windows, merge one lane at a time, preview command, verify, 409 handling, heartbeat), logon Scheduled Task (S4U) | A, C | `/queue run`'s current SKILL.md, so the shared orchestrator rules stay identical |
