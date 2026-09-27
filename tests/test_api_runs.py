import main


def test_run_lifecycle(client):
    rid = client.post("/api/runs", json={"cap": 3}).json()["id"]
    assert client.post(f"/api/runs/{rid}/heartbeat").status_code == 200
    r = client.post(f"/api/runs/{rid}/end", json={"status": "finished", "summary": {"verified": 3, "blocked": 1}})
    assert r.status_code == 200
    latest = client.get("/api/runs/latest").json()
    assert (latest["id"], latest["status"], latest["summary"]["verified"]) == (rid, "finished", 3)


def test_stale_doing_is_reported_when_run_heartbeat_is_old_or_run_ended(client):
    with main.db() as c:
        c.execute("INSERT INTO runs (id, started_at, heartbeat_at, status) VALUES ('old','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','running')")
        c.execute("INSERT INTO runs (id, started_at, heartbeat_at, status) VALUES ('live', ?, ?, 'running')",
                  (main.now_iso(), main.now_iso()))
        c.execute("INSERT INTO batches (id, ingested_at) VALUES ('b1','t')")
        c.execute("INSERT INTO comments (id, batch_id, status, run_id) VALUES ('stuck','b1','doing','old')")
        c.execute("INSERT INTO comments (id, batch_id, status, run_id) VALUES ('fine','b1','doing','live')")
        c.execute("INSERT INTO comments (id, batch_id, status, run_id) VALUES ('orphan','b1','doing',NULL)")
    ids = sorted(s["id"] for s in client.get("/api/stale?minutes=30").json()["stale"])
    assert ids == ["orphan", "stuck"]


def test_heartbeat_on_unknown_run_is_404(client):
    assert client.post("/api/runs/nope/heartbeat").status_code == 404


def test_run_end_rejects_unknown_status(client):
    rid = client.post("/api/runs", json={"cap": 3}).json()["id"]
    r = client.post(f"/api/runs/{rid}/end", json={"status": "banana"})
    assert r.status_code == 422
    latest = client.get("/api/runs/latest").json()
    assert latest["status"] == "running"


def test_stale_includes_doing_comment_whose_run_ended_recently(client):
    with main.db() as c:
        c.execute(
            "INSERT INTO runs (id, started_at, heartbeat_at, ended_at, status) VALUES ('ended', ?, ?, ?, 'finished')",
            (main.now_iso(), main.now_iso(), main.now_iso()),
        )
        c.execute("INSERT INTO batches (id, ingested_at) VALUES ('b1','t')")
        c.execute("INSERT INTO comments (id, batch_id, status, run_id) VALUES ('ended_doing','b1','doing','ended')")
    ids = [s["id"] for s in client.get("/api/stale?minutes=30").json()["stale"]]
    assert "ended_doing" in ids


def test_heartbeat_and_end_on_an_ended_run_are_409(client):
    rid = client.post("/api/runs", json={"cap": 3}).json()["id"]
    assert client.post(f"/api/runs/{rid}/end", json={"status": "finished"}).status_code == 200
    assert client.post(f"/api/runs/{rid}/heartbeat").status_code == 409
    assert client.post(f"/api/runs/{rid}/end", json={"status": "failed"}).status_code == 409
    assert client.get("/api/runs/latest").json()["status"] == "finished"


def test_end_on_unknown_run_is_404(client):
    assert client.post("/api/runs/nope/end", json={"status": "finished"}).status_code == 404
