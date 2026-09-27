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


def test_human_comment_can_go_new_to_done_with_event(client):
    _comment("new", recipient="human")
    r = client.post("/api/comment/c1/status", json={"to": "done", "expected_from": "new", "actor": "user"})
    assert r.status_code == 200, r.text
    with main.db() as c:
        assert c.execute("SELECT status FROM comments WHERE id='c1'").fetchone()[0] == "done"
        ev = c.execute("SELECT from_status, to_status, actor FROM status_events WHERE comment_id='c1'").fetchone()
    assert tuple(ev) == ("new", "done", "user")


def test_llm_comment_new_to_done_is_still_422(client):
    _comment("new", recipient="llm")
    r = client.post("/api/comment/c1/status", json={"to": "done", "expected_from": "new", "actor": "user"})
    assert r.status_code == 422
    with main.db() as c:
        assert c.execute("SELECT status FROM comments WHERE id='c1'").fetchone()[0] == "new"
