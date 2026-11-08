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
            "framework_hint": {"name": "next"}, "sort_index": 3,
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
    assert cm["viewport_file"] is None
    assert cm["framework_hint"] == {"name": "next"}
    assert cm["sort_index"] == 3
    assert any("responses.json" in step for step in doc["_llm_guide"]["how_to_apply"])


def test_claude_input_without_feedback_json_still_writes(tmp_data_dir):
    bid = main.ingest_payload({"schema": "prototype-feedback/v2", "comments": [
        {"id": "z", "page_url": "https://a.dev/", "comment": "x", "bbox": {}, "viewport": {}}]}, "old.json")
    doc = json.loads((tmp_data_dir / "batches" / bid / "claude_input.json").read_text(encoding="utf-8"))
    assert doc["comments"][0]["intent"] is None and doc["comments"][0]["status"] == "new"


def test_claude_json_route_reflects_current_status(client, tmp_data_dir):
    bid = main.ingest_payload({"schema": "prototype-feedback/v5", "comments": [
        {"id": "s1", "page_url": "https://a.dev/", "comment": "x", "bbox": {}, "viewport": {}}]}, "s.json")
    with main.db() as c:
        c.execute("UPDATE comments SET status='done' WHERE id='s1'")
    doc = client.get(f"/batch/{bid}/json").json()
    assert doc["comments"][0]["status"] == "done"
    assert client.get("/batch/nope/json").status_code == 404


def _v6_doc(client, tmp_data_dir, guide=True):
    import base64
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    payload = {
        "schema": "prototype-feedback/v6", "reviewer": "R",
        "round_note": {"what_changed": "new nav", "preview_url": "https://p.dev", "focus": None,
                       "reviewer_is_developer": False, "written_at": "t"},
        "repo": "https://github.com/x/a", "session": {"id": "s1", "round": 2},
        "prior_commits": [{"comment_id": "c0", "commit_sha": "abc", "files_changed": ["a.css"],
                           "state": "fixed", "acted_at": "t"}],
        "export_kind": "send_claude_code",
        "comments": [{
            "id": "c1", "page_url": "https://a.dev/", "selector": "h1", "bbox": {}, "viewport": {"w": 1},
            "comment": "again", "screenshot_path": "screenshots/c1.webp",
            "verb": "fix", "urgent": True, "round": 2, "build_key": "b2",
            "history": [{"kind": "reopen", "at": "2026-11-03T00:00:00Z"}],
            "status": {"state": "fixed", "at": "2026-11-02T00:00:00Z"},
            "response": {"commit_sha": "abc", "files_changed": ["a.css"]},
            "previous_comment": "old text", "screenshot_error": None,
            "attachments": [{"type": "image", "name": "x.png", "path": "attachments/x.png"}],
        }],
    }
    if guide:
        payload["_llm_guide"] = {"purpose": "EXPORT-OWN-GUIDE", "how_to_apply": ["step"]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("feedback.json", json.dumps(payload))
        z.writestr("screenshots/c1.webp", png)
        z.writestr("attachments/x.png", png)
    res = client.post("/api/ingest", files={"files": ("r.zip", buf.getvalue(), "application/zip")}).json()
    assert res["results"][0]["ok"], res
    bid = res["results"][0]["batch_id"]
    out = tmp_data_dir / "batches" / bid
    return json.loads((out / "claude_input.json").read_text(encoding="utf-8")), out


def test_v6_pass_through_and_absolute_paths(client, tmp_data_dir):
    doc, out = _v6_doc(client, tmp_data_dir)
    cm = doc["comments"][0]
    assert (cm["verb"], cm["urgent"], cm["round"], cm["build_key"]) == ("fix", True, 2, "b2")
    assert cm["history"][0]["kind"] == "reopen" and cm["response"]["commit_sha"] == "abc"
    assert cm["status"] == {"state": "fixed", "at": "2026-11-02T00:00:00Z"}
    assert cm["server_status"] == "new"
    assert cm["previous_comment"] == "old text" and cm["screenshot_error"] is None
    assert doc["round_note"]["what_changed"] == "new nav" and doc["repo"] == "https://github.com/x/a"
    assert doc["session"]["id"] == "s1" and doc["prior_commits"][0]["commit_sha"] == "abc"
    assert doc["export_kind"] == "send_claude_code"
    import os
    assert os.path.isabs(cm["screenshot_file"]) and cm["screenshot_file"].endswith("c1.webp")
    assert os.path.exists(cm["screenshot_file"])
    assert os.path.isabs(cm["attachments"][0]["file_path"]) and os.path.exists(cm["attachments"][0]["file_path"])


def test_v6_embeds_export_guide_verbatim_plus_preamble(client, tmp_data_dir):
    doc, out = _v6_doc(client, tmp_data_dir)
    assert doc["_llm_guide"] == {"purpose": "EXPORT-OWN-GUIDE", "how_to_apply": ["step"]}
    pre = " ".join(doc["_server_preamble"])
    assert "If you are running the pfb plugin's /pfb:feedback skill, its steps win over this guide. " \
           "Otherwise follow this guide." in pre
    assert "curl -F file=@responses.json http://127.0.0.1:8090/api/responses" in pre
    prompt = (out / "claude_prompt.md").read_text(encoding="utf-8")
    assert "curl -F file=@responses.json http://127.0.0.1:8090/api/responses" in prompt
    assert "steps win over this guide" in prompt


def test_fallback_guide_has_no_png_claim_and_mentions_return_path(client, tmp_data_dir):
    doc, _ = _v6_doc(client, tmp_data_dir, guide=False)
    ref = json.dumps(doc["_llm_guide"]["field_reference"])
    assert "a PNG" not in ref and "WebP" in ref
    assert any("responses.json" in s for s in doc["_llm_guide"]["how_to_apply"])
