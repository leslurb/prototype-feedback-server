import re

import main


def test_batches_page_keeps_ingest_and_list(client):
    html = client.get("/batches").text
    assert "Drop a feedback export to ingest" in html
    assert "Recent batches" in html


def test_nav_links_dashboard_batches_new_issue(client):
    html = client.get("/batches").text
    for href in ('href="/"', 'href="/batches"', 'href="/new-issue"', 'href="/projects"'):
        assert href in html


def test_delete_batch_redirects_to_batches(client):
    with main.db() as c:
        c.execute("INSERT INTO batches (id, ingested_at) VALUES ('del1','t')")
    r = client.post("/api/batch/del1/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/batches"
    with main.db() as c:
        assert c.execute("SELECT id FROM batches WHERE id='del1'").fetchone() is None
