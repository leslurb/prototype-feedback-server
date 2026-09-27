import main


def test_queue_groups_by_project_then_page_lane(client):
    with main.db() as c:
        c.execute("""INSERT INTO projects (id, created_at, updated_at, name, repo, local_path)
                     VALUES ('p1','t','t','abdry','r/a','C:/abdry')""")
        c.execute("INSERT INTO batches (id, ingested_at) VALUES ('b1','t')")
        rows = [("c1", "https://x.dev/products?a=1", "queued"), ("c2", "https://x.dev/products/", "queued"),
                ("c3", "https://x.dev/", "queued"), ("c4", "https://x.dev/", "new")]
        c.executemany("""INSERT INTO comments (id, batch_id, page_url, status, recipient, project_id, comment, expected_result)
                         VALUES (?, 'b1', ?, ?, 'llm', 'p1', 'fix', 'fixed')""", rows)
    body = client.get("/api/queue").json()
    assert len(body["projects"]) == 1
    p = body["projects"][0]
    assert (p["repo"], p["local_path"]) == ("r/a", "C:/abdry")
    lanes = {lane["page"]: [cm["id"] for cm in lane["comments"]] for lane in p["lanes"]}
    assert lanes == {"/products": ["c1", "c2"], "/": ["c3"]}
    assert body["total"] == 3


def test_queue_is_empty_list_not_error_when_nothing_queued(client):
    body = client.get("/api/queue").json()
    assert body == {"projects": [], "total": 0}


def test_queued_comment_with_no_repo_is_unqueued_visibly(client):
    with main.db() as c:
        c.execute("""INSERT INTO projects (id, created_at, updated_at, name)
                     VALUES ('p2','t','t','no-repo-project')""")
        c.execute("INSERT INTO batches (id, ingested_at) VALUES ('b2','t')")
        c.execute("""INSERT INTO comments (id, batch_id, page_url, status, recipient, project_id, comment, expected_result)
                     VALUES ('lost', 'b2', 'https://x.dev/', 'queued', 'llm', 'p2', 'fix', 'fixed')""")
        c.execute("""INSERT INTO projects (id, created_at, updated_at, name, repo, local_path)
                     VALUES ('p3','t','t','abdry','r/a','C:/abdry')""")
        c.execute("INSERT INTO batches (id, ingested_at) VALUES ('b3','t')")
        c.execute("""INSERT INTO comments (id, batch_id, page_url, status, recipient, project_id, comment, expected_result)
                     VALUES ('ok1', 'b3', 'https://x.dev/', 'queued', 'llm', 'p3', 'fix', 'fixed')""")
    body = client.get("/api/queue").json()
    ids = [cm["id"] for p in body["projects"] for lane in p["lanes"] for cm in lane["comments"]]
    assert "lost" not in ids
    assert ids == ["ok1"]
    assert body["total"] == 1
    with main.db() as c:
        row = c.execute("SELECT status FROM comments WHERE id = 'lost'").fetchone()
        assert row["status"] == "new"
        ev = c.execute(
            "SELECT from_status, to_status, actor, note FROM status_events WHERE comment_id = 'lost'"
        ).fetchone()
        assert (ev["from_status"], ev["to_status"], ev["actor"], ev["note"]) == (
            "queued", "new", "user", "unqueued: no repo mapped")
