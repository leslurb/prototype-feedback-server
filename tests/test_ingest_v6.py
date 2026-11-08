import json

import main


def _c(cid, text="t", **extra):
    base = {"id": cid, "page_url": "https://a.dev/", "selector": "h1", "bbox": {},
            "viewport": {"w": 1280}, "comment": text, "created_at": "2026-11-01T10:00:00Z"}
    base.update(extra)
    return base


def _p(*comments, **top):
    return {"schema": "prototype-feedback/v6", "reviewer": "R", "comments": list(comments), **top}


def _row(cid):
    with main.db() as c:
        return c.execute("SELECT * FROM comments WHERE id = ?", (cid,)).fetchone()


def test_new_comment_stores_v6_fields():
    main.ingest_payload_ex(_p(_c("a", verb="fix", urgent=True, round=2,
                                 history=[{"kind": "reply", "at": "2026-11-02T00:00:00Z"}],
                                 status={"state": "fixed", "at": "2026-11-02T01:00:00Z"},
                                 response={"commit_sha": "abc"}, previous_comment="old",
                                 screenshot_error="capture failed")), "r.zip")
    r = _row("a")
    assert (r["verb"], r["urgent"], r["round"]) == ("fix", 1, 2)
    assert json.loads(r["history_json"])[0]["kind"] == "reply"
    assert json.loads(r["status_json"])["state"] == "fixed"
    assert json.loads(r["response_json"]) == {"commit_sha": "abc"}


def test_newer_history_updates_resets_status_and_regenerates(tmp_data_dir):
    first = main.ingest_payload_ex(_p(_c("a", history=[], status={"state": "fixed", "at": "2026-11-02T00:00:00Z"},
                                         round=1)), "r1.zip")
    with main.db() as c:
        c.execute("UPDATE comments SET status='verified' WHERE id='a'")
    again = main.ingest_payload_ex(_p(_c("a", round=2,
                                         history=[{"kind": "reopen", "at": "2026-11-03T00:00:00Z", "note": "no"}],
                                         status={"state": "fixed", "at": "2026-11-02T00:00:00Z"})), "r2.zip")
    assert again["updated"] == 1 and again["batch_id"] == first["batch_id"]
    r = _row("a")
    assert r["status"] == "new" and r["round"] == 2
    doc = json.loads((tmp_data_dir / "batches" / first["batch_id"] / "claude_input.json").read_text(encoding="utf-8"))
    assert doc["comments"][0]["history"][0]["kind"] == "reopen"
    with main.db() as c:
        ev = c.execute("SELECT from_status, to_status FROM status_events WHERE comment_id='a'").fetchall()
    assert [tuple(e) for e in ev] == [("verified", "new")]


def test_newer_status_without_reset_kind_keeps_server_status():
    main.ingest_payload_ex(_p(_c("a", status={"state": "proposed", "at": "2026-11-02T00:00:00Z"})), "r1.zip")
    with main.db() as c:
        c.execute("UPDATE comments SET status='verified' WHERE id='a'")
    up = main.ingest_payload_ex(_p(_c("a", status={"state": "confirmed", "at": "2026-11-04T00:00:00Z"},
                                      history=[{"kind": "note", "at": "2026-11-01T00:00:00Z"}])), "r2.zip")
    r = _row("a")
    assert up["updated"] == 1 and r["status"] == "verified"
    assert json.loads(r["status_json"])["state"] == "confirmed"


def test_same_or_older_timestamps_are_not_updates():
    pl = _p(_c("a", history=[{"kind": "reply", "at": "2026-11-03T00:00:00Z"}],
               status={"state": "fixed", "at": "2026-11-03T00:00:00Z"}))
    main.ingest_payload_ex(pl, "r1.zip")
    with main.db() as c:
        c.execute("UPDATE comments SET status='verified' WHERE id='a'")
    again = main.ingest_payload_ex(pl, "r2.zip")
    assert again["updated"] == 0 and _row("a")["status"] == "verified"


def test_v5_comments_without_v6_fields_still_ingest():
    main.ingest_payload_ex({"schema": "prototype-feedback/v5", "comments": [_c("z")]}, "o.json")
    r = _row("z")
    assert r["history_json"] is None and r["verb"] is None
