"""Pure rules for the feedback work queue. No I/O, so it is tested directly."""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

STATUSES = ("new", "queued", "doing", "verified", "done", "skipped", "blocked")
USER, DRAIN = "user", "drain"

# (from, to) -> actors allowed to make that move. The drain can never reach
# "done": Done is the user's judgement on the before/after (design B2).
TRANSITIONS: dict[tuple[str, str], set[str]] = {
    ("new", "queued"): {USER},
    ("new", "skipped"): {USER},
    ("queued", "new"): {USER},
    ("queued", "doing"): {DRAIN},
    ("queued", "skipped"): {USER},
    ("doing", "verified"): {DRAIN},
    ("doing", "blocked"): {DRAIN},
    ("doing", "queued"): {DRAIN, USER},
    ("doing", "skipped"): {USER},
    ("verified", "done"): {USER},
    ("verified", "queued"): {USER},
    ("verified", "skipped"): {USER},
    ("verified", "blocked"): {DRAIN},
    ("blocked", "queued"): {USER},
    ("blocked", "skipped"): {USER},
    ("done", "queued"): {USER},
    ("skipped", "new"): {USER},
}


def can_transition(frm: str, to: str, actor: str) -> bool:
    return actor in TRANSITIONS.get((frm, to), set())


def round_state(counts: dict, needs_repo: bool, shipped: bool, rereview_sent: bool) -> str:
    n = lambda k: counts.get(k, 0)  # noqa: E731
    if needs_repo:
        return "Needs repo"
    if n("doing"):
        return "Running"
    if n("queued"):
        return "Queued"
    if n("blocked"):
        return "Blocked"
    if n("new"):
        return "Open"
    if n("verified"):
        return "To confirm"
    if shipped:
        return "Shipped"
    if rereview_sent:
        return "Re-review sent"
    return "Ready to ship"


def can_ship(counts: dict) -> bool:
    finished = counts.get("done", 0) + counts.get("skipped", 0)
    total = sum(counts.get(s, 0) for s in STATUSES)
    return total > 0 and finished == total
