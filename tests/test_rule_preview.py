"""Tests for the read-only rule-impact preview diff.

Two sets of confirmed faults (original rule vs unsaved draft) are compared
as half-open interval covers: added / removed pieces, shared pieces whose
confirmation instant moved, and unchanged pieces.
"""

from app.engine import FaultInterval, diff_fault_sets


def f(start, end, duration=None, confirmed=None):
    duration = 0.0 if duration is None else duration
    confirmed = start + duration if confirmed is None else confirmed
    return FaultInterval(
        start=float(start), end=float(end), rule_id=1,
        duration_required=float(duration), confirmed_at=float(confirmed),
        evidence=[],
    )


def kinds(segments):
    return [(s.kind, s.start, s.end) for s in segments]


def all_segments(diff):
    return diff.added + diff.removed + diff.confirmation_changed + diff.unchanged


def test_empty_to_empty_has_no_changes():
    diff = diff_fault_sets([], [])
    assert not diff.has_changes
    assert diff.counts() == {"added": 0, "removed": 0, "confirmation_changed": 0, "unchanged": 0}


def test_identical_faults_are_one_unchanged_segment():
    a = [f(0, 10, duration=2)]
    diff = diff_fault_sets(a, [f(0, 10, duration=2)])
    assert not diff.has_changes
    assert kinds(diff.unchanged) == [("unchanged", 0.0, 10.0)]


def test_added_and_removed_segments_split_at_boundaries():
    # original: [0,6) ; draft: [4,10); the overlap [4,6) keeps a shifted
    # start, so its confirmation instant moves too (duration 0 -> 4 vs 6).
    diff = diff_fault_sets([f(0, 6)], [f(4, 10)])
    assert kinds(diff.removed) == [("removed", 0.0, 4.0)]
    assert kinds(diff.added) == [("added", 6.0, 10.0)]
    assert kinds(diff.confirmation_changed) == [("confirmation_changed", 4.0, 6.0)]
    assert diff.has_changes


def test_added_removed_unchanged_split_when_confirmation_stable():
    # Same end, draft starts later but requires 0 duration so confirmation
    # instants agree (both 0): [0,4) removed, [4,6) genuinely unchanged.
    diff = diff_fault_sets([f(0, 6, duration=0, confirmed=0)],
                           [f(4, 6, duration=0, confirmed=0)])
    assert kinds(diff.removed) == [("removed", 0.0, 4.0)]
    assert kinds(diff.unchanged) == [("unchanged", 4.0, 6.0)]
    assert diff.added == [] and diff.confirmation_changed == []


def test_touching_boundaries_emit_no_zero_segment():
    # [0,6) vs [6,10) touch: half-open cover never overlaps, nothing shared
    diff = diff_fault_sets([f(0, 6)], [f(6, 10)])
    assert kinds(diff.removed) == [("removed", 0.0, 6.0)]
    assert kinds(diff.added) == [("added", 6.0, 10.0)]
    assert diff.unchanged == []
    assert diff.confirmation_changed == []


def test_same_interval_with_shifted_confirmation_is_flagged():
    # The interval [0,10) never moves; only the required duration (and hence
    # the confirmation instant) changes: it must still be reported.
    diff = diff_fault_sets([f(0, 10, duration=2)], [f(0, 10, duration=5)])
    assert diff.added == [] and diff.removed == []
    assert kinds(diff.confirmation_changed) == [("confirmation_changed", 0.0, 10.0)]
    seg = diff.confirmation_changed[0]
    assert seg.confirmed_at_before == 2.0
    assert seg.confirmed_at_after == 5.0
    assert seg.duration_before == 2.0
    assert seg.duration_after == 5.0


def test_confirmation_shift_only_inside_overlap_pieces():
    # before [0,10) confirm 2 ; after [5,15) confirm 5+3=8
    diff = diff_fault_sets([f(0, 10, duration=2)], [f(5, 15, duration=3)])
    assert kinds(diff.removed) == [("removed", 0.0, 5.0)]
    assert kinds(diff.added) == [("added", 10.0, 15.0)]
    assert kinds(diff.confirmation_changed) == [("confirmation_changed", 5.0, 10.0)]
    seg = diff.confirmation_changed[0]
    assert seg.confirmed_at_before == 2.0 and seg.confirmed_at_after == 8.0


def test_partial_overlap_same_confirmation_stays_unchanged():
    # Shared prefix with the same start (hence the same confirmation instant),
    # draft extends later -> [0,4) unchanged, [4,10) added.
    diff = diff_fault_sets([f(0, 4, duration=2)], [f(0, 10, duration=2)])
    assert kinds(diff.unchanged) == [("unchanged", 0.0, 4.0)]
    assert kinds(diff.added) == [("added", 4.0, 10.0)]
    assert diff.removed == [] and diff.confirmation_changed == []


def test_partial_overlap_shifts_confirmation_on_shared_piece():
    # confirmed_at = start + duration, so a shifted start moves confirmation
    # even when the required duration itself is unchanged.
    diff = diff_fault_sets([f(0, 6, duration=2)], [f(4, 10, duration=2)])
    assert kinds(diff.removed) == [("removed", 0.0, 4.0)]
    assert kinds(diff.added) == [("added", 6.0, 10.0)]
    assert kinds(diff.confirmation_changed) == [("confirmation_changed", 4.0, 6.0)]
    seg = diff.confirmation_changed[0]
    assert seg.confirmed_at_before == 2.0
    assert seg.confirmed_at_after == 6.0


def test_new_draft_fault_is_fully_added():
    diff = diff_fault_sets([], [f(0, 10, duration=1)])
    assert kinds(diff.added) == [("added", 0.0, 10.0)]
    assert diff.added[0].confirmed_at_after == 1.0
    assert diff.added[0].confirmed_at_before is None
    assert diff.has_changes


def test_segments_partition_the_union_without_gaps_or_overlaps():
    diff = diff_fault_sets(
        [f(0, 6, duration=1), f(8, 14, duration=1)],
        [f(4, 10, duration=2), f(12, 18, duration=2)],
    )
    segs = sorted(all_segments(diff), key=lambda s: s.start)
    # consecutive pieces stitch back together over [0,18)
    assert segs[0].start == 0.0 and segs[-1].end == 18.0
    for a, b in zip(segs, segs[1:]):
        assert a.end == b.start


def test_to_dict_shape():
    diff = diff_fault_sets([f(0, 6)], [f(4, 10)])
    payload = diff.to_dict()
    assert payload["has_changes"] is True
    assert set(payload) == {"has_changes", "counts", "added", "removed",
                            "confirmation_changed", "unchanged"}
    assert payload["removed"][0]["kind"] == "removed"
    assert payload["added"][0]["confirmed_at_before"] is None
