"""Latency measurement: packets, clock sync, chirp analysis and a full run against a fake ESP32."""

import asyncio
import random
import struct

import numpy as np
import pytest

from btsource import calibration as cal
from btsource.config import Options
from btsource.devices import DeviceStore

ESP_OFFSET_US = 5_000_000_000  # the ESP32's clock is far ahead of ours
KEY = "geheim1234"


def test_packets_roundtrip():
    pong = cal.parse_packet(cal.pack_pong(7, 123, 456))
    assert isinstance(pong, cal.Pong) and (pong.seq, pong.t_addon_us, pong.t_esp_us) == (7, 123, 456)
    samples = np.arange(-5, 5, dtype=np.int16)
    audio = cal.parse_packet(cal.pack_audio(3, -42, 16000, samples))
    assert isinstance(audio, cal.AudioChunk) and audio.t_esp_us == -42 and audio.rate == 16000
    assert np.array_equal(audio.samples, samples)
    assert cal.parse_packet(b"junk") is None
    ping = cal.pack_ping(KEY, 1, 99)
    assert ping[:5] == b"SLM1\x01" and ping[5] == len(KEY) and ping[6 : 6 + len(KEY)] == KEY.encode()


def test_offset_prefers_fast_round_trips():
    pongs = []
    for i in range(30):
        sent = 1_000_000 + i * 30_000
        slow = 40_000 if i % 3 else 0  # every third exchange is fast
        recv = sent + 2_000 + slow
        esp = sent + 1_000 + ESP_OFFSET_US  # true midpoint for fast ones
        pongs.append((cal.Pong(i, sent, esp), recv))
    sync = cal.estimate_offset(pongs)
    assert abs(sync.offset_us - ESP_OFFSET_US) < 500
    assert sync.rtt_us == 2_000


