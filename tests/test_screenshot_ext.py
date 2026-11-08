# tests/test_screenshot_ext.py
import json

import pytest

import main


def _c(cid, shot=None, viewport=None):
    cm = {"id": cid, "page_url": "https://a.dev/", "selector": "h1", "bbox": {},
          "viewport": {"w": 1280}, "comment": "t", "created_at": "2026-11-01T10:00:00Z",
          "edited_at": None}
    if shot:
        cm["screenshot_path"] = shot
    if viewport:
        cm["viewport_screenshot_path"] = viewport
    return cm


def _src(tmp_path, files):
    """Build an unzipped v6 export folder with the given {relative_path: bytes}."""
    src = tmp_path / "export"
    for rel, data in files.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(data)
    (src / "feedback.json").write_text(json.dumps(
        {"schema": "prototype-feedback/v6", "reviewer": "R", "comments": list(files_comments(files))}))
    return src


def files_comments(files):
    return [_c(rel.split("/")[-1].split(".")[0], shot=rel)
            for rel in files if rel.startswith("screenshots/")]


def _batch_row(cid):
    with main.db() as c:
        return c.execute("SELECT batch_id, screenshot_path FROM comments WHERE id = ?", (cid,)).fetchone()


def test_webp_screenshot_keeps_webp_suffix(tmp_path):
    src = _src(tmp_path, {"screenshots/c1.webp": b"RIFFdata"})
    payload = json.loads((src / "feedback.json").read_text())
    res = main.ingest_payload_ex(payload, "r.zip", source_dir=src)
    row = _batch_row("c1")
    assert row["screenshot_path"] == f"batches/{res['batch_id']}/screenshots/c1.webp"
    assert (main.DATA / row["screenshot_path"]).read_bytes() == b"RIFFdata"


@pytest.mark.parametrize("name,expected_ext", [
    ("c1.png", ".png"), ("c1.jpg", ".jpg"), ("c1.jpeg", ".jpeg"), ("c1.WEBP", ".webp"),
    ("c1.gif", ".png"), ("c1", ".png"),
])
def test_screenshot_suffix_is_kept_or_defaults_to_png(tmp_path, name, expected_ext):
    src = _src(tmp_path, {f"screenshots/{name}": b"img"})
    payload = json.loads((src / "feedback.json").read_text())
    main.ingest_payload_ex(payload, "r.zip", source_dir=src)
    row = _batch_row("c1")
    assert row["screenshot_path"].endswith("/screenshots/c1" + expected_ext)
    assert (main.DATA / row["screenshot_path"]).exists()


def test_viewport_screenshot_keeps_webp_suffix(tmp_path):
    src = _src(tmp_path, {"viewport/v1.webp": b"vp"})
    payload = {"schema": "prototype-feedback/v6", "reviewer": "R",
               "comments": [_c("c1", viewport="viewport/v1.webp")]}
    res = main.ingest_payload_ex(payload, "r.zip", source_dir=src)
    with main.db() as c:
        vp = c.execute("SELECT viewport_path FROM comments WHERE id = 'c1'").fetchone()[0]
    assert vp == f"batches/{res['batch_id']}/viewport-screenshots/c1.webp"
    assert (main.DATA / vp).read_bytes() == b"vp"
