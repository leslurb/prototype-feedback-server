# Server Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the companion server safe and loop-ready for "Send to Claude Code": block cross-site writes, make re-ingesting a growing session work, serve per-comment status as `prototype-feedback-response/v2`, and give Claude a full-fidelity `claude_input.json`.

**Spec:** `C:\Users\sebas\prototype-feedback-extension\docs\superpowers\specs\2026-11-08-claude-code-integration-design.md` §6 and §8.

**Non-goals:**
- No extension changes, no plugin, no new DB columns for v5 fields (they are read from the batch's copied `feedback.json` instead).
- No content-addressed asset store, no WebP handling, no image resizing.
- No change to status transitions, the dashboard UI, or Gmail flows.
- No push of this repo (it is public and `main` is 60 commits ahead of `origin`; pushing is the user's call).

**Architecture:** A small ASGI-level guard in `main.py` rejects unsafe-method requests whose `Origin`/`Sec-Fetch-Site` show a foreign page. `ingest_payload` splits incoming comments into known and new by ID, updates known ones in place, and only creates a batch for new ones, deleting its batch directory if anything fails. The status mapping is a pure function in `feedback_state.py`; `feedback_api.py` builds and serves the v2 document. `write_claude_artifacts` merges the copied v5 `feedback.json` with DB status into a `v5-slim` file.

**Tech Stack:** Python 3.13, FastAPI/Starlette, SQLite, pytest + `fastapi.testclient`.

**Assumptions / Preconditions:**
- Working dir `C:\Users\sebas\prototype-feedback-server`; venv at `.venv\` with `requirements-dev.txt` installed.
- Baseline: `.venv/Scripts/python -m pytest -q` → `152 passed`.
- Create and use branch `feature/server-hardening` from `main` before Task 1.
- Tests import `main` with `PFB_DATA_DIR` set by `tests/conftest.py`; the `client` fixture is `TestClient(main.app)`. `TestClient` sends no `Origin` and no `Sec-Fetch-Site`, so existing tests keep passing under the guard.
- `main.db()` commits on success and closes without commit on exception (rollback). Only files on disk can be left behind by a failed ingest.
- Statuses: `new, queued, doing, verified, done, skipped, blocked` (`feedback_state.STATUSES`); `comments.recipient` is `llm` or `human`.

**Rollback:** Code-only. `git checkout main && git branch -D feature/server-hardening`. No migrations, no data rewrites.

---

### Task 1: Cross-site write guard

**Files:**
- Modify: `main.py:71-80` (after the CORS middleware)
- Test: `tests/test_origin_guard.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_origin_guard.py
import main

EXT = "chrome-extension://lkpciiloidfgdhlgaefcbggajlfljmbp"
URL = "/api/projects"   # any unsafe route; body content is irrelevant to the guard


def test_cross_site_simple_post_is_refused(client):
    r = client.post(URL, content="name=x", headers={
        "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site",
        "Content-Type": "text/plain"})
    assert r.status_code == 403
    assert r.json() == {"detail": "cross-site write refused"}


def test_foreign_origin_without_fetch_metadata_is_refused(client):
    r = client.post(URL, json={"name": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_same_site_other_port_is_refused(client):
    r = client.post(URL, json={"name": "x"}, headers={
        "Origin": "http://localhost:3000", "Sec-Fetch-Site": "same-site"})
    assert r.status_code == 403


def test_extension_origin_is_allowed(client):
    # Spike 2026-11-08: Chrome sends Sec-Fetch-Site: cross-site from the extension.
    r = client.post(URL, json={"name": "x"}, headers={"Origin": EXT, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code != 403


def test_dashboard_same_origin_is_allowed(client):
    r = client.post(URL, json={"name": "x"}, headers={
        "Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code != 403


def test_no_origin_no_metadata_is_allowed(client):
    # curl, scripts, the /feedback drain: no browser, no ambient credentials to abuse.
    r = client.post(URL, json={"name": "x"})
    assert r.status_code != 403


def test_reads_are_never_guarded(client):
    r = client.get("/api/overview", headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_origin_guard.py -v`
Expected: FAIL — `test_cross_site_simple_post_is_refused`, `test_foreign_origin_without_fetch_metadata_is_refused`, `test_same_site_other_port_is_refused` fail (status is not 403); the four "allowed" tests pass.

- [ ] **Step 3: Write minimal implementation**

In `main.py`, replace the comment block above `app.add_middleware(` (lines 71-74) with the first two lines below, keep the CORS `add_middleware` call unchanged, and add the guard after it:

```python
# The CORS allowlist only governs CORS-enabled reads and preflighted requests. It does NOT stop
# "simple" cross-site POSTs (text/plain or form bodies), so the write guard below does that.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(os.environ.get("PFB_EXTRA_ORIGINS")),
    allow_methods=["*"],
    allow_headers=["*"],
)

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def write_allowed(method: str, origin: Optional[str], fetch_site: Optional[str],
                  own_origin: str, allowed: list[str]) -> bool:
    """Refuse writes a foreign web page could forge. Same-origin (the dashboard), the
    allow-listed extension origins, and non-browser clients (no Origin, no fetch metadata)
    pass. Everything else -- cross-site or same-site pages -- is refused."""
    if method not in UNSAFE_METHODS:
        return True
    if fetch_site == "same-origin":
        return True
    if origin:
        return origin == own_origin or origin in allowed
    return fetch_site in (None, "none")


_WRITE_ALLOWED_ORIGINS = cors_origins(os.environ.get("PFB_EXTRA_ORIGINS"))


@app.middleware("http")
async def cross_site_write_guard(request: Request, call_next):
    own = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if not write_allowed(request.method, request.headers.get("origin"),
                         request.headers.get("sec-fetch-site"), own, _WRITE_ALLOWED_ORIGINS):
        return JSONResponse({"detail": "cross-site write refused"}, status_code=403)
    return await call_next(request)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_origin_guard.py -v`
Expected: 7 passed.
Run: `.venv/Scripts/python -m pytest -q`
Expected: `159 passed`.

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_origin_guard.py
git commit -m "fix: refuse cross-site writes (Origin + Sec-Fetch-Site guard)"
```

---

### Task 2: Idempotent ingest — known comments update in place

**Files:**
- Modify: `main.py:284-482` (`ingest_payload`) and `main.py:699-729` (`api_ingest`)
- Test: `tests/test_ingest_idempotent.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ingest_idempotent.py
import main


def _c(cid, text="t", edited_at=None):
    return {"id": cid, "page_url": "https://a.dev/", "selector": "h1", "bbox": {},
            "viewport": {"w": 1280}, "comment": text, "created_at": "2026-11-01T10:00:00Z",
            "edited_at": edited_at}


def _p(*comments):
    return {"schema": "prototype-feedback/v5", "reviewer": "R", "comments": list(comments)}


def _batches():
    with main.db() as c:
        return c.execute("SELECT id, comment_count FROM batches ORDER BY ingested_at").fetchall()


def test_reexport_with_one_new_comment_makes_a_batch_of_one():
    first = main.ingest_payload_ex(_p(_c("a"), _c("b")), "r1.zip")
    second = main.ingest_payload_ex(_p(_c("a"), _c("b"), _c("c")), "r2.zip")
    assert (first["new"], second["new"], second["known"]) == (2, 1, 2)
    assert second["batch_id"] != first["batch_id"]
    with main.db() as c:
        rows = {r["id"]: r["batch_id"] for r in c.execute("SELECT id, batch_id FROM comments")}
    assert rows == {"a": first["batch_id"], "b": first["batch_id"], "c": second["batch_id"]}
    assert [b["comment_count"] for b in _batches()] == [2, 1]


def test_reexport_keeps_server_status():
    main.ingest_payload_ex(_p(_c("a")), "r1.zip")
    with main.db() as c:
        c.execute("UPDATE comments SET status='verified', commit_sha='abc' WHERE id='a'")
    main.ingest_payload_ex(_p(_c("a")), "r2.zip")
    with main.db() as c:
        r = c.execute("SELECT status, commit_sha FROM comments WHERE id='a'").fetchone()
    assert (r["status"], r["commit_sha"]) == ("verified", "abc")


def test_all_known_makes_no_new_batch_and_returns_the_original():
    first = main.ingest_payload_ex(_p(_c("a")), "r1.zip")
    again = main.ingest_payload_ex(_p(_c("a")), "r1-again.zip")
    assert again == {"batch_id": first["batch_id"], "new": 0, "known": 1, "updated": 0}
    assert len(_batches()) == 1


def test_newer_edit_updates_text_older_does_not():
    main.ingest_payload_ex(_p(_c("a", "old", "2026-11-02T00:00:00Z")), "r1.zip")
    up = main.ingest_payload_ex(_p(_c("a", "new", "2026-11-03T00:00:00Z")), "r2.zip")
    stale = main.ingest_payload_ex(_p(_c("a", "stale", "2026-11-01T00:00:00Z")), "r3.zip")
    with main.db() as c:
        text = c.execute("SELECT comment FROM comments WHERE id='a'").fetchone()[0]
    assert (up["updated"], stale["updated"], text) == (1, 0, "new")


def test_ingest_payload_still_returns_a_batch_id_string():
    assert isinstance(main.ingest_payload(_p(_c("a")), "r1.zip"), str)


def test_api_ingest_reports_counts(client):
    import io, json, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("feedback.json", json.dumps(_p(_c("a"))))
    for _ in range(2):
        r = client.post("/api/ingest", files={"files": ("r.zip", buf.getvalue(), "application/zip")})
    res = r.json()["results"][0]
    assert (res["ok"], res["new"], res["known"]) == (True, 0, 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_ingest_idempotent.py -v`
Expected: FAIL — `AttributeError: module 'main' has no attribute 'ingest_payload_ex'` (and the second plain ingest of `a` would raise `sqlite3.IntegrityError: UNIQUE constraint failed: comments.id`).

- [ ] **Step 3: Write minimal implementation**

3a. Rename the existing function header and docstring in `main.py:284-289`:

```python
def ingest_payload_ex(payload: dict, source_filename: str, source_dir: Optional[Path] = None) -> dict:
    """Insert a batch for the comments this server has not seen; update known ones in place.

    Returns {"batch_id", "new", "known", "updated"}. Comment IDs are stable across re-exports,
    so a known ID keeps its batch and server status; only its text is refreshed, and only
    when the incoming edited_at is newer. If every comment is known, no batch is created and
    batch_id is the batch of the first known comment.

    For v1/v2 payloads, screenshots/attachments come from inline base64.
    For v3+ payloads, they come from files inside source_dir (the unzipped temp dir).
    """
```

3b. Directly after the schema check (after current line 295, before `batch_id = str(uuid.uuid4())`), insert:

```python
    comments = payload.get("comments", []) or []
    incoming_ids = [cm.get("id") for cm in comments if cm.get("id")]
    with db() as c:
        known_rows = {
            r["id"]: r for r in c.execute(
                f"SELECT id, batch_id, edited_at FROM comments WHERE id IN ({','.join('?' * len(incoming_ids))})",
                incoming_ids,
            )
        } if incoming_ids else {}
        updated = 0
        for cm in comments:
            row = known_rows.get(cm.get("id"))
            if row and cm.get("edited_at") and (row["edited_at"] or "") < cm["edited_at"]:
                c.execute("UPDATE comments SET comment = ?, edited_at = ?, updated_at = ? WHERE id = ?",
                          (cm.get("comment"), cm["edited_at"], now_iso(), cm["id"]))
                updated += 1
    new_comments = [cm for cm in comments if cm.get("id") not in known_rows]
    if not new_comments:
        first_known = known_rows[incoming_ids[0]]["batch_id"] if known_rows else None
        return {"batch_id": first_known, "new": 0, "known": len(known_rows), "updated": updated}
```

3c. Delete the now-duplicate line `comments = payload.get("comments", []) or []` (current line 308). In the batch INSERT tuple change `len(comments),` to `len(new_comments),`. Change the loop header `for cm in comments:` (current line 332) to `for cm in new_comments:`.

3d. Replace the function's tail (current lines 480-482):

```python
    # Write a slim "claude_input.json" + "claude_prompt.md" per batch.
    write_claude_artifacts(batch_id)
    return {"batch_id": batch_id, "new": len(new_comments), "known": len(known_rows), "updated": updated}


def ingest_payload(payload: dict, source_filename: str, source_dir: Optional[Path] = None) -> str:
    """Backward-compatible wrapper: returns only the batch id."""
    return ingest_payload_ex(payload, source_filename, source_dir)["batch_id"]
```

3e. In `api_ingest` (`main.py:699-729`) replace the two `batch_id = ingest_payload(...)` calls and the success append:

```python
                    res = ingest_payload_ex(payload, fname, source_dir=tmp)
            else:
                payload = json.loads(raw.decode("utf-8"))
                res = ingest_payload_ex(payload, fname)
            results.append({"ok": True, "filename": fname, **res})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_ingest_idempotent.py -v`
Expected: 6 passed.
Run: `.venv/Scripts/python -m pytest -q`
Expected: `165 passed`.

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_ingest_idempotent.py
git commit -m "fix: re-ingest of a growing session no longer fails on known comment ids"
```

---

### Task 3: Failed ingest leaves no orphan batch directory

**Files:**
- Modify: `main.py` (`ingest_payload_ex`, the block from `batch_id = str(uuid.uuid4())` to the end of the `with db() as c:` that inserts the batch)
- Test: `tests/test_ingest_idempotent.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_ingest_idempotent.py
import pytest
from fastapi import HTTPException


def test_failed_ingest_removes_its_batch_dir(tmp_data_dir):
    bad = _p(_c("x"))
    bad["comments"][0]["screenshot_path"] = "../../escape.png"
    before = set((tmp_data_dir / "batches").iterdir())
    with pytest.raises(HTTPException):
        main.ingest_payload_ex(bad, "bad.zip", source_dir=tmp_data_dir / "nonexistent-src")
    assert set((tmp_data_dir / "batches").iterdir()) == before
    assert _batches() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_ingest_idempotent.py::test_failed_ingest_removes_its_batch_dir -v`
Expected: FAIL — the `assert set(...) == before` line shows one extra `batches/<uuid>` directory.

- [ ] **Step 3: Write minimal implementation**

Wrap everything from `batch_dir = BATCHES / batch_id` through the end of the `with db() as c:` block (the one that ends with the `UPDATE batches SET project_id` statement) in `try/except`:

```python
    batch_id = str(uuid.uuid4())
    batch_dir = BATCHES / batch_id
    try:
        (batch_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        # ... the existing mkdir / feedback.json copy / `with db() as c:` block, unchanged,
        #     indented one level under `try:` ...
    except BaseException:
        # The DB transaction already rolled back (db() commits only on success);
        # remove the files this attempt copied so no orphan batch dir remains.
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise
```

(The executor indents the existing lines between `(batch_dir / "screenshots").mkdir(...)` and the `UPDATE batches SET project_id` statement by four spaces; no line inside changes.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_ingest_idempotent.py -v`
Expected: 7 passed.
Run: `.venv/Scripts/python -m pytest -q`
Expected: `166 passed`.

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_ingest_idempotent.py
git commit -m "fix: a failed ingest removes the batch dir it created"
```

---

### Task 4: Pure status → response/v2 mapping

**Files:**
- Modify: `feedback_state.py` (append)
- Test: `tests/test_state_rules.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_state_rules.py
import feedback_state as fs


def test_response_status_mapping():
    llm = {s: fs.response_status(s, "llm") for s in fs.STATUSES}
    assert llm == {"new": "pending", "queued": "pending", "doing": "in_progress",
                   "verified": "fixed", "done": "fixed", "skipped": "skipped",
                   "blocked": "needs_input"}
    assert fs.response_status("done", "human") == "answered"
    assert fs.response_status("new", "human") == "pending"
    assert fs.response_status("mystery", "llm") == "pending"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_state_rules.py::test_response_status_mapping -v`
Expected: FAIL — `AttributeError: module 'feedback_state' has no attribute 'response_status'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to feedback_state.py

# Server status -> prototype-feedback-response/v2 status (spec 2026-11-08 §6).
# "verified" (the drain's self-report) and "done" (the user's confirmation) both read as
# "fixed" to the extension; "done" additionally sets confirmed=true in the response.
RESPONSE_STATUS = {
    "new": "pending", "queued": "pending", "doing": "in_progress",
    "verified": "fixed", "done": "fixed", "skipped": "skipped", "blocked": "needs_input",
}


def response_status(status: str, recipient: str) -> str:
    if recipient == "human" and status == "done":
        return "answered"
    return RESPONSE_STATUS.get(status, "pending")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_state_rules.py -v`
Expected: all pass, including `test_response_status_mapping`.

- [ ] **Step 5: Commit**

```bash
git add feedback_state.py tests/test_state_rules.py
git commit -m "feat: map server statuses to prototype-feedback-response/v2"
```

---

### Task 5: `responses.json` endpoints (per round, per project)

**Files:**
- Modify: `feedback_api.py` (new helper above `make_router`; two routes inside `make_router` before `router.change_status = change_status`)
- Test: `tests/test_api_responses.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_responses.py
import main


def _seed():
    with main.db() as c:
        c.execute("INSERT INTO projects (id, created_at, updated_at, name) VALUES ('p1','t','t','P')")
        c.execute("INSERT INTO batches (id, ingested_at, project_id) VALUES ('b1','2026-11-01','p1')")
        c.execute("INSERT INTO batches (id, ingested_at, project_id) VALUES ('b2','2026-11-02','p1')")
        rows = [
            ("c1", "b1", "verified", "llm", "header fixed", "abc123", "2026-11-08T16:02:00Z"),
            ("c2", "b1", "blocked", "llm", "Saving as % or $?", None, "2026-11-08T16:03:00Z"),
            ("c3", "b1", "done", "human", "Yes, monthly is default", None, "2026-11-08T16:04:00Z"),
            ("c4", "b2", "done", "llm", "faq accordion", "def456", "2026-11-08T16:05:00Z"),
        ]
        for r in rows:
            c.execute("""INSERT INTO comments (id, batch_id, status, recipient, status_note, commit_sha,
                         updated_at, project_id, bbox_json) VALUES (?,?,?,?,?,?,?,'p1','{}')""", r)


def test_round_responses_shape(client):
    _seed()
    r = client.get("/api/rounds/b1/responses.json")
    assert r.status_code == 200
    doc = r.json()
    assert doc["schema"] == "prototype-feedback-response/v2"
    assert doc["batch_id"] == "b1"
    by = {x["comment_id"]: x for x in doc["responses"]}
    assert set(by) == {"c1", "c2", "c3"}
    assert by["c1"] == {"comment_id": "c1", "status": "fixed", "summary": "header fixed",
                        "commit_sha": "abc123", "acted_at": "2026-11-08T16:02:00Z", "confirmed": False}
    assert by["c2"]["status"] == "needs_input" and by["c2"]["question_back"] == "Saving as % or $?"
    assert by["c3"]["status"] == "answered" and by["c3"]["answer"] == "Yes, monthly is default"


def test_project_responses_span_rounds(client):
    _seed()
    doc = client.get("/api/projects/p1/responses.json").json()
    by = {x["comment_id"]: x for x in doc["responses"]}
    assert set(by) == {"c1", "c2", "c3", "c4"}
    assert by["c4"]["confirmed"] is True and doc["batch_id"] is None


def test_unknown_round_and_project_404(client):
    assert client.get("/api/rounds/nope/responses.json").status_code == 404
    assert client.get("/api/projects/nope/responses.json").status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_api_responses.py -v`
Expected: FAIL — 404 on `/api/rounds/b1/responses.json` (route not found), so `assert r.status_code == 200` fails.

- [ ] **Step 3: Write minimal implementation**

Add above `def make_router` in `feedback_api.py`:

```python
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
```

Add inside `make_router`, just before `router.change_status = change_status`:

```python
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
```

(`load_project` and `fs` are already imported/defined in `feedback_api.py`; `api_repeat_check` uses both.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_api_responses.py -v`
Expected: 3 passed.
Run: `.venv/Scripts/python -m pytest -q`
Expected: `170 passed`.

- [ ] **Step 5: Commit**

```bash
git add feedback_api.py tests/test_api_responses.py
git commit -m "feat: serve per-round and per-project responses.json (response/v2)"
```

---

### Task 6: `claude_input.json` as v5-slim

**Files:**
- Modify: `main.py:485-574` (`write_claude_artifacts`)
- Test: `tests/test_claude_input.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claude_input.py
import io, json, zipfile

import main


def _zip(payload):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("feedback.json", json.dumps(payload))
    return buf.getvalue()


def test_claude_input_carries_v5_fields_and_status(client, tmp_data_dir):
    payload = {
        "schema": "prototype-feedback/v5", "reviewer": "R",
        "repo_hints": {"https://a.dev": {"repo": "https://github.com/x/a"}},
        "version_hints": {"a.dev": {"commit": "a1f3c9e", "source": "meta"}},
        "comments": [{
            "id": "c1", "page_url": "https://a.dev/", "selector": "h1", "bbox": {}, "viewport": {"w": 1280},
            "comment": "fix it", "intent": "bug", "severity": "high", "recipient_resolved": "llm",
            "element_snapshot": {"tag": "h1"}, "version_hint": {"commit": "a1f3c9e"},
        }],
    }
    res = client.post("/api/ingest", files={"files": ("r.zip", _zip(payload), "application/zip")}).json()
    bid = res["results"][0]["batch_id"]
    with main.db() as c:
        c.execute("UPDATE comments SET status='queued', expected_result='title fits' WHERE id='c1'")
    main.write_claude_artifacts(bid)
    doc = json.loads((tmp_data_dir / "batches" / bid / "claude_input.json").read_text(encoding="utf-8"))
    assert doc["schema"] == "prototype-feedback/v5-slim"
    assert doc["repo_hints"] == payload["repo_hints"]
    assert doc["version_hints"] == payload["version_hints"]
    cm = doc["comments"][0]
    assert (cm["intent"], cm["severity"], cm["recipient"], cm["status"], cm["expected_result"]) == \
        ("bug", "high", "llm", "queued", "title fits")
    assert cm["element_snapshot"] == {"tag": "h1"}
    assert cm["version_hint"] == {"commit": "a1f3c9e"}
    assert "viewport_file" in cm
    assert any("responses.json" in step for step in doc["_llm_guide"]["how_to_apply"])


def test_claude_input_without_feedback_json_still_writes(tmp_data_dir):
    bid = main.ingest_payload({"schema": "prototype-feedback/v2", "comments": [
        {"id": "z", "page_url": "https://a.dev/", "comment": "x", "bbox": {}, "viewport": {}}]}, "old.json")
    doc = json.loads((tmp_data_dir / "batches" / bid / "claude_input.json").read_text(encoding="utf-8"))
    assert doc["comments"][0]["intent"] is None and doc["comments"][0]["status"] == "new"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_claude_input.py -v`
Expected: FAIL — `assert doc["schema"] == "prototype-feedback/v5-slim"` (actual `prototype-feedback/v2-slim`).

- [ ] **Step 3: Write minimal implementation**

In `write_claude_artifacts`, after the `att_rows` query block and before `atts_by_comment` (current line 502), load the copied export:

```python
    src_path = BATCHES / batch_id / "feedback.json"
    try:
        src = json.loads(src_path.read_text(encoding="utf-8")) if src_path.exists() else {}
    except (OSError, ValueError) as e:
        print(f"warning: batch {batch_id}: unreadable feedback.json ({e}); v5 fields omitted", file=sys.stderr)
        src = {}
    src_by_id = {cm.get("id"): cm for cm in src.get("comments") or [] if isinstance(cm, dict)}
    V5_FIELDS = ("intent", "severity", "element_snapshot", "version_hint", "framework_hint", "sort_index")
```

Replace the `comments.append({...})` body (current lines 519-534) with:

```python
        s = src_by_id.get(r["id"], {})
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
                "viewport_file": r["viewport_path"],
                "comment": r["comment"],
                "recipient": r["recipient"],
                "status": r["status"],
                "expected_result": r["expected_result"],
                **{k: s.get(k) for k in V5_FIELDS},
                "attachments": atts_by_comment.get(r["id"], []),
            }
        )
```

Replace the `payload = {...}` head and guide (current lines 536-560) with:

```python
    payload = {
        "schema": "prototype-feedback/v5-slim",
        "exported_at": b["exported_at"],
        "reviewer": b["reviewer"],
        "project": b["project"],
        "repo_hints": src.get("repo_hints") or json.loads(b["repo_hints_json"] or "{}"),
        "version_hints": src.get("version_hints") or {},
        "comment_count": len(comments),
        "_llm_guide": {
            "purpose": "Pinned feedback from a reviewer. Each comment is anchored to a DOM element OR viewport region with a cropped screenshot. Apply the changes the reviewer requests.",
            "how_to_apply": [
                "Skip comments whose status is done or skipped; they are already handled.",
                "Work on recipient='llm' comments. For recipient='human', draft an answer only; do not change code.",
                "Order: severity 'high' first, then group by page_url.",
                "For each comment, Read screenshot_file (crop) and viewport_file (context) to see what the reviewer saw.",
                "Locate the source from page_url and repo_hints; if version_hints pins a commit, compare it with HEAD and say how far apart they are before editing.",
                "Find the element via selector, then element_snapshot; for region_type='region' use bbox + screenshot.",
                "If expected_result is set, that is the definition of done.",
                "For attachments: type='image' or 'file' has file_path -- Read it; type='link' has url -- WebFetch it.",
                "Make one commit per comment.",
                "If a comment is unclear, do not guess: report it as needs_input with one question.",
                "When finished, write responses.json (schema prototype-feedback-response/v2: comment_id, status fixed|answered|proposed|needs_input|skipped, summary, answer, question_back, commit_sha) next to this file.",
            ],
            "field_reference": {
                "comments[].bbox": "{x,y,w,h} in CSS viewport pixels (NOT device pixels)",
                "comments[].viewport": "{w,h,scrollX,scrollY} in CSS px",
                "comments[].screenshot_file": "Relative path to a PNG cropped to bbox.",
                "comments[].viewport_file": "Relative path to a downscaled full-viewport PNG, or null.",
                "comments[].status": "Server work-queue status: new|queued|doing|verified|done|skipped|blocked.",
                "comments[].intent": "bug|question|polish|behavior-change from the extension, or null for old exports.",
                "comments[].element_snapshot": "PII-redacted HTML/style/ARIA of the pinned element, or null.",
                "comments[].attachments[].file_path": "Present for image/file. Read with the Read tool.",
                "comments[].attachments[].url": "Present for link. Use WebFetch.",
            },
        },
        "comments": comments,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_claude_input.py -v`
Expected: 2 passed.
Run: `.venv/Scripts/python -m pytest -q`
Expected: `172 passed`.

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_claude_input.py
git commit -m "feat: claude_input.json v5-slim with status, v5 fields and responses guidance"
```

---

### Task 7: End-to-end verification against a running server

**Files:** none (manual, throwaway data dir)

- [ ] **Step 1: Start the server on a scratch data dir and port**

```bash
cd /c/Users/sebas/prototype-feedback-server
export PFB_DATA_DIR="$(mktemp -d)" PFB_PORT=8099
.venv/Scripts/python -m uvicorn main:app --host 127.0.0.1 --port 8099 &
sleep 3
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8099/
```
Expected: `200`.

- [ ] **Step 2: Ingest the same session twice, then with one more comment**

```bash
python - <<'EOF'
import io, json, zipfile
def z(ids, name):
    p = {"schema": "prototype-feedback/v5", "comments": [
        {"id": i, "page_url": "https://a.dev/", "comment": i, "bbox": {}, "viewport": {}} for i in ids]}
    with zipfile.ZipFile(name, "w") as f: f.writestr("feedback.json", json.dumps(p))
z(["a", "b"], "r1.zip"); z(["a", "b", "c"], "r2.zip")
EOF
curl -s -F files=@r1.zip http://127.0.0.1:8099/api/ingest
curl -s -F files=@r1.zip http://127.0.0.1:8099/api/ingest
curl -s -F files=@r2.zip http://127.0.0.1:8099/api/ingest
```
Expected, in order: `"new":2,"known":0`; `"new":0,"known":2`; `"new":1,"known":2`, each with `"ok":true`.

- [ ] **Step 3: Read status back as response/v2**

```bash
BID=$(curl -s -F files=@r1.zip http://127.0.0.1:8099/api/ingest | python -c "import json,sys;print(json.load(sys.stdin)['results'][0]['batch_id'])")
curl -s http://127.0.0.1:8099/api/rounds/$BID/responses.json
```
Expected: JSON with `"schema":"prototype-feedback-response/v2"` and two entries, `a` and `b`, each `"status":"pending"`.

- [ ] **Step 4: Prove the write guard on the live server**

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8099/api/projects \
  -H "Origin: https://evil.example" -H "Sec-Fetch-Site: cross-site" -H "Content-Type: text/plain" -d 'name=x'
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8099/api/projects \
  -H "Origin: chrome-extension://lkpciiloidfgdhlgaefcbggajlfljmbp" -H "Content-Type: application/json" -d '{"name":"x"}'
```
Expected: `403`, then a non-403 code (the route's own response).

- [ ] **Step 5: Dashboard still works in a browser**

Open `http://127.0.0.1:8099/batches` in the in-app browser, drop `r2.zip` on the drop zone. Expected: the result shows success (no "cross-site write refused"); the batch list shows the batches. This proves same-origin dashboard POSTs pass the guard.

- [ ] **Step 6: Stop the server and clean up**

```bash
kill %1
rm -f r1.zip r2.zip
```

- [ ] **Step 7: Full suite**

Run: `.venv/Scripts/python -m pytest -q`
Expected: `172 passed`.
