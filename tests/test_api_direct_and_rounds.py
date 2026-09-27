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
