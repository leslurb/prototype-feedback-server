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


def test_answered_llm_is_verified_with_answer_note(client):
    _seed([("l", "doing", "llm")])
    assert _post(client, [{"comment_id": "l", "status": "answered", "answer": "yes"}]).json()["applied"] == 1
    assert _state("l")[0][:2] == ("verified", "yes")


def test_human_answered_only_stores_note_without_status_change(client):
    _seed([("h", "new", "human")])
    r = _post(client, [{"comment_id": "h", "status": "answered", "answer": "monthly"}]).json()
    assert r["applied"] == 1
    assert _state("h") == (("new", "monthly", None), [])


def test_human_comment_rejects_other_statuses(client):
    _seed([("h", "new", "human")])
    r = _post(client, [{"comment_id": "h", "status": "fixed", "summary": "x"}]).json()
    assert r["applied"] == 0 and r["rejected"][0]["id"] == "h"
    assert _state("h") == (("new", None, None), [])


def test_new_comment_is_queued_by_user_then_driven_by_drain(client):
    _seed([("a", "new", "llm")])
    assert _post(client, [{"comment_id": "a", "status": "fixed", "summary": "s", "commit_sha": "c1"}]).json()["applied"] == 1
    st, ev = _state("a")
    assert st == ("verified", "s", "c1")
    assert ev == [("new", "queued", "user"), ("queued", "doing", "drain"), ("doing", "verified", "drain")]


def test_blocked_comment_is_requeued_then_driven(client):
    _seed([("a", "blocked", "llm")])
    assert _post(client, [{"comment_id": "a", "status": "fixed", "summary": "s"}]).json()["applied"] == 1
    st, ev = _state("a")
    assert st[0] == "verified"
    assert ev == [("blocked", "queued", "user"), ("queued", "doing", "drain"), ("doing", "verified", "drain")]


def test_skipped_and_wontfix_become_blocked_with_agent_note(client):
    _seed([("s", "queued", "llm"), ("w", "new", "llm")])
    r = _post(client, [{"comment_id": "s", "status": "skipped", "summary": "dup"},
                       {"comment_id": "w", "status": "wontfix", "summary": "by design"}])
    assert r.json()["applied"] == 2
    assert _state("s")[0][:2] == ("blocked", "Skipped by agent: dup")
    assert _state("w")[0][:2] == ("blocked", "Skipped by agent: by design")
    assert all(e[2] in ("user", "drain") for cid in "sw" for e in _state(cid)[1])


def test_verification_url_is_appended_to_the_note(client):
    _seed([("a", "doing", "llm")])
    _post(client, [{"comment_id": "a", "status": "fixed", "summary": "s", "verification_url": "https://p.dev/x"}])
    assert _state("a")[0][1] == "s · Verify: https://p.dev/x"


def test_in_progress_moves_queued_to_doing(client):
    _seed([("a", "queued", "llm")])
    assert _post(client, [{"comment_id": "a", "status": "in_progress"}]).json()["applied"] == 1
    assert _state("a")[0][0] == "doing"


def test_unknown_ids_are_counted_not_errors(client):
    _seed([("a", "doing", "llm")])
    r = _post(client, [{"comment_id": "ghost", "status": "fixed"}, {"comment_id": "a", "status": "fixed"}])
    assert r.status_code == 200
    assert r.json() == {"matched": 1, "unmatched": 1, "applied": 1, "rejected": []}


def test_transition_rules_are_respected_with_reasons(client):
    _seed([("dn", "done", "llm"), ("sk", "skipped", "llm"), ("v", "verified", "llm")])
    r = _post(client, [{"comment_id": "dn", "status": "fixed"}, {"comment_id": "sk", "status": "fixed"},
                       {"comment_id": "v", "status": "fixed"}, {"comment_id": "v", "status": "weird"}]).json()
    assert r["matched"] == 4 and r["applied"] == 0
    assert {x["id"] for x in r["rejected"]} == {"dn", "sk", "v"} and len(r["rejected"]) == 4
    assert all(x["reason"] for x in r["rejected"])
    assert _state("dn")[0][0] == "done" and _state("sk")[0][0] == "skipped"


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
