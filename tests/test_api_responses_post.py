import json

import main


def _seed(rows):
    with main.db() as c:
        c.execute("INSERT OR IGNORE INTO batches (id, ingested_at) VALUES ('b1','t')")
        for cid, status, recipient in rows:
            c.execute("INSERT INTO comments (id, batch_id, status, recipient, bbox_json) VALUES (?,?,?,?,'{}')",
                      (cid, "b1", status, recipient))


def _state(cid):
    with main.db() as c:
        r = c.execute("SELECT status, status_note, commit_sha FROM comments WHERE id=?", (cid,)).fetchone()
        ev = c.execute("SELECT from_status, to_status, actor FROM status_events WHERE comment_id=? ORDER BY at, rowid",
                       (cid,)).fetchall()
    return tuple(r), [tuple(e) for e in ev]


def _post(client, responses, schema="prototype-feedback-response/v2"):
    return client.post("/api/responses", json={"schema": schema, "responses": responses})


def test_fixed_on_doing_becomes_verified_with_commit_and_drain_actor(client):
    _seed([("a", "doing", "llm")])
    r = _post(client, [{"comment_id": "a", "status": "fixed", "summary": "done it", "commit_sha": "abc"}])
    assert r.json() == {"matched": 1, "unmatched": 0, "applied": 1, "rejected": []}
    st, ev = _state("a")
    assert st == ("verified", "done it", "abc") and ev == [("doing", "verified", "drain")]


def test_queued_comment_walks_through_doing(client):
    _seed([("a", "queued", "llm")])
    assert _post(client, [{"comment_id": "a", "status": "shipped", "summary": "s"}]).json()["applied"] == 1
    st, ev = _state("a")
    assert st[0] == "verified" and [e[:2] for e in ev] == [("queued", "doing"), ("doing", "verified")]
    assert {e[2] for e in ev} == {"drain"}


def test_v1_names_and_blocked_mappings_carry_the_note(client):
    _seed([("p", "doing", "llm"), ("n", "doing", "llm"), ("d", "doing", "llm")])
    r = _post(client, [
        {"comment_id": "p", "status": "proposed", "summary": "plan X"},
        {"comment_id": "n", "status": "needs_input", "question_back": "% or $?"},
        {"comment_id": "d", "status": "deferred", "summary": "later"},
    ], schema="prototype-feedback-response/v1")
    assert r.json()["applied"] == 3
    assert _state("p")[0][:2] == ("blocked", "plan X")
    assert _state("n")[0][:2] == ("blocked", "% or $?")
    assert _state("d")[0][:2] == ("blocked", "later")


def test_answered_llm_is_verified_human_is_done(client):
    _seed([("l", "doing", "llm"), ("h", "new", "human")])
    r = _post(client, [{"comment_id": "l", "status": "answered", "answer": "yes"},
                       {"comment_id": "h", "status": "answered", "answer": "monthly"}])
    assert r.json()["applied"] == 2
    assert _state("l")[0][:2] == ("verified", "yes")
    assert _state("h")[0][:2] == ("done", "monthly")
    assert _state("h")[1] == [("new", "done", "drain")]


def test_fixed_on_human_comment_is_done(client):
    _seed([("h", "new", "human")])
    assert _post(client, [{"comment_id": "h", "status": "fixed", "summary": "ok"}]).json()["applied"] == 1
    assert _state("h")[0][0] == "done"


def test_skipped_and_wontfix_become_skipped(client):
    _seed([("s", "queued", "llm"), ("w", "new", "llm")])
    r = _post(client, [{"comment_id": "s", "status": "skipped", "summary": "dup"},
                       {"comment_id": "w", "status": "wontfix", "summary": "by design"}])
    assert r.json()["applied"] == 2
    assert _state("s")[0][0] == "skipped" and _state("w")[0][0] == "skipped"


def test_unknown_ids_are_counted_not_errors(client):
    _seed([("a", "doing", "llm")])
    r = _post(client, [{"comment_id": "ghost", "status": "fixed"}, {"comment_id": "a", "status": "fixed"}])
    assert r.status_code == 200
    assert r.json() == {"matched": 1, "unmatched": 1, "applied": 1, "rejected": []}


def test_transition_rules_are_respected_with_reasons(client):
    _seed([("new1", "new", "llm"), ("dn", "done", "llm"), ("bl", "blocked", "llm"), ("v", "verified", "llm")])
    r = _post(client, [{"comment_id": "new1", "status": "fixed"}, {"comment_id": "dn", "status": "fixed"},
                       {"comment_id": "bl", "status": "fixed"}, {"comment_id": "v", "status": "fixed"},
                       {"comment_id": "v", "status": "weird"}]).json()
    assert r["matched"] == 5 and r["applied"] == 0
    assert {x["id"] for x in r["rejected"]} == {"new1", "dn", "bl", "v"}
    assert all(x["reason"] for x in r["rejected"])
    assert _state("new1")[0][0] == "new" and _state("dn")[0][0] == "done"


def test_verified_can_be_downgraded_to_blocked(client):
    _seed([("v", "verified", "llm")])
    assert _post(client, [{"comment_id": "v", "status": "needs_input", "question_back": "q?"}]).json()["applied"] == 1
    assert _state("v")[0][:2] == ("blocked", "q?")


def test_multipart_file_upload(client):
    _seed([("a", "doing", "llm")])
    body = json.dumps({"schema": "prototype-feedback-response/v2",
                       "responses": [{"comment_id": "a", "status": "fixed", "summary": "x"}]})
    r = client.post("/api/responses", files={"file": ("responses.json", body, "application/json")})
    assert r.json()["applied"] == 1 and _state("a")[0][0] == "verified"


def test_bad_input_is_400(client):
    assert client.post("/api/responses", json={"schema": "other/v1", "responses": []}).status_code == 400
    assert client.post("/api/responses", json={"schema": "prototype-feedback-response/v2"}).status_code == 400
    assert client.post("/api/responses", files={"file": ("r.json", "not json", "application/json")}).status_code == 400
    assert client.post("/api/responses", content=b"[1]", headers={"content-type": "application/json"}).status_code == 400


def test_cross_site_browser_write_still_refused(client):
    r = client.post("/api/responses", json={"schema": "prototype-feedback-response/v2", "responses": []},
                    headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
