import random

from btsource.timing import CaptureClock

RATE = 48000
CHUNK = 1024  # frames (~21 ms, an AAC frame)
CHUNK_US = CHUNK * 1_000_000 // RATE
GAP_THRESHOLD_US = 15_000  # aiosendspin SourceBridge inserts silence above this


def _bridge_gaps(stamps: list[tuple[int, int]]) -> int:
    """Count silence insertions, following aiosendspin SourceBridge._track_timestamp."""
    anchor, frames, skew, gaps = stamps[0][0], 0, 0, 0
    for ts, n in stamps:
        expected = anchor + frames * 1_000_000 // RATE
        offset = ts - expected - skew
        if offset > GAP_THRESHOLD_US:
            gaps += 1
            frames += offset * RATE // 1_000_000
        else:
            step = int(n * 1_000_000 // RATE * 0.05)
            skew += max(-step, min(step, offset))
        frames += n
    return gaps


def _simulate(seconds: float, *, jitter_us: int, ppm: float = 0.0, seed: int = 1):
    rnd = random.Random(seed)
    clock = CaptureClock(RATE)
    stamps, arrivals = [], []
    n = int(seconds * 1_000_000 / CHUNK_US)
    for i in range(n):
        true_us = int(i * CHUNK_US * (1 + ppm / 1e6))
        arrival = 5_000_000 + true_us + rnd.randint(0, jitter_us)
        stamps.append((clock.timestamp(arrival, CHUNK), CHUNK))
        arrivals.append(arrival)
    return clock, stamps, arrivals


def test_arrival_stamps_would_stutter():
    _, _, arrivals = _simulate(30, jitter_us=60_000)
    assert _bridge_gaps([(a, CHUNK) for a in arrivals]) > 20


def test_smoothed_stamps_have_no_gaps_under_jitter():
    _, stamps, _ = _simulate(120, jitter_us=60_000)
    assert _bridge_gaps(stamps) == 0


def test_follows_clock_drift():
    for ppm in (-300.0, 300.0):
        _, stamps, arrivals = _simulate(600, jitter_us=40_000, ppm=ppm)
        assert _bridge_gaps(stamps) == 0
        # Timestamps stay close to the real arrival envelope (no runaway).
        assert abs(arrivals[-1] - stamps[-1][0]) < 100_000


def test_reanchors_after_pause():
    clock = CaptureClock(RATE)
    t = 0
    for _ in range(100):
        clock.timestamp(t, CHUNK)
        t += CHUNK_US
    t += 5_000_000  # input paused for 5 s
    ts = clock.timestamp(t, CHUNK)
    assert abs(ts - t) < 1000
    assert clock.reanchors == 1
