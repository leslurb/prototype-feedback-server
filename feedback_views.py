"""View models for the dashboard pages. Pure: plain data in, plain data out."""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

import feedback_state as fs

SEG_ORDER = ("done", "verified", "doing", "queued", "new", "blocked", "skipped")
STATE_CLASS = {
    "Needs repo": "bad", "Running": "warn", "Queued": "info", "Blocked": "bad",
    "Open": "plain", "To confirm": "info", "Ready to ship": "good",
    "Re-review sent": "purple", "Shipped": "plain", "Human only": "plain",
}
# (from, to, label, button kind). Every entry must be a move feedback_api allows for the
# user (the human-recipient new->done close is the one rule outside the table).
ACTIONS = [
    ("verified", "done", "Looks fixed: mark Done", "primary"),
    ("verified", "queued", "Not fixed: re-queue", ""),
    ("verified", "skipped", "Skip", ""),
    ("new", "queued", "Queue", "primary"),
    ("new", "done", "Mark done", "primary"),
    ("new", "skipped", "Skip", ""),
    ("queued", "new", "Unqueue", ""),
    ("queued", "skipped", "Skip", ""),
    ("doing", "skipped", "Skip (cancels at merge)", ""),
    ("blocked", "queued", "Re-queue", "primary"),
    ("blocked", "skipped", "Skip", ""),
    ("done", "queued", "Reopen", ""),
    ("skipped", "new", "Restore", ""),
]


def progress_segments(counts: dict) -> list[dict]:
    return [{"status": s, "n": counts[s]} for s in SEG_ORDER if counts.get(s)]


def summary(counts: dict) -> str:
    total = sum(counts.get(s, 0) for s in SEG_ORDER)
    parts = [f"{counts.get('done', 0)}/{total} done"]
    parts += [f"{counts[s]} {s}" for s in SEG_ORDER[1:] if counts.get(s)]
    return " · ".join(parts)


def number_rounds(rounds: list[dict]) -> list[dict]:
    """Input order is build_overview's: direct first, then exports newest first."""
    exports = [r["id"] for r in rounds if r["kind"] != "direct"]
    return [{**r, "label": "Your issues" if r["kind"] == "direct"
             else f"Round {len(exports) - exports.index(r['id'])}"} for r in rounds]


def lanes(rows: Iterable) -> list[dict]:
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(fs.normalize_path(r["page_url"]), []).append(r)
    return [{"page": p, "comments": cs} for p, cs in grouped.items()]


def actions_for(status: str, recipient: str) -> list[dict]:
    out = []
    for frm, to, label, kind in ACTIONS:
        if frm != status:
            continue
        if recipient == "human" and to == "queued":
            continue
        if recipient != "human" and (frm, to) == ("new", "done"):
            continue
        out.append({"from": frm, "to": to, "label": label, "kind": kind})
    return out


def lane_cap_note(now_et: datetime) -> str:
    """Mirrors /queue run's cap rule: 1 lane inside the fleet's Claude-job windows."""
    hm = now_et.hour * 60 + now_et.minute
    if 8 * 60 <= hm < 8 * 60 + 45 or 10 * 60 <= hm < 10 * 60 + 45:
        return "1 lane now — fleet job window"
    return "3 lanes now · 1 during 08:00–08:45 and 10:00–10:45 ET"
