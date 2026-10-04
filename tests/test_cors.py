import main

EXT = "chrome-extension://lkpciiloidfgdhlgaefcbggajlfljmbp"


def _preflight(client, origin):
    return client.options("/api/comments/queue", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})


def test_foreign_origin_is_refused(client):
    r = _preflight(client, "https://evil.example")
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers


def test_published_extension_is_allowed(client):
    r = _preflight(client, EXT)
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == EXT


def test_extra_origins_parsed_from_env():
    assert main.cors_origins("chrome-extension://dev1, http://x.test ,") == [
        EXT, "chrome-extension://dev1", "http://x.test"]
