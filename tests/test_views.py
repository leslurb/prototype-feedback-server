from datetime import datetime

import pytest

import feedback_state as fs
import feedback_views as fv


def test_segments_follow_fixed_order_and_skip_zeros():
    assert fv.progress_segments({"new": 2, "done": 3, "queued": 0}) == [
        {"status": "done", "n": 3}, {"status": "new", "n": 2}]


def test_summary_counts_skipped_in_the_denominator():
    assert fv.summary({"done": 3, "skipped": 3}) == "3/6 done · 3 skipped"
    assert fv.summary({}) == "0/0 done"


def test_number_rounds_labels_direct_and_counts_exports_oldest_first():
    rounds = [{"id": "d", "kind": "direct"}, {"id": "r3", "kind": "export"},
              {"id": "r2", "kind": "export"}, {"id": "r1", "kind": "export"}]
    assert [r["label"] for r in fv.number_rounds(rounds)] == ["Your issues", "Round 3", "Round 2", "Round 1"]


def test_lanes_group_by_normalised_page_in_first_seen_order():
    rows = [{"id": 1, "page_url": "https://x.dev/products?a=1"}, {"id": 2, "page_url": "https://x.dev/"},
            {"id": 3, "page_url": "https://x.dev/products/"}]
    assert [(l["page"], [c["id"] for c in l["comments"]]) for l in fv.lanes(rows)] == [
        ("/products", [1, 3]), ("/", [2])]


@pytest.mark.parametrize("status", fs.STATUSES)
@pytest.mark.parametrize("recipient", ["llm", "human"])
def test_every_offered_action_is_one_the_server_allows(status, recipient):
    for a in fv.actions_for(status, recipient):
        assert a["from"] == status
        # The two human-only moves feedback_api.change_status allows outside the table.
        human_only = recipient == "human" and (a["from"], a["to"]) in {("new", "done"), ("done", "new")}
        assert human_only or fs.can_transition(a["from"], a["to"], fs.USER), a
        assert not (recipient == "human" and a["to"] == "queued")


def test_key_actions_present():
    assert ("verified", "done") in {(a["from"], a["to"]) for a in fv.actions_for("verified", "llm")}
    assert ("new", "queued") in {(a["from"], a["to"]) for a in fv.actions_for("new", "llm")}
    assert ("new", "done") in {(a["from"], a["to"]) for a in fv.actions_for("new", "human")}
    assert ("new", "done") not in {(a["from"], a["to"]) for a in fv.actions_for("new", "llm")}
    assert ("doing", "queued") in {(a["from"], a["to"]) for a in fv.actions_for("doing", "llm")}
    assert ("doing", "queued") not in {(a["from"], a["to"]) for a in fv.actions_for("doing", "human")}


def test_done_human_item_can_be_reopened_to_new_but_llm_reopens_to_queued():
    assert {(a["from"], a["to"]) for a in fv.actions_for("done", "human")} == {("done", "new")}
    assert {(a["from"], a["to"]) for a in fv.actions_for("done", "llm")} == {("done", "queued")}


def test_to_et_converts_utc_iso_to_eastern():
    assert fv.to_et("2026-10-01T14:30:00+00:00") == "2026-10-01 10:30 ET"
    assert fv.to_et("2026-10-01T02:05:00+00:00") == "2026-09-30 22:05 ET"


def test_to_et_tolerates_missing():
    assert fv.to_et(None) == "—"
    assert fv.to_et("") == "—"


@pytest.mark.parametrize("hm,in_window", [((7, 59), False), ((8, 0), True), ((8, 44), True), ((8, 45), False),
                                          ((10, 0), True), ((10, 44), True), ((10, 45), False), ((14, 0), False)])
def test_lane_cap_note_tracks_fleet_job_windows(hm, in_window):
    note = fv.lane_cap_note(datetime(2026, 10, 5, hm[0], hm[1]))
    assert note.startswith("1 lane now") is in_window
