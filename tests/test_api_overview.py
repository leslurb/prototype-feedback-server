import main


def _seed():
    with main.db() as c:
        c.execute("INSERT INTO projects (id, created_at, updated_at, name, repo) VALUES ('p1','t','t','abdry','r/a')")
        c.execute("INSERT INTO projects (id, created_at, updated_at, name) VALUES ('p2','t','t','sunsip')")
        # r1/r2 are exports: `project` holds a reviewer-sent LABEL, never the id. Rounds are
        # found through `project_id` (or, for x1 below, through the comment's project_id).
        c.execute("INSERT INTO batches (id, ingested_at, reviewer, project, project_id, kind) "
                  "VALUES ('r1','2026-09-09','Cailyn','abdry label','p1','export')")
        c.execute("INSERT INTO batches (id, ingested_at, reviewer, project, project_id, kind) "
                  "VALUES ('r2','2026-09-18','Mike','abdry label','p1','export')")
        c.execute("INSERT INTO batches (id, ingested_at, reviewer, project, project_id, kind) "
                  "VALUES ('d1','2026-09-20','You','p1','p1','direct')")
        c.execute("INSERT INTO batches (id, ingested_at, reviewer, project, project_id, kind) "
                  "VALUES ('s1','2026-09-24','Cailyn','p2','p2','export')")
        # x1: export batch whose `project` label ('abdry label') is NOT abdry's id and whose
        # `project_id` is NULL -- it must still surface under abdry, via its comment's project_id.
        c.execute("INSERT INTO batches (id, ingested_at, reviewer, project, project_id, kind) "
                  "VALUES ('x1','2026-09-11','Cailyn','abdry label',NULL,'export')")
        rows = [
            ("a", "r1", "done", "llm", "p1"), ("b", "r1", "done", "llm", "p1"), ("h", "r1", "new", "human", "p1"),
            ("c", "r2", "verified", "llm", "p1"), ("d", "r2", "blocked", "llm", "p1"),
            ("e", "d1", "new", "llm", "p1"),
            ("f", "s1", "new", "llm", "p2"),
            ("g", "x1", "new", "llm", "p1"),
        ]
        c.executemany("INSERT INTO comments (id, batch_id, status, recipient, project_id, page_url) VALUES (?,?,?,?,?,'/')", rows)


def test_overview_rounds_states_trend_and_needs_you(client):
    _seed()
    ov = client.get("/api/overview").json()
    projects = {p["name"]: p for p in ov["projects"]}
    abdry = projects["abdry"]
    rounds = {r["id"]: r for r in abdry["rounds"]}
    assert abdry["rounds"][0]["id"] == "d1"                  # "Your issues" first
    assert rounds["r2"]["state"] == "Blocked"
    assert rounds["r1"]["counts"] == {"done": 2}              # human comments are not in LLM counts
    assert rounds["r1"]["human_open"] == 1
    # x1's `project` label ('abdry label') is not abdry's id, and its own project_id is NULL --
    # it only appears under abdry because its comment's project_id is 'p1'.
    assert "x1" in rounds
    # oldest first, across all three abdry exports (r1 09-09, x1 09-11, r2 09-18): 3, 1, 2 rows
    assert abdry["trend"] == [3, 1, 2]
    assert projects["sunsip"]["rounds"][0]["state"] == "Needs repo"
    ny = ov["needs_you"]
    assert (ny["blocked"], ny["to_confirm"], ny["human_open"]) == (1, 1, 1)
    assert ny["unmapped_projects"] == [{"project_id": "p2", "name": "sunsip", "waiting": 1}]
