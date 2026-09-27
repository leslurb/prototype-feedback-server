import main

COMMENT_COLS = {"status", "recipient", "expected_result", "verify_selector", "viewport_w",
                "lane", "run_id", "commit_sha", "after_path", "verified_at", "verify_json",
                "repeat_of", "repeat_kind", "status_note", "updated_at"}
BATCH_COLS = {"kind", "repo_hints_json", "shipped_at", "rereview_sent_at"}
PROJECT_COLS = {"repo", "local_path", "url_prefixes_json", "preview_cmd", "preview_url"}


def _cols(c, table):
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}


def test_migration_adds_columns_and_tables():
    with main.db() as c:
        assert COMMENT_COLS <= _cols(c, "comments")
        assert BATCH_COLS <= _cols(c, "batches")
        assert PROJECT_COLS <= _cols(c, "projects")
        assert {"id", "started_at", "heartbeat_at", "ended_at", "cap", "status", "summary"} <= _cols(c, "runs")
        assert {"id", "comment_id", "at", "from_status", "to_status", "actor", "note", "run_id"} <= _cols(c, "status_events")


def test_migration_is_idempotent_and_defaults_are_safe():
    main.init_db()  # second run must not raise
    with main.db() as c:
        c.execute("INSERT INTO batches (id, ingested_at) VALUES ('b1', 'now')")
        c.execute("INSERT INTO comments (id, batch_id) VALUES ('c1', 'b1')")
        row = c.execute("SELECT status, recipient FROM comments WHERE id='c1'").fetchone()
        kind = c.execute("SELECT kind FROM batches WHERE id='b1'").fetchone()["kind"]
    assert (row["status"], row["recipient"], kind) == ("new", "llm", "export")