def _recording(latency_s: float, sched: cal.Schedule, rate: int = 16000, noise: float = 0.02):
    """What a microphone next to the speaker hears, as AUDIO chunks with ESP timestamps."""
    rng = np.random.default_rng(1)
    total = sched.lead_s + sched.chirp_times_s[-1] + sched.chirp_s + sched.max_latency_s
    emitted = cal.render(rate, sched, total) * 0.3
    heard = np.zeros_like(emitted)
    d = int(latency_s * rate)
    heard[d:] = emitted[: len(emitted) - d]
    heard[d + 160 :] += 0.4 * emitted[: len(emitted) - d - 160]  # room echo 10 ms later
    heard += rng.normal(0, noise, len(heard))
    t0_us = 10_000_000
    chunks = []
    for i in range(0, len(heard), 512):
        block = np.clip(heard[i : i + 512] * 32767, -32768, 32767).astype(np.int16)
        chunks.append(cal.AudioChunk(i // 512, t0_us + ESP_OFFSET_US + i * 1_000_000 // rate, rate, block))
    return chunks, t0_us


@pytest.mark.parametrize("latency", [0.25, 1.234, 3.3])
def test_analyze_finds_latency(latency):
    sched = cal.Schedule()
    chunks, t0 = _recording(latency, sched)
    sync = cal.ClockSync(ESP_OFFSET_US, 3000, 40)
    res = cal.analyze(chunks, sync, t0, sched)
    assert abs(res.latency_ms - latency * 1000) < 2
    assert res.chirps_found == len(sched.chirp_times_s)


def test_analyze_rejects_silence():
    sched = cal.Schedule()
    chunks, t0 = _recording(1.0, sched)
    for ch in chunks:
        ch.samples[:] = np.random.default_rng(2).normal(0, 300, len(ch.samples)).astype(np.int16)
    with pytest.raises(cal.MeasurementError):
        cal.analyze(chunks, cal.ClockSync(ESP_OFFSET_US, 3000, 40), t0, sched)


# -- full run over UDP -----------------------------------------------------------------


class FakeLink:
    streaming = True


class FakePipeline:
    def __init__(self):
        self.calibrating = False
        self.chunks: list[tuple[bytes, int]] = []

    def inject(self, pcm, arrival_ns):
        self.chunks.append((pcm, arrival_ns))

    def speaker_sample(self, addon_us: int, latency_us: int) -> float:
        """Left channel of the injected stream, as played `latency` later (48 kHz)."""
        if not self.chunks:
            return 0.0
        t0 = self.chunks[0][1] // 1000
        idx = int((addon_us - latency_us - t0) * 48 / 1000)
        if idx < 0:
            return 0.0
        chunk_frames = len(self.chunks[0][0]) // 4
        c, k = divmod(idx, chunk_frames)
        if c >= len(self.chunks):
            return 0.0
        pcm = self.chunks[c][0]
        if 4 * k + 2 > len(pcm):
            return 0.0
        return struct.unpack_from("<h", pcm, 4 * k)[0] / 32768.0


class FakeEsp(asyncio.DatagramProtocol):
    """Answers pings with its own clock and streams a 'microphone' that hears the speaker."""

    def __init__(self, pipeline: FakePipeline, latency_us: int):
        self.pipeline = pipeline
        self.latency_us = latency_us
        self.transport = None
        self.stream_task = None
        self.peer = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        t_esp = cal.now_us() + ESP_OFFSET_US
        kind, body = data[4], data[5:]
        klen = body[0]
        if body[1 : 1 + klen].decode() != KEY:
            return
        rest = body[1 + klen :]
        if kind == cal.PING:
            seq, t_addon = struct.unpack("<IQ", rest)
            self.transport.sendto(cal.pack_pong(seq, t_addon, t_esp), addr)
        elif kind == cal.START:
            self.peer = addr
            self.stream_task = asyncio.get_running_loop().create_task(self._stream())
        elif kind == cal.STOP and self.stream_task:
            self.stream_task.cancel()

    async def _stream(self):
        rate, block = 16000, 256
        rng = random.Random(3)
        seq = 0
        start = cal.now_us()
        while True:
            t_first = start + seq * block * 1_000_000 // rate
            wait = (t_first + block * 1_000_000 // rate - cal.now_us()) / 1e6
            if wait > 0:
                await asyncio.sleep(wait)
            samples = np.array([
                self.pipeline.speaker_sample(t_first + i * 1_000_000 // rate, self.latency_us) * 0.5
                + rng.gauss(0, 0.01)
                for i in range(block)
            ])
            pkt = cal.pack_audio(seq, t_first + ESP_OFFSET_US, rate,
                                 np.clip(samples * 32767, -32768, 32767).astype(np.int16))
            self.transport.sendto(pkt, self.peer)
            seq += 1


@pytest.mark.asyncio
async def test_full_measurement_against_fake_esp(tmp_path):
    loop = asyncio.get_running_loop()
    pipeline = FakePipeline()
    esp_transport, esp = await loop.create_datagram_endpoint(
        lambda: FakeEsp(pipeline, latency_us=640_000), local_addr=("127.0.0.1", 0))
    port = esp_transport.get_extra_info("sockname")[1]
    try:
        opts = Options(calibration_mic=f"127.0.0.1:{port}", calibration_key=KEY, delay_report_ms=1000)
        devices = DeviceStore(None, 1000)
        sched = cal.Schedule(lead_s=0.3, chirp_times_s=(0.0, 0.45, 1.0, 1.35), max_latency_s=1.2)
        calibrator = cal.Calibrator(opts, pipeline, FakeLink(), devices,
                                    cal.CalibrationStore.load(tmp_path / "c.json"), schedule=sched)
        sync = await calibrator.test_connection()
        assert abs(sync.offset_us - ESP_OFFSET_US) < 5_000

        calibrator.start_measurement("JBL")
        for _ in range(200):
            await asyncio.sleep(0.1)
            if not calibrator.busy:
                break
        assert calibrator.error is None, calibrator.error
        assert abs(calibrator.last.latency_ms - 640) < 15
        assert not pipeline.calibrating

        effective = calibrator.set_default(round(calibrator.last.latency_ms))
        assert devices.default_delay_ms == effective
        again = cal.CalibrationStore.load(tmp_path / "c.json")
        assert again.measurements[0].label == "JBL" and again.default_delay_ms == effective
        assert calibrator.set_default(None) == 1000 and devices.default_delay_ms == 1000
    finally:
        esp_transport.close()


@pytest.mark.asyncio
async def test_measurement_reports_missing_esp(tmp_path):
    opts = Options(calibration_mic="127.0.0.1:9", calibration_key=KEY)
    calibrator = cal.Calibrator(opts, FakePipeline(), FakeLink(), DeviceStore(None, 1000),
                                cal.CalibrationStore.load(None))
    calibrator.start_measurement("x")
    for _ in range(50):
        await asyncio.sleep(0.1)
        if not calibrator.busy:
            break
    assert calibrator.error and "antwortet nicht" in calibrator.error
