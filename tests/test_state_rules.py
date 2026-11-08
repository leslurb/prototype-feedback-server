import pytest

from feedback_state import DRAIN, USER, can_ship, can_transition, round_state


@pytest.mark.parametrize("frm,to,actor,ok", [
    ("new", "queued", USER, True),
    ("queued", "doing", DRAIN, True),
    ("queued", "doing", USER, False),       # only the drain claims work
    ("doing", "verified", DRAIN, True),
    ("doing", "done", DRAIN, False),        # B2: the drain can never mark Done
    ("verified", "done", DRAIN, False),
    ("verified", "done", USER, True),
    ("doing", "skipped", USER, True),       # skip mid-run; drain drops the commit at merge
    ("doing", "queued", DRAIN, True),       # lane conflict re-queue
    ("done", "queued", USER, True),         # reopen
    ("new", "done", USER, False),           # no skipping the pipeline
    ("blocked", "doing", DRAIN, False),     # a blocked item needs the user first
])
def test_transitions(frm, to, actor, ok):
    assert can_transition(frm, to, actor) is ok


@pytest.mark.parametrize("counts,needs_repo,shipped,resent,expected", [
    ({"new": 7}, True, False, False, "Needs repo"),
    ({}, False, False, False, "Human only"),
    ({"doing": 1, "queued": 2, "done": 3}, False, False, False, "Running"),
    ({"queued": 2}, False, False, False, "Queued"),
    ({"blocked": 1, "done": 4}, False, False, False, "Blocked"),
    ({"new": 2, "done": 3}, False, False, False, "Open"),
    ({"verified": 2, "done": 3}, False, False, False, "To confirm"),
    ({"done": 5, "skipped": 1}, False, False, False, "Ready to ship"),
    ({"done": 5}, False, False, True, "Re-review sent"),
    ({"done": 5}, False, True, True, "Shipped"),
])
def test_round_state(counts, needs_repo, shipped, resent, expected):
    assert round_state(counts, needs_repo, shipped, resent) == expected


def test_can_ship_counts_skipped_as_finished_but_not_verified():
    assert can_ship({"done": 3, "skipped": 2}) is True
    assert can_ship({"done": 3, "verified": 1}) is False   # a verified item still needs the user's look
    assert can_ship({}) is False                           # nothing to ship


import feedback_state as fs


def test_response_status_mapping():
    llm = {s: fs.response_status(s, "llm") for s in fs.STATUSES}
    assert llm == {"new": "pending", "queued": "pending", "doing": "in_progress",
                   "verified": "fixed", "done": "fixed", "skipped": "skipped",
                   "blocked": "needs_input"}
    assert fs.response_status("done", "human") == "answered"
    assert fs.response_status("new", "human") == "pending"
    assert fs.response_status("mystery", "llm") == "pending"
