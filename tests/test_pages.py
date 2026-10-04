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
