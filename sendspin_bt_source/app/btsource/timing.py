"""Smooth capture timestamps for bursty Bluetooth input.

A2DP packets arrive in bursts: the radio and the sender's scheduling add tens of
milliseconds of jitter. Stamping each chunk with its arrival time makes the
receiving Sendspin bridge see "gaps" (it fills anything over ~15 ms with
silence) and then surplus it has to drop, which is audible as stutter.

Instead the timestamp advances by the sample count. The anchor follows the
*earliest* arrivals (arrival can only be later than capture, never earlier) and
slowly follows clock drift between sender and us. Real gaps (pause, lost
packets for a while) re-anchor once they persist.
"""

from __future__ import annotations

WINDOW_US = 2_000_000        # evaluate the arrival envelope over this span
DRIFT_GAIN = 0.5             # fraction of the window's minimum offset applied per window
MAX_DRIFT_STEP_US = 5_000    # cap per window (≈2500 ppm, far above real clock skew)
GAP_US = 300_000             # persistent lateness above this is a real gap → re-anchor
RESUME_US = 1_000_000        # a single chunk this late means the input paused → re-anchor now


class CaptureClock:
    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.reset()

    def reset(self) -> None:
        self._anchor_us: int | None = None
        self._frames = 0
        self._window_start_frames = 0
        self._window_min: int | None = None
        self.reanchors = 0

    def _position_us(self) -> int:
        assert self._anchor_us is not None
        return self._anchor_us + self._frames * 1_000_000 // self.sample_rate

    def timestamp(self, arrival_us: int, frames: int) -> int:
        """Return the capture timestamp for a chunk of ``frames`` that arrived at ``arrival_us``.

        ``arrival_us`` is when the chunk's first sample was available; the chunk is
        assumed to directly follow the previous one.
        """
        if self._anchor_us is None:
            self._anchor_us = arrival_us
            self._frames = 0
            self._window_start_frames = 0
            self._window_min = None
        offset = arrival_us - self._position_us()
        if offset > RESUME_US:
            self._anchor_us += offset
            self._window_min = None
            self.reanchors += 1
            offset = 0
        if offset < 0:
            # Arrived earlier than predicted: our anchor was too late. Move it at once;
            # this only shifts timestamps backwards, which the bridge absorbs as skew.
            self._anchor_us += offset
            offset = 0
        if self._window_min is None or offset < self._window_min:
            self._window_min = offset
        ts = self._position_us()
        self._frames += frames
        window_frames = self._frames - self._window_start_frames
        if window_frames * 1_000_000 // self.sample_rate >= WINDOW_US:
            self._close_window()
        return ts

    def _close_window(self) -> None:
        lateness = self._window_min or 0
        if lateness > GAP_US:
            # Every packet of the window came far too late: input was interrupted.
            self._anchor_us += lateness
            self.reanchors += 1
        elif lateness > 0:
            self._anchor_us += min(MAX_DRIFT_STEP_US, int(lateness * DRIFT_GAIN))
        self._window_start_frames = self._frames
        self._window_min = None
