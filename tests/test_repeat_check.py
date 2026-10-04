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


def test_unknown_project_is_404(client):
    _seed()
    assert client.get("/api/projects/nope/repeat-check", params={"pin_comment_id": "pin"}).status_code == 404


def test_pin_belonging_to_another_project_is_422(client):
    _seed()
    with main.db() as c:
        c.execute("INSERT INTO projects (id, created_at, updated_at, name, repo) VALUES ('p2','t','t','Q','r/b')")
        c.execute("INSERT INTO comments (id, batch_id, project_id, status, page_url, selector, bbox_json) "
                  "VALUES ('otherpin','new','p2','new','https://x.dev/?a=1','footer img','{}')")
    assert client.get("/api/projects/p1/repeat-check",
                       params={"pin_comment_id": "otherpin"}).status_code == 422
