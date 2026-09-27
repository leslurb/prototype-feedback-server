import json

import main


def _payload(**over):
    p = {
        "schema": "prototype-feedback/v5",
        "reviewer": "Mike",
        "project_id": "proj1",
        "repo_hints": {"https://a.dev": {"repo": "r/a"}},
        "comments": [{
            "id": "c-new", "page_url": "https://a.dev/?x=1", "selector": "footer .logo img",
            "bbox": {"x": 0, "y": 0, "w": 10, "h": 10}, "viewport": {"w": 390, "h": 844},
            "comment": "logo blurry", "recipient_resolved": "human",
        }],
    }
    p.update(over)
    return p


def _project():
    with main.db() as c:
        c.execute("INSERT INTO projects (id, created_at, updated_at, name, repo) VALUES ('proj1','t','t','P','r/a')")


def test_ingest_stores_recipient_hints_and_viewport():
    _project()
    bid = main.ingest_payload(_payload(), "x.json")
    with main.db() as c:
        cm = c.execute("SELECT * FROM comments WHERE id='c-new'").fetchone()
        b = c.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
    assert (cm["recipient"], cm["viewport_w"], cm["status"]) == ("human", 390, "new")
    assert json.loads(b["repo_hints_json"]) == {"https://a.dev": {"repo": "r/a"}}
    assert b["kind"] == "export"


def test_empty_recipient_falls_back_to_llm_like_the_extension():
    _project()
    p = _payload()
    p["comments"][0]["recipient_resolved"] = ""
    main.ingest_payload(p, "x.json")
    with main.db() as c:
        assert c.execute("SELECT recipient FROM comments WHERE id='c-new'").fetchone()[0] == "llm"


def test_ingest_flags_regression_of_a_shipped_done_comment():
    _project()
    with main.db() as c:
        c.execute("INSERT INTO batches (id, ingested_at, shipped_at) VALUES ('old','2026-09-01','2026-09-02')")
        c.execute("""INSERT INTO comments (id, batch_id, page_url, selector, bbox_json, status, project_id)
                     VALUES ('c-old','old','https://a.dev/','footer .logo img','{}','done','proj1')""")
    main.ingest_payload(_payload(), "x.json")
    with main.db() as c:
        cm = c.execute("SELECT repeat_of, repeat_kind FROM comments WHERE id='c-new'").fetchone()
    assert (cm["repeat_of"], cm["repeat_kind"]) == ("c-old", "regression")


def test_ingest_stores_top_level_project_id_on_the_batch():
    _project()
    bid = main.ingest_payload(_payload(), "x.json")
    with main.db() as c:
        assert c.execute("SELECT project_id FROM batches WHERE id=?", (bid,)).fetchone()[0] == "proj1"


def test_ingest_without_project_id_uses_the_single_auto_tagged_project():
    _project()
    with main.db() as c:
        c.execute("""INSERT INTO project_reviewers (id, project_id, email, name, status)
                     VALUES ('r1','proj1','mike@x.dev','Mike','invited')""")
    p = _payload(reviewer_email="mike@x.dev")
    del p["project_id"]
    p["comments"].append({**p["comments"][0], "id": "c-two"})
    bid = main.ingest_payload(p, "x.json")
    with main.db() as c:
        assert c.execute("SELECT project_id FROM batches WHERE id=?", (bid,)).fetchone()[0] == "proj1"


def test_ingest_leaves_batch_project_null_when_comments_span_projects():
    _project()
    with main.db() as c:
        c.execute("INSERT INTO projects (id, created_at, updated_at, name) VALUES ('proj2','t','t','Q')")
    p = _payload()
    del p["project_id"]
    p["comments"][0]["project_id"] = "proj1"
    p["comments"].append({**p["comments"][0], "id": "c-two", "project_id": "proj2"})
    bid = main.ingest_payload(p, "x.json")
    with main.db() as c:
        assert c.execute("SELECT project_id FROM batches WHERE id=?", (bid,)).fetchone()[0] is None
