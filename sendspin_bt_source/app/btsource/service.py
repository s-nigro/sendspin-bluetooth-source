"""Glue between the Bluetooth side and the Sendspin source stream."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from . import a2dp
from .bluez import BluezManager, StreamListener, TransportInfo
from .config import Options
from .sendspin_link import SendspinLink

_LOGGER = logging.getLogger(__name__)

SILENCE_PEAK = 40  # ~ -58 dBFS


class AudioPipeline(StreamListener):
    def __init__(self, options: Options, link: SendspinLink) -> None:
        self.options = options
        self.link = link
        self._decoders: dict[str, a2dp.A2dpDecoder] = {}
        self._current: str | None = None
        self._gain = 1.0
        self._last_sound = 0.0
        self._queue: asyncio.Queue[tuple[bytes, int]] = asyncio.Queue(maxsize=400)
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._feeder()),
            asyncio.create_task(self._signal_loop()),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t

    # -- StreamListener ----------------------------------------------------------------

    def on_stream_started(self, transport: TransportInfo) -> None:
        self._decoders[transport.path] = a2dp.A2dpDecoder(transport.config)
        self._current = transport.path
        if transport.volume is None:
            self._gain = 1.0

    def on_stream_stopped(self, transport: TransportInfo) -> None:
        dec = self._decoders.pop(transport.path, None)
        if dec is not None and (dec.lost_packets or dec.decode_errors):
            _LOGGER.info(
                "Statistik %s: %d verlorene Pakete, %d Dekodierfehler",
                transport.address, dec.lost_packets, dec.decode_errors,
            )
        if self._current == transport.path:
            self._current = next(reversed(self._decoders), None)

    def on_volume(self, transport: TransportInfo, volume: int) -> None:
        if not self.options.apply_remote_volume or transport.path != self._current:
            return
        self._gain = a2dp.a2dp_volume_to_gain(volume)
        _LOGGER.debug("Lautstärke vom Gerät: %d/127 (Faktor %.2f)", volume, self._gain)

    def on_packet(self, transport: TransportInfo, data: bytes, arrival_ns: int) -> None:
        if transport.path != self._current:
            return
        dec = self._decoders.get(transport.path)
        if dec is None:
            return
        pcm = dec.feed(data)
        if not pcm:
            return
        if a2dp.peak_level(pcm) > SILENCE_PEAK:
            self._last_sound = time.monotonic()
        if not self.link.streaming:
            return
        pcm = a2dp.apply_gain(pcm, self._gain)
        try:
            self._queue.put_nowait((pcm, arrival_ns))
        except asyncio.QueueFull:
            _LOGGER.debug("Sendepuffer voll – Paket verworfen")

    # -- tasks ----------------------------------------------------------------------------

    async def _feeder(self) -> None:
        while True:
            pcm, arrival_ns = await self._queue.get()
            await self.link.feed(pcm, arrival_ns, time.monotonic_ns())

    def signal_present(self, now: float | None = None) -> bool:
        if self._current is None:
            return False
        if self._last_sound == 0.0:
            return False
        if self.options.silence_timeout <= 0:
            return True
        now = time.monotonic() if now is None else now
        return now - self._last_sound < self.options.silence_timeout

    async def _signal_loop(self) -> None:
        while True:
            self.link.set_signal(self.signal_present())
            await asyncio.sleep(0.5)


class Service:
    def __init__(self, options: Options, data_dir, notifier=None) -> None:
        self.options = options
        self.link = SendspinLink(options, data_dir, on_pairing_code=notifier)
        self.pipeline = AudioPipeline(options, self.link)
        self.bluez: BluezManager | None = None

    async def run(self) -> None:
        await self.link.start()
        self.pipeline.start()
        self.bluez = BluezManager(self.options, self.pipeline)
        await self.bluez.start()
        _LOGGER.info("Bereit. Gerät '%s' per Bluetooth koppeln und Wiedergabe starten.", self.options.device_name)
        try:
            await asyncio.Event().wait()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if self.bluez is not None:
            with contextlib.suppress(Exception):
                await self.bluez.stop()
        await self.pipeline.stop()
        await self.link.stop()
