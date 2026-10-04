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


def test_delete_batch_redirects_to_batches(client):
    with main.db() as c:
        c.execute("INSERT INTO batches (id, ingested_at) VALUES ('del1','t')")
    r = client.post("/api/batch/del1/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/batches"
    with main.db() as c:
        assert c.execute("SELECT id FROM batches WHERE id='del1'").fetchone() is None


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


def test_needs_you_strip_ignores_archived_projects(client):
    _seed()
    with main.db() as c:
        c.execute("INSERT INTO projects (id, created_at, updated_at, name, archived) "
                  "VALUES ('p3','t','t','archived-proj',1)")
        c.execute("INSERT INTO batches (id, ingested_at, reviewer, project, project_id, kind) "
                  "VALUES ('r3','t','Mike','archived-proj','p3','export')")
        rows = [("ab", "r3", "blocked", "llm", "p3", "https://z.dev/"),
                ("av", "r3", "verified", "llm", "p3", "https://z.dev/"),
                ("ah", "r3", "new", "human", "p3", "https://z.dev/")]
        c.executemany("INSERT INTO comments (id, batch_id, status, recipient, project_id, page_url, comment) "
                      "VALUES (?,?,?,?,?,?, 'text ' || ?)", [(*r, r[0]) for r in rows])
    html = client.get("/").text
    for cid in ("ab", "av", "ah"):
        assert f'href="/comment/{cid}"' not in html
    assert "1 to confirm" in html and 'href="/comment/b"' in html
    assert "1 for you, not Claude" in html and 'href="/comment/h"' in html
