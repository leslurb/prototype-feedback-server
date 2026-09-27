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
    if not any(n(s) for s in STATUSES):
        return "Human only"
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


def normalize_path(u: Optional[str]) -> str:
    """Page identity for lanes and repeat detection: path only, no query/hash/trailing slash."""
    if not u:
        return "/"
    path = urlsplit(u).path if "://" in u else u.split("?", 1)[0].split("#", 1)[0]
    return "/" + path.strip("/")


def resolve_repo(project: Optional[dict], page_url: Optional[str], batch_hints: Optional[dict]) -> Optional[dict]:
    """Project mapping first (owner-set, works for every reviewer); the reviewer's
    own repo_hints only as a fallback. A full URL outside the project's prefixes
    is not trusted. A bare path (direct issue) always belongs to its project. With no
    prefixes configured, the project's repo is trusted for any URL: the comment already
    belongs to this project."""
    if project and project.get("repo"):
        prefixes = [p for p in (project.get("url_prefixes") or []) if p]
        if project.get("target_url"):
            prefixes.append(project["target_url"])
        is_url = bool(page_url) and "://" in page_url
        if not is_url or not prefixes or any(page_url.startswith(p) for p in prefixes):
            return {"repo": project["repo"], "local_path": project.get("local_path"), "source": "project"}
    best = None
    for prefix, info in (batch_hints or {}).items():
        if page_url and page_url.startswith(prefix) and isinstance(info, dict) and info.get("repo"):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, info)
    if best:
        return {"repo": best[1]["repo"], "local_path": best[1].get("local_path"), "source": "reviewer_hint"}
    return None


def iou(a: dict, b: dict) -> float:
    try:
        ax, ay, aw, ah = a["x"], a["y"], a["w"], a["h"]
        bx, by, bw, bh = b["x"], b["y"], b["w"], b["h"]
    except (KeyError, TypeError):
        return 0.0
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def classify_repeat(new: dict, priors: list[dict]) -> Optional[tuple[str, str]]:
    """new/priors: {page, selector, bbox} (+ id, shipped on priors, which are the
    project's earlier DONE comments). Same page AND (same selector, or bbox
    overlap > 0.5 when either side has no selector). Shipped prior =
    regression; unshipped prior = the fix didn't take."""
    for p in priors:
        if p["page"] != new["page"]:
            continue
        if new.get("selector") and p.get("selector"):
            same = new["selector"] == p["selector"]
        else:
            same = iou(new.get("bbox") or {}, p.get("bbox") or {}) > 0.5
        if same:
            return p["id"], ("regression" if p.get("shipped") else "didnt_take")
    return None
