"""build_keep_filtergraph / complement -- pure string and arithmetic, so
these need no ffmpeg, no video and no storage.

Worth testing at this level rather than only end to end: a filtergraph that
is subtly wrong still produces *a* video, and the end-to-end test is slow
and needs real encoding. These catch the shape directly.
"""

import pytest

from backend.workers.match_edit_workers import build_keep_filtergraph, complement


def test_single_segment_emits_no_concat() -> None:
    """concat needs two real inputs; one surviving segment is just a trim."""
    graph, outputs = build_keep_filtergraph([(1.0, 4.0)], has_audio=False, preview=False)

    assert "concat" not in graph
    assert "split" not in graph
    assert "trim=start=1.0:end=4.0" in graph
    assert outputs == ["-map", "[outv]"]


def test_multiple_segments_split_then_concat() -> None:
    keep = [(0.0, 1.0), (2.0, 3.0), (4.0, 5.0)]

    graph, _ = build_keep_filtergraph(keep, has_audio=False, preview=False)

    assert "[0:v]split=3[sv0][sv1][sv2]" in graph
    assert "concat=n=3:v=1:a=0[outv]" in graph


def test_six_segments_scale_to_k() -> None:
    """K is unbounded in this feature -- remove_segment's no-split shortcut
    was only ever verified at N=2, so this emits split at every K."""
    keep = [(float(i), float(i) + 0.5) for i in range(6)]

    graph, _ = build_keep_filtergraph(keep, has_audio=False, preview=False)

    assert "[0:v]split=6[sv0][sv1][sv2][sv3][sv4][sv5]" in graph
    assert "concat=n=6:v=1:a=0[outv]" in graph


def test_audio_adds_asplit_and_an_interleaved_concat() -> None:
    keep = [(0.0, 1.0), (2.0, 3.0)]

    graph, outputs = build_keep_filtergraph(keep, has_audio=True, preview=False)

    assert "[0:a]asplit=2[sa0][sa1]" in graph
    # Interleaved so one concat handles both streams and they cannot
    # disagree about segment count.
    assert "[v0][a0][v1][a1]concat=n=2:v=1:a=1[outv][outa]" in graph
    assert outputs == ["-map", "[outv]", "-map", "[outa]"]


def test_no_audio_emits_no_audio_chains() -> None:
    graph, outputs = build_keep_filtergraph([(0.0, 1.0), (2.0, 3.0)], has_audio=False, preview=False)

    assert "atrim" not in graph
    assert "asplit" not in graph
    assert "[outa]" not in graph
    assert outputs == ["-map", "[outv]"]


def test_preview_routes_through_a_scale_hop() -> None:
    """This worker drives run_ffmpeg directly, so it cannot inherit
    process_video's preview handling and has to apply it itself."""
    graph, _ = build_keep_filtergraph([(0.0, 1.0), (2.0, 3.0)], has_audio=False, preview=True)

    assert "[vraw]" in graph
    assert graph.rstrip().endswith("[outv]")


def test_zero_segments_is_a_programming_error() -> None:
    """The workers raise a user-facing InvalidMediaParamsError before ever
    reaching here; an empty list at this level means a caller bug."""
    with pytest.raises(ValueError):
        build_keep_filtergraph([], has_audio=False, preview=False)


# --- complement ----------------------------------------------------------


def test_complement_of_a_middle_range() -> None:
    assert complement([(4.0, 5.0)], duration=12.0) == [(0.0, 4.0), (5.0, 12.0)]


def test_complement_of_three_bands_is_four_segments() -> None:
    """The acceptance-test shape: red at 0-1, 4-5, 8-9 in a 12s clip leaves
    four keep segments, which is what exercises K=4."""
    gaps = complement([(0.0, 1.0), (4.0, 5.0), (8.0, 9.0)], duration=12.0)

    assert gaps == [(1.0, 4.0), (5.0, 8.0), (9.0, 12.0)]


def test_complement_when_a_range_touches_the_start() -> None:
    assert complement([(0.0, 2.0)], duration=10.0) == [(2.0, 10.0)]


def test_complement_when_a_range_touches_the_end() -> None:
    assert complement([(8.0, 10.0)], duration=10.0) == [(0.0, 8.0)]


def test_complement_of_full_coverage_is_empty() -> None:
    assert complement([(0.0, 10.0)], duration=10.0) == []
