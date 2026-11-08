# tests/test_file_mime.py
import pytest

import main


def _put(rel, data=b"x"):
    p = main.DATA / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return rel


@pytest.mark.parametrize("name,mime", [
    ("c.webp", "image/webp"), ("c.WEBP", "image/webp"), ("c.jpg", "image/jpeg"),
    ("c.jpeg", "image/jpeg"), ("c.png", "image/png"), ("c.bin", "application/octet-stream"),
])
def test_data_url_mime_follows_suffix(name, mime):
    rel = _put(f"mimetest/data-url/{name}")
    assert main._file_to_data_url(rel).startswith(f"data:{mime};base64,")


def test_explicit_mime_still_wins():
    rel = _put("mimetest/explicit/c.png")
    assert main._file_to_data_url(rel, "application/pdf").startswith("data:application/pdf;base64,")


def test_file_route_serves_webp_as_image_webp(client):
    rel = _put("mimetest/route/c.webp", b"RIFF")
    r = client.get(f"/file/{rel}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert r.content == b"RIFF"
