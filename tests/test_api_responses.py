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
