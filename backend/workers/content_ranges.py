"""Turning per-frame verdicts into time ranges.

Pure: no ffmpeg, no storage, no network, no settings. That is deliberate --
this is the only genuinely new algorithm in content search, and everything
else in the feature is a variation on something the repo already does, so
this is the part that earns exhaustive table-driven tests rather than
end-to-end ones.

**What a "hit" actually means.** A frame sampled at t=8.0 is evidence about
its neighbourhood, not about the instant 8.0 -- nothing was looked at
between 6.0 and 10.0, so a subject visible at 8.0 was plausibly visible
either side of it. Every hit therefore widens to half an interval in each
direction before anything else happens. Treating hits as instants would
produce ranges a fraction of a second long that then get dropped as
sub-minimum, and the search would find nothing.
"""

__all__ = ["coalesce"]


def coalesce(
    hit_times: list[float],
    *,
    interval: float,
    duration: float,
    bridge_gap: float,
    min_range: float,
    pad: float,
) -> list[tuple[float, float]]:
    """Sorted, non-overlapping ranges covering the sampled hits.

    Step order is load-bearing and is the thing implementations get wrong:

    1. **Sort.** Batches complete out of order under the request semaphore,
       so arrival order says nothing about time order.
    2. **Widen** each hit by half an interval each way, clamped to the
       video -- see the module docstring.
    3. **Merge** ranges separated by no more than `bridge_gap`. The
       arithmetic to keep in mind: after widening, consecutive hits abut
       (gap 0), one missed sample leaves a gap of exactly `interval`, and
       two misses leave `2 * interval`. Callers pass `interval` to bridge a
       brief occlusion and nothing more -- passing `2 * interval` also
       merges two consecutive misses, which silently joins genuinely
       separate appearances.
    4. **Drop** what is left shorter than `min_range`.
    5. **Pad** outward, clamp, and **merge again**. Padding creates new
       overlaps; a merge pass that ran only before it would leave
       overlapping ranges, and the consumers compute a set complement from
       these -- overlapping input there produces negative-length keep
       segments and a filtergraph that fails at runtime.

    Padding is outward for both polarities: remove_matches then cuts
    slightly more than it saw (never clipping a frame of the subject into
    the output) and keep_matches keeps slightly more (never clipping the
    subject's entry or exit out of the supercut). One direction serves both.
    """
    if not hit_times:
        return []

    half = interval / 2.0
    widened = [
        (max(0.0, t - half), min(duration, t + half))
        for t in sorted(hit_times)
        if t is not None
    ]
    merged = _merge(widened, gap=bridge_gap)
    kept = [(s, e) for s, e in merged if e - s >= min_range]
    if not kept:
        return []

    padded = [(max(0.0, s - pad), min(duration, e + pad)) for s, e in kept]
    # Re-merged because padding can push neighbours into each other.
    return _merge(padded, gap=0.0)


def _merge(ranges: list[tuple[float, float]], *, gap: float) -> list[tuple[float, float]]:
    """Combine ranges separated by `gap` or less. Input must be sorted by
    start. `gap=0.0` merges only genuine overlaps and exact abutments."""
    if not ranges:
        return []
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        last_start, last_end = merged[-1]
        if start - last_end <= gap:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged
