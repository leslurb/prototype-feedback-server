import io, json, zipfile

import main


def _zip(payload):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("feedback.json", json.dumps(payload))
    return buf.getvalue()


def test_claude_input_carries_v5_fields_and_status(client, tmp_data_dir):
    payload = {
        "schema": "prototype-feedback/v5", "reviewer": "R",
        "repo_hints": {"https://a.dev": {"repo": "https://github.com/x/a"}},
        "version_hints": {"a.dev": {"commit": "a1f3c9e", "source": "meta"}},
        "comments": [{
            "id": "c1", "page_url": "https://a.dev/", "selector": "h1", "bbox": {}, "viewport": {"w": 1280},
            "comment": "fix it", "intent": "bug", "severity": "high", "recipient_resolved": "llm",
            "element_snapshot": {"tag": "h1"}, "version_hint": {"commit": "a1f3c9e"},
        }],
    }
    res = client.post("/api/ingest", files={"files": ("r.zip", _zip(payload), "application/zip")}).json()
    bid = res["results"][0]["batch_id"]
    with main.db() as c:
        c.execute("UPDATE comments SET status='queued', expected_result='title fits' WHERE id='c1'")
    main.write_claude_artifacts(bid)
    doc = json.loads((tmp_data_dir / "batches" / bid / "claude_input.json").read_text(encoding="utf-8"))
    assert doc["schema"] == "prototype-feedback/v5-slim"
    assert doc["repo_hints"] == payload["repo_hints"]
    assert doc["version_hints"] == payload["version_hints"]
    cm = doc["comments"][0]
    assert (cm["intent"], cm["severity"], cm["recipient"], cm["status"], cm["expected_result"]) == \
        ("bug", "high", "llm", "queued", "title fits")
    assert cm["element_snapshot"] == {"tag": "h1"}
    assert cm["version_hint"] == {"commit": "a1f3c9e"}
    assert "viewport_file" in cm
    assert any("responses.json" in step for step in doc["_llm_guide"]["how_to_apply"])


def test_claude_input_without_feedback_json_still_writes(tmp_data_dir):
    bid = main.ingest_payload({"schema": "prototype-feedback/v2", "comments": [
        {"id": "z", "page_url": "https://a.dev/", "comment": "x", "bbox": {}, "viewport": {}}]}, "old.json")
    doc = json.loads((tmp_data_dir / "batches" / bid / "claude_input.json").read_text(encoding="utf-8"))
    assert doc["comments"][0]["intent"] is None and doc["comments"][0]["status"] == "new"
