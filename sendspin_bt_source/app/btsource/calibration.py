"""Latency measurement with an ESP32 microphone (ESPHome component ``sendspin_latency_mic``).

The add-on plays a sequence of chirps into its own Sendspin source; Music Assistant
plays it on the chosen speaker; the ESP32 next to the speaker streams its microphone
with timestamps over UDP. After a clock sync between add-on and ESP32 the chirps are
located in the recording, giving the time from "audio enters the add-on" to "sound
leaves the speaker" -- exactly the delay to report to Bluetooth senders.

UDP protocol (little endian, header ``b"SLM1"`` + type byte):

* PING  (1)  add-on -> ESP: key, u32 seq, u64 t_addon_us
* PONG  (2)  ESP -> add-on: u32 seq, u64 t_addon_us (echo), u64 t_esp_us (at receipt)
* START (3)  add-on -> ESP: key, u32 duration_ms  (ESP streams to the sender address)
* STOP  (4)  add-on -> ESP: key
* AUDIO (5)  ESP -> add-on: u32 seq, i64 t_esp_first_sample_us, u32 rate, u16 n, n x i16

``key`` is a length-prefixed shared secret so nobody else on the network can switch
the microphone on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import struct
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

_LOGGER = logging.getLogger(__name__)

MAGIC = b"SLM1"
PING, PONG, START, STOP, AUDIO = 1, 2, 3, 4, 5
DEFAULT_MIC_PORT = 8939

OUT_RATE = 48000
OUT_CHANNELS = 2
CHUNK_S = 0.02


def now_us() -> int:
    return time.monotonic_ns() // 1000


# -- packets ---------------------------------------------------------------------------


def _key_field(key: str) -> bytes:
    raw = key.encode("utf-8")[:64]
    return bytes([len(raw)]) + raw


def pack_ping(key: str, seq: int, t_addon_us: int) -> bytes:
    return MAGIC + bytes([PING]) + _key_field(key) + struct.pack("<IQ", seq, t_addon_us)


def pack_start(key: str, duration_ms: int) -> bytes:
    return MAGIC + bytes([START]) + _key_field(key) + struct.pack("<I", duration_ms)


def pack_stop(key: str) -> bytes:
    return MAGIC + bytes([STOP]) + _key_field(key)


def pack_pong(seq: int, t_addon_us: int, t_esp_us: int) -> bytes:
    return MAGIC + bytes([PONG]) + struct.pack("<IQQ", seq, t_addon_us, t_esp_us)


def pack_audio(seq: int, t_esp_first_us: int, rate: int, samples: np.ndarray) -> bytes:
    samples = np.asarray(samples, dtype="<i2")
    return MAGIC + bytes([AUDIO]) + struct.pack("<IqIH", seq, t_esp_first_us, rate, len(samples)) + samples.tobytes()


@dataclass
class Pong:
    seq: int
    t_addon_us: int
    t_esp_us: int


@dataclass
class AudioChunk:
    seq: int
    t_esp_us: int
    rate: int
    samples: np.ndarray


def parse_packet(data: bytes) -> Pong | AudioChunk | None:
    if len(data) < 5 or data[:4] != MAGIC:
        return None
    kind, body = data[4], data[5:]
    try:
        if kind == PONG:
            seq, t_addon, t_esp = struct.unpack_from("<IQQ", body)
            return Pong(seq, t_addon, t_esp)
        if kind == AUDIO:
            seq, t_esp, rate, n = struct.unpack_from("<IqIH", body)
            samples = np.frombuffer(body, dtype="<i2", count=n, offset=struct.calcsize("<IqIH"))
            return AudioChunk(seq, t_esp, rate, samples.astype(np.int16))
    except (struct.error, ValueError):
        return None
    return None


# -- clock sync ------------------------------------------------------------------------


@dataclass
class ClockSync:
    """ESP clock minus add-on clock, from the ping with the shortest round trip."""

    offset_us: int
    rtt_us: int
    samples: int

    def to_addon(self, t_esp_us: int) -> int:
        return t_esp_us - self.offset_us


def estimate_offset(pongs: list[tuple[Pong, int]]) -> ClockSync | None:
    """``pongs``: (pong, t_addon_received_us). NTP-style midpoint of the fastest exchanges."""
    if not pongs:
        return None
    ranked = sorted(pongs, key=lambda pr: pr[1] - pr[0].t_addon_us)
    best = ranked[: max(1, len(ranked) // 5)]
    offsets = [p.t_esp_us - (p.t_addon_us + recv) // 2 for p, recv in best]
    rtt = best[0][1] - best[0][0].t_addon_us
    return ClockSync(int(np.median(offsets)), rtt, len(pongs))


# -- test signal -----------------------------------------------------------------------


@dataclass
class Schedule:
    lead_s: float = 3.0
    chirp_times_s: tuple[float, ...] = (0.0, 1.3, 2.9, 4.2, 5.8, 7.1)
    chirp_s: float = 0.08
    f0: float = 800.0
    f1: float = 6000.0
    amplitude: float = 0.5
    max_latency_s: float = 8.0

    @property
    def starts_s(self) -> list[float]:
        return [self.lead_s + t for t in self.chirp_times_s]

    @property
    def emit_s(self) -> float:
        return self.lead_s + self.chirp_times_s[-1] + self.chirp_s + self.max_latency_s


def chirp(rate: int, sched: Schedule) -> np.ndarray:
    n = int(sched.chirp_s * rate)
    t = np.arange(n) / rate
    k = (sched.f1 - sched.f0) / sched.chirp_s
    sig = np.sin(2 * np.pi * (sched.f0 * t + 0.5 * k * t * t))
    return sig * np.hanning(n)


def render(rate: int, sched: Schedule, total_s: float) -> np.ndarray:
    out = np.zeros(int(total_s * rate) + 1)
    c = chirp(rate, sched)
    for start in sched.starts_s:
        i = int(round(start * rate))
        out[i : i + len(c)] += c[: max(0, len(out) - i)]
    return out


def emission_pcm(sched: Schedule) -> bytes:
    """The whole test signal as 48 kHz s16 stereo."""
    mono = render(OUT_RATE, sched, sched.emit_s) * sched.amplitude
    s16 = np.clip(mono * 32767, -32768, 32767).astype("<i2")
    return np.repeat(s16, OUT_CHANNELS).tobytes()


# -- analysis --------------------------------------------------------------------------


@dataclass
class Result:
    latency_ms: float
    spread_ms: float
    chirps_found: int
    chirps_total: int
    coverage: float
    quality: float
    rtt_ms: float


class MeasurementError(RuntimeError):
    pass


def analyze(chunks: list[AudioChunk], sync: ClockSync, t0_us: int, sched: Schedule) -> Result:
    """Find the test chirps in the microphone recording.

    ``t0_us`` is the add-on time of the first emitted sample. Returns the latency from
    emission to the microphone (add-on clock).
    """
    if not chunks:
        raise MeasurementError("Keine Mikrofon-Daten vom ESP32 empfangen")
    rate = chunks[0].rate
    total_s = sched.lead_s + sched.chirp_times_s[-1] + sched.chirp_s + sched.max_latency_s
    n = int(total_s * rate)
    rec = np.zeros(n)
    have = np.zeros(n, dtype=bool)
    for ch in chunks:
        if ch.rate != rate:
            continue
        start = int(round((sync.to_addon(ch.t_esp_us) - t0_us) * rate / 1e6))
        a, b = max(0, start), min(n, start + len(ch.samples))
        if a < b:
            rec[a:b] = ch.samples[a - start : b - start] / 32768.0
            have[a:b] = True
    coverage = float(have.mean())
    if coverage < 0.5:
        raise MeasurementError(f"Zu wenig Mikrofon-Daten ({coverage:.0%}) – WLAN-Verbindung des ESP32 prüfen")
    rec -= rec[have].mean() if have.any() else 0.0

    ref = render(rate, sched, total_s)[:n]
    size = 1 << int(np.ceil(np.log2(2 * n)))
    corr = np.fft.irfft(np.fft.rfft(rec, size) * np.conj(np.fft.rfft(ref, size)), size)
    max_lag = int(sched.max_latency_s * rate)
    lags = corr[: max_lag + 1]
    coarse = int(np.argmax(lags))
    noise = float(np.median(np.abs(lags))) or 1e-12
    quality = float(lags[coarse] / noise)

    # Refine per chirp around the coarse lag.
    c = chirp(rate, sched)
    window = int(0.15 * rate)
    found: list[float] = []
    for start in sched.starts_s:
        centre = int(round(start * rate)) + coarse
        a, b = centre - window, centre + window + len(c)
        if a < 0 or b > n or not have[a:b].all():
            continue
        seg = rec[a:b]
        local = np.correlate(seg, c, mode="valid")
        k = int(np.argmax(local))
        peak = local[k]
        floor = float(np.median(np.abs(local))) or 1e-12
        if peak / floor < 6:
            continue
        # Parabolic interpolation for sub-sample accuracy.
        if 0 < k < len(local) - 1:
            y0, y1, y2 = local[k - 1], local[k], local[k + 1]
            denom = y0 - 2 * y1 + y2
            k = k + (0.5 * (y0 - y2) / denom if denom else 0.0)
        found.append((a + k - int(round(start * rate))) / rate)
    if len(found) < 3:
        raise MeasurementError(
            f"Messton nur {len(found)}× erkannt – ESP32 näher an die Box, lauter stellen oder Umgebung leiser"
        )
    lat = float(np.median(found))
    return Result(
        latency_ms=round(lat * 1000, 1),
        spread_ms=round((max(found) - min(found)) * 1000, 1),
        chirps_found=len(found),
        chirps_total=len(sched.starts_s),
        coverage=round(coverage, 3),
        quality=round(quality, 1),
        rtt_ms=round(sync.rtt_us / 1000, 1),
    )


# -- UDP link to the ESP32 -------------------------------------------------------------


class MicLink(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self.peer_ip: str | None = None
        self.pongs: list[tuple[Pong, int]] = []
        self.chunks: list[AudioChunk] = []
        self.recording = False

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        t = now_us()
        if self.peer_ip is not None and addr[0] != self.peer_ip:
            return
        pkt = parse_packet(data)
        if isinstance(pkt, Pong):
            self.pongs.append((pkt, t))
        elif isinstance(pkt, AudioChunk) and self.recording:
            self.chunks.append(pkt)


async def resolve(host: str) -> str:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_DGRAM)
        return infos[0][4][0]
    except OSError:
        if not host.endswith(".local"):
            raise
    from zeroconf import IPVersion
    from zeroconf.asyncio import AsyncZeroconf

    azc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    try:
        from zeroconf import AddressResolver

        resolver = AddressResolver(host if host.endswith(".") else host + ".")
        if await resolver.async_request(azc.zeroconf, 3000):
            addrs = resolver.ip_addresses_by_version(IPVersion.V4Only)
            if addrs:
                return str(addrs[0])
    finally:
        await azc.async_close()
    raise OSError(f"{host} nicht gefunden")


def parse_target(target: str) -> tuple[str, int]:
    target = target.strip()
    if target.count(":") == 1:
        host, port = target.rsplit(":", 1)
        return host, int(port)
    return target, DEFAULT_MIC_PORT


# -- persistence -----------------------------------------------------------------------


@dataclass
class Measurement:
    label: str
    latency_ms: float
    spread_ms: float
    chirps_found: int
    time: int


@dataclass
class CalibrationStore:
    path: Path | None
    measurements: list[Measurement] = field(default_factory=list)
    default_delay_ms: int | None = None

    @classmethod
    def load(cls, path: Path | None) -> CalibrationStore:
        store = cls(path)
        if path is None:
            return store
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return store
        except (OSError, ValueError) as err:
            _LOGGER.warning("Messwerte %s unlesbar: %s", path, err)
            return store
        store.measurements = [Measurement(**m) for m in raw.get("measurements", []) if isinstance(m, dict)]
        value = raw.get("default_delay_ms")
        store.default_delay_ms = value if isinstance(value, int) else None
        return store

    def save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_suffix(".tmp")
        data = {"measurements": [asdict(m) for m in self.measurements], "default_delay_ms": self.default_delay_ms}
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


# -- orchestration ---------------------------------------------------------------------


class Calibrator:
    """Runs one measurement at a time; state is polled by the web page."""

    def __init__(self, options, pipeline, link, device_store, store: CalibrationStore,
                 schedule: Schedule | None = None) -> None:
        self.options = options
        self.pipeline = pipeline
        self.link = link
        self.devices = device_store
        self.store = store
        self.schedule = schedule or Schedule()
        self.status = "bereit"
        self.busy = False
        self.error: str | None = None
        self.last: Result | None = None
        self._task: asyncio.Task | None = None
        if store.default_delay_ms is not None:
            device_store.default_delay_ms = store.default_delay_ms

    @property
    def configured(self) -> bool:
        return bool(self.options.calibration_mic and self.options.calibration_key)

    def state(self) -> dict:
        return {
            "configured": self.configured,
            "mic": self.options.calibration_mic,
            "busy": self.busy,
            "status": self.status,
            "error": self.error,
            "last": asdict(self.last) if self.last else None,
            "measurements": [asdict(m) for m in self.store.measurements],
            "measured_default_ms": self.store.default_delay_ms,
            "option_default_ms": self.options.delay_report_ms,
        }

    # -- public actions ---------------------------------------------------------------

    def start_measurement(self, label: str) -> None:
        if self.busy:
            raise RuntimeError("Es läuft bereits eine Messung")
        if not self.configured:
            raise RuntimeError("Mikrofon nicht eingerichtet (Optionen calibration_mic/calibration_key)")
        self.busy, self.error, self.status = True, None, "starte …"
        self._task = asyncio.create_task(self._run(label.strip()[:60] or "Messung"))

    async def test_connection(self) -> ClockSync:
        async with self._open() as (link, addr):
            return await self._sync(link, addr, 15)

    def set_default(self, value: int | None) -> int:
        self.store.default_delay_ms = None if value is None else max(0, min(6000, int(value)))
        self.store.save()
        effective = self.options.delay_report_ms if value is None else self.store.default_delay_ms
        self.devices.default_delay_ms = effective
        return effective

    def delete(self, index: int) -> None:
        if 0 <= index < len(self.store.measurements):
            del self.store.measurements[index]
            self.store.save()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    # -- internals --------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _open(self):
        host, port = parse_target(self.options.calibration_mic)
        ip = await resolve(host)
        loop = asyncio.get_running_loop()
        transport, link = await loop.create_datagram_endpoint(MicLink, local_addr=("0.0.0.0", 0))
        link.peer_ip = ip
        try:
            yield link, (ip, port)
        finally:
            transport.close()

    async def _sync(self, link: MicLink, addr, count: int) -> ClockSync:
        link.pongs.clear()
        for seq in range(count):
            link.transport.sendto(pack_ping(self.options.calibration_key, seq, now_us()), addr)
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.2)
        sync = estimate_offset(list(link.pongs))
        if sync is None:
            raise MeasurementError("ESP32 antwortet nicht – Adresse und Schlüssel prüfen, ESPHome-Log ansehen")
        return sync

    async def _run(self, label: str) -> None:
        sched = self.schedule
        try:
            async with self._open() as (link, addr):
                self.status = "Uhrenabgleich mit dem ESP32 …"
                before = await self._sync(link, addr, 40)
                self.pipeline.calibrating = True
                self.status = "Warte auf Music Assistant – Live Input auf der Box/Gruppe abspielen"
                for _ in range(300):
                    if self.link.streaming:
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise MeasurementError(
                        "Music Assistant spielt den Live Input nicht ab – in MA auf der Box starten und erneut messen"
                    )
                self.status = "Messton läuft …"
                link.chunks.clear()
                link.recording = True
                duration_ms = int((sched.emit_s + 2) * 1000)
                link.transport.sendto(pack_start(self.options.calibration_key, duration_ms), addr)
                await asyncio.sleep(0.3)  # microphone start-up
                t0 = await self._emit(sched)
                link.recording = False
                link.transport.sendto(pack_stop(self.options.calibration_key), addr)
                self.status = "Uhrenabgleich und Auswertung …"
                after = await self._sync(link, addr, 20)
                sync = ClockSync((before.offset_us + after.offset_us) // 2, min(before.rtt_us, after.rtt_us),
                                 before.samples + after.samples)
                result = await asyncio.to_thread(analyze, list(link.chunks), sync, t0, sched)
            result.latency_ms = round(result.latency_ms - self.options.mic_offset_ms, 1)
            self.last = result
            self.store.measurements.append(
                Measurement(label, result.latency_ms, result.spread_ms, result.chirps_found, int(time.time()))
            )
            self.store.measurements = self.store.measurements[-30:]
            self.store.save()
            self.status = f"{label}: {result.latency_ms:.0f} ms (±{result.spread_ms / 2:.0f} ms)"
            _LOGGER.info("Latenz %s: %.0f ms (Streuung %.0f ms, %d/%d Töne, Uhr-RTT %.0f ms)", label,
                         result.latency_ms, result.spread_ms, result.chirps_found, result.chirps_total, result.rtt_ms)
        except (MeasurementError, OSError, ValueError) as err:
            self.error = str(err)
            self.status = "Messung fehlgeschlagen"
            _LOGGER.warning("Messung fehlgeschlagen: %s", err)
        finally:
            self.pipeline.calibrating = False
            self.busy = False

    async def _emit(self, sched: Schedule) -> int:
        """Feed the test signal in real time; returns the add-on time of its first sample."""
        pcm = emission_pcm(sched)
        frame = OUT_CHANNELS * 2
        step = int(CHUNK_S * OUT_RATE) * frame
        t0_ns = time.monotonic_ns()
        for i, off in enumerate(range(0, len(pcm), step)):
            at_ns = t0_ns + int(i * CHUNK_S * 1e9)
            delay = (at_ns - time.monotonic_ns()) / 1e9
            if delay > 0:
                await asyncio.sleep(delay)
            self.pipeline.inject(pcm[off : off + step], at_ns)
        return t0_ns // 1000
