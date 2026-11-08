# tests/test_ingest_idempotent.py
import pytest
from fastapi import HTTPException

import main


def _c(cid, text="t", edited_at=None):
    return {"id": cid, "page_url": "https://a.dev/", "selector": "h1", "bbox": {},
            "viewport": {"w": 1280}, "comment": text, "created_at": "2026-11-01T10:00:00Z",
            "edited_at": edited_at}


def _p(*comments):
    return {"schema": "prototype-feedback/v5", "reviewer": "R", "comments": list(comments)}


def _batches():
    with main.db() as c:
        return c.execute("SELECT id, comment_count FROM batches ORDER BY ingested_at").fetchall()


def test_reexport_with_one_new_comment_makes_a_batch_of_one():
    first = main.ingest_payload_ex(_p(_c("a"), _c("b")), "r1.zip")
    second = main.ingest_payload_ex(_p(_c("a"), _c("b"), _c("c")), "r2.zip")
    assert (first["new"], second["new"], second["known"]) == (2, 1, 2)
    assert second["batch_id"] != first["batch_id"]
    with main.db() as c:
        rows = {r["id"]: r["batch_id"] for r in c.execute("SELECT id, batch_id FROM comments")}
    assert rows == {"a": first["batch_id"], "b": first["batch_id"], "c": second["batch_id"]}
    assert [b["comment_count"] for b in _batches()] == [2, 1]


def test_reexport_keeps_server_status():
    main.ingest_payload_ex(_p(_c("a")), "r1.zip")
    with main.db() as c:
        c.execute("UPDATE comments SET status='verified', commit_sha='abc' WHERE id='a'")
    main.ingest_payload_ex(_p(_c("a")), "r2.zip")
    with main.db() as c:
        r = c.execute("SELECT status, commit_sha FROM comments WHERE id='a'").fetchone()
    assert (r["status"], r["commit_sha"]) == ("verified", "abc")


def test_all_known_makes_no_new_batch_and_returns_the_original():
    first = main.ingest_payload_ex(_p(_c("a")), "r1.zip")
    again = main.ingest_payload_ex(_p(_c("a")), "r1-again.zip")
    assert again == {"batch_id": first["batch_id"], "new": 0, "known": 1, "updated": 0}
    assert len(_batches()) == 1


def test_newer_edit_updates_text_older_does_not():
    main.ingest_payload_ex(_p(_c("a", "old", "2026-11-02T00:00:00Z")), "r1.zip")
    up = main.ingest_payload_ex(_p(_c("a", "new", "2026-11-03T00:00:00Z")), "r2.zip")
    stale = main.ingest_payload_ex(_p(_c("a", "stale", "2026-11-01T00:00:00Z")), "r3.zip")
    with main.db() as c:
        text = c.execute("SELECT comment FROM comments WHERE id='a'").fetchone()[0]
    assert (up["updated"], stale["updated"], text) == (1, 0, "new")


def test_ingest_payload_still_returns_a_batch_id_string():
    assert isinstance(main.ingest_payload(_p(_c("a")), "r1.zip"), str)


def test_api_ingest_reports_counts(client):
    import io, json, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("feedback.json", json.dumps(_p(_c("a"))))
    for _ in range(2):
        r = client.post("/api/ingest", files={"files": ("r.zip", buf.getvalue(), "application/zip")})
    res = r.json()["results"][0]
    assert (res["ok"], res["new"], res["known"]) == (True, 0, 1)


def test_failed_ingest_removes_its_batch_dir(tmp_data_dir):
    bad = _p(_c("x"))
    bad["comments"][0]["screenshot_path"] = "../../escape.png"
    before = set((tmp_data_dir / "batches").iterdir())
    with pytest.raises(HTTPException):
        main.ingest_payload_ex(bad, "bad.zip", source_dir=tmp_data_dir / "nonexistent-src")
    assert set((tmp_data_dir / "batches").iterdir()) == before
    assert _batches() == []
