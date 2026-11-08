import main

EXT = "chrome-extension://lkpciiloidfgdhlgaefcbggajlfljmbp"
URL = "/api/projects"   # any unsafe route; body content is irrelevant to the guard


def test_cross_site_simple_post_is_refused(client):
    r = client.post(URL, content="name=x", headers={
        "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site",
        "Content-Type": "text/plain"})
    assert r.status_code == 403
    assert r.json() == {"detail": "cross-site write refused"}


def test_foreign_origin_without_fetch_metadata_is_refused(client):
    r = client.post(URL, json={"name": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_same_site_other_port_is_refused(client):
    r = client.post(URL, json={"name": "x"}, headers={
        "Origin": "http://localhost:3000", "Sec-Fetch-Site": "same-site"})
    assert r.status_code == 403


def test_extension_origin_is_allowed(client):
    # Spike 2026-11-08: Chrome sends Sec-Fetch-Site: cross-site from the extension.
    r = client.post(URL, json={"name": "x"}, headers={"Origin": EXT, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code != 403


def test_dashboard_same_origin_is_allowed(client):
    r = client.post(URL, json={"name": "x"}, headers={
        "Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code != 403


def test_no_origin_no_metadata_is_allowed(client):
    # curl, scripts, the /feedback drain: no browser, no ambient credentials to abuse.
    r = client.post(URL, json={"name": "x"})
    assert r.status_code != 403


def test_reads_are_never_guarded(client):
    r = client.get("/api/overview", headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200
