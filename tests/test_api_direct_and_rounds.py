import main


def _project(pid="p1", repo="r/a", name="P"):
    with main.db() as c:
        c.execute("INSERT INTO projects (id, created_at, updated_at, name, repo) VALUES (?,'t','t',?,?)",
                  (pid, name, repo))


def test_direct_issue_goes_to_one_direct_batch_per_project(client):
    _project()
    body = {"title": "Queue button stays enabled", "page": "/", "viewport_w": 1440,
            "expected_result": "Button is greyed out", "recipient": "llm"}
    a = client.post("/api/projects/p1/issues", json=body).json()
    b = client.post("/api/projects/p1/issues", json=body).json()
    assert a["batch_id"] == b["batch_id"] and a["status"] == "new"
    with main.db() as c:
        row = c.execute("SELECT kind, reviewer, project, project_id FROM batches WHERE id=?",
                         (a["batch_id"],)).fetchone()
    assert tuple(row) == ("direct", "You", "P", "p1")


def test_unknown_pin_is_404(client):
    _project()
    body = {"title": "x", "page": "/", "recipient": "llm", "pin_comment_id": "nope"}
    r = client.post("/api/projects/p1/issues", json=body)
    assert r.status_code == 404
    with main.db() as c:
        assert c.execute("SELECT COUNT(*) FROM batches WHERE kind='direct'").fetchone()[0] == 0


def test_pin_from_another_project_is_422(client):
    _project()
    _project(pid="p2", name="Q")
    with main.db() as c:
        c.execute("INSERT INTO batches (id, ingested_at, project_id) VALUES ('b2','t','p2')")
        c.execute("INSERT INTO comments (id, batch_id, project_id) VALUES ('c9','b2','p2')")
    body = {"title": "x", "page": "/", "recipient": "llm", "pin_comment_id": "c9"}
    r = client.post("/api/projects/p1/issues", json=body)
    assert r.status_code == 422
    with main.db() as c:
        assert c.execute("SELECT COUNT(*) FROM batches WHERE kind='direct'").fetchone()[0] == 0


def test_mark_unknown_round_is_404(client):
    assert client.post("/api/rounds/nope/mark", json={"what": "shipped"}).status_code == 404


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


def test_direct_batch_can_never_be_marked_as_a_round(client):
    with main.db() as c:
        c.execute("INSERT INTO batches (id, ingested_at, kind) VALUES ('d1','t','direct')")
        c.execute("INSERT INTO comments (id, batch_id, status, recipient) VALUES ('c1','d1','done','llm')")
    r = client.post("/api/rounds/d1/mark", json={"what": "shipped"})
    assert r.status_code == 422
    assert "direct stream" in r.json()["detail"]
    with main.db() as c:
        assert c.execute("SELECT shipped_at FROM batches WHERE id='d1'").fetchone()[0] is None
