"""content_ranges.coalesce -- pure, so these run with no ffmpeg, no
storage, no network and no database.

This is the only genuinely new algorithm in content search, and the way it
fails is quiet: a wrong step order still returns plausible-looking ranges
that cut the wrong footage. So it is tested exhaustively here rather than
being inferred from the end-to-end video test.
"""

from backend.workers.content_ranges import coalesce

# The production defaults, so these tests describe real behaviour rather
# than a configuration nothing runs with.
DEFAULTS = {"interval": 2.0, "duration": 60.0, "bridge_gap": 4.0, "min_range": 0.5, "pad": 0.25}


def _coalesce(hits, **overrides):
    return coalesce(hits, **{**DEFAULTS, **overrides})


def test_no_hits_is_empty_not_an_error() -> None:
    assert _coalesce([]) == []


def test_a_single_hit_widens_to_its_neighbourhood() -> None:
    """A frame sampled at 10s is evidence about 9-11s, not about the
    instant -- nothing was looked at either side of it."""
    assert _coalesce([10.0]) == [(8.75, 11.25)]  # +/- half interval, then padded


def test_consecutive_hits_become_one_range() -> None:
    ranges = _coalesce([10.0, 12.0, 14.0])

    assert len(ranges) == 1
    start, end = ranges[0]
    assert start == 8.75 and end == 15.25


def test_a_one_sample_gap_is_bridged() -> None:
    """One missed frame -- the subject turned away, or was briefly occluded
    -- must not split one appearance into two ranges."""
    ranges = _coalesce([10.0, 12.0, 16.0, 18.0])  # nothing at 14

    assert len(ranges) == 1


def test_a_long_gap_is_not_bridged() -> None:
    """A genuine absence must stay two ranges, or remove_matches would cut
    footage the subject was never in."""
    ranges = _coalesce([10.0, 12.0, 40.0, 42.0])

    assert len(ranges) == 2


def test_hits_are_clamped_to_the_video() -> None:
    """Never negative, never past the end -- these feed ffmpeg trim ranges
    and a negative start is a runtime failure."""
    ranges = _coalesce([0.0, 60.0], duration=60.0, bridge_gap=0.0)

    assert ranges[0][0] == 0.0
    assert ranges[-1][1] == 60.0


def test_padding_that_creates_an_overlap_is_re_merged() -> None:
    """The step-order regression. Padding pushes neighbours into each other;
    a merge pass that ran only *before* padding would leave overlapping
    ranges, and the consumers compute a set complement from these --
    overlapping input yields negative-length keep segments and a
    filtergraph that fails at runtime.

    Hits at 10.0 and 13.5 widen to (9.0, 11.0) and (12.5, 14.5) -- a 1.5s
    gap, wider than the 1.0 bridge_gap, so they survive step 3 as two
    ranges. Then 0.8s of padding on each side closes 1.6s of it and they
    overlap.
    """
    ranges = _coalesce([10.0, 13.5], bridge_gap=1.0, pad=0.8)

    assert len(ranges) == 1, f"expected the pad to close the gap, got {ranges}"
    _assert_sorted_and_disjoint(ranges)


def test_output_is_always_sorted_and_disjoint() -> None:
    ranges = _coalesce([5.0, 7.0, 30.0, 32.0, 50.0])

    _assert_sorted_and_disjoint(ranges)


def test_shuffled_input_matches_sorted_input() -> None:
    """Batches complete out of order under the request semaphore, so
    arrival order says nothing about time order."""
    ordered = _coalesce([10.0, 12.0, 30.0, 32.0])
    shuffled = _coalesce([32.0, 10.0, 30.0, 12.0])

    assert ordered == shuffled


def test_ranges_shorter_than_the_minimum_are_dropped() -> None:
    """With a sampling interval far below min_range, a lone hit is noise
    rather than an appearance."""
    assert _coalesce([10.0], interval=0.2, min_range=1.0, pad=0.0) == []


def test_a_lone_hit_survives_at_the_default_interval() -> None:
    """The deliberate bias: at a 2s interval a single hit widens to 2s,
    comfortably past min_range, so a brief appearance is kept rather than
    filtered out. Biased toward keeping too much."""
    assert _coalesce([10.0]) != []


def _assert_sorted_and_disjoint(ranges) -> None:
    for (a_start, a_end), (b_start, b_end) in zip(ranges, ranges[1:]):
        assert a_start < a_end, f"zero or negative length range {(a_start, a_end)}"
        assert a_end < b_start, f"overlapping or unsorted: {(a_start, a_end)} then {(b_start, b_end)}"


def test_two_missed_samples_are_not_bridged_at_the_production_gap() -> None:
    """The arithmetic worth stating: after widening, hits k samples apart
    are separated by (k-1) * interval. So one miss leaves `interval` and two
    misses leave `2 * interval`.

    Production passes bridge_gap=interval, which bridges one miss and stops
    there. The earlier 2*interval default also merged two-miss gaps, which
    joined genuinely separate appearances -- remove_matches then cut footage
    the subject was never in, the opposite of the intended bias.

    Hits at 0 and 6 sampled every 2s skip 2 and 4: a 4.0s gap.
    """
    ranges = _coalesce([0.0, 6.0], interval=2.0, bridge_gap=2.0, duration=12.0)

    assert len(ranges) == 2, f"a two-sample gap must not bridge, got {ranges}"


def test_one_missed_sample_is_still_bridged_at_the_production_gap() -> None:
    """The other side of the same boundary. Hits at 0 and 4 skip only the
    sample at 2: a 2.0s gap, exactly bridge_gap, so a brief occlusion still
    closes."""
    ranges = _coalesce([0.0, 4.0], interval=2.0, bridge_gap=2.0, duration=12.0)

    assert len(ranges) == 1, f"a one-sample gap should bridge, got {ranges}"


def test_a_chain_of_one_sample_gaps_merges_transitively() -> None:
    """The case the boundary tests above miss, because they only test
    *pairs*. Bridging is transitive: three separate appearances, each one
    missed sample from the next, collapse into one continuous range.

    This is not a bug in coalesce -- at a 2s interval those three hits are
    genuinely indistinguishable from one appearance with two brief
    occlusions, and no bridge_gap can tell them apart. It is recorded here
    because it is what made a 12s clip with red at 0-1, 4-5 and 8-9 come
    back as the single range (0.0, 9.25): remove_matches then cut 6 seconds
    the subject was never in.

    The fix was to sample finer on short clips (_sample_interval's
    vision_min_frames ceiling), not to lower bridge_gap -- bridging still
    has to hold for genuine occlusions, and coalescing happens before the
    polarity is known, so it cannot be tuned per-consumer.
    """
    ranges = _coalesce([0.0, 4.0, 8.0], interval=2.0, bridge_gap=2.0, duration=12.0)

    assert ranges == [(0.0, 9.25)], f"expected one transitively merged range, got {ranges}"


def test_finer_sampling_resolves_what_the_chain_collapsed() -> None:
    """The other half of that story, at the interval the worker now picks
    for a 12-second clip: the same three appearances stay three ranges."""
    hits = [0.0, 0.5, 4.0, 4.5, 8.0, 8.5]

    ranges = _coalesce(hits, interval=0.5, bridge_gap=0.5, duration=12.0)

    assert len(ranges) == 3, f"expected the three bands to stay separate, got {ranges}"
