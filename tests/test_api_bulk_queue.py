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


def test_unknown_comment_id_is_reported_not_found(client):
    _seed()
    body = client.post("/api/comments/queue", json={"comment_ids": ["c1", "nope"]}).json()
    assert body["queued"] == ["c1"]
    assert {"id": "nope", "reason": "not found"} in body["skipped"]


def test_row_changed_concurrently_is_skipped_and_others_still_queue(client):
    _seed()
    with main.db() as c:
        c.execute("INSERT INTO comments (id, batch_id, status, recipient, project_id, page_url) VALUES ('c5','b1','new','llm','p1','/')")
        c.execute("CREATE TRIGGER t_stale BEFORE UPDATE OF status ON comments WHEN NEW.id = 'c1' BEGIN SELECT RAISE(IGNORE); END")
    try:
        body = client.post("/api/comments/queue", json={"batch_ids": ["b1"]}).json()
        assert body["queued"] == ["c5"]
        stale = {s["id"]: s["reason"] for s in body["skipped"]}
        assert stale["c1"].startswith("changed concurrently")
        with main.db() as c:
            status = c.execute("SELECT status FROM comments WHERE id = 'c5'").fetchone()["status"]
        assert status == "queued"
    finally:
        with main.db() as c:
            c.execute("DROP TRIGGER t_stale")
