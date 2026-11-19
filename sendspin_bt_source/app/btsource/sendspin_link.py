"""Sendspin client with the source@v1 role: shows up in Music Assistant as a Live Input."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import ClientError, web
from aiosendspin.client import ClientListener, PairingSupport, SendspinClient, SourceCapture
from aiosendspin.models.core import DeviceInfo
from aiosendspin.models.player import SupportedAudioFormat
from aiosendspin.models.source import ClientHelloSourceFeatures, ClientHelloSourceSupport
from aiosendspin.models.types import AudioCodec, PairAbortReason, Roles, SignalState
from aiosendspin.noise import FileClientPairingStore, Identity, b64url_decode, b64url_encode
from aiosendspin.noise.driver import HandshakeAbortedError

from . import __version__
from .a2dp import OUTPUT_CHANNELS, OUTPUT_RATE
from .config import Options
from .timing import CaptureClock

_LOGGER = logging.getLogger(__name__)

PairingCodeHook = Callable[[str | None, str | None], Awaitable[None]]


def load_identity(path: Path) -> Identity:
    try:
        data = json.loads(path.read_text())
        return Identity.from_private_bytes(b64url_decode(data["private_key"]))
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, KeyError, ValueError, OSError) as err:
        _LOGGER.warning("Identität in %s unlesbar, erzeuge neue (MA muss neu koppeln): %s", path, err)
    identity = Identity.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"private_key": b64url_encode(identity.private_bytes)}))
    return identity


class SendspinLink:
    """Owns the Sendspin client, the pairing flow and the source capture stream."""

    def __init__(
        self,
        options: Options,
        data_dir: Path,
        on_pairing_code: PairingCodeHook | None = None,
        *,
        advertise_mdns: bool = True,
    ) -> None:
        self.options = options
        self._advertise_mdns = advertise_mdns
        self.data_dir = data_dir
        self._on_pairing_code = on_pairing_code
        self.client: SendspinClient | None = None
        self._listener: ClientListener | None = None
        self._capture: SourceCapture | None = None
        self._capture_lock = asyncio.Lock()
        self._clock = CaptureClock(OUTPUT_RATE)
        self._clock_owner: SourceCapture | None = None
        self._signal = SignalState.ABSENT
        self._reported: tuple[int, SignalState] | None = None
        self._tasks: list[asyncio.Task] = []
        self._audio_format = SupportedAudioFormat(
            codec=AudioCodec(options.stream_codec),
            channels=OUTPUT_CHANNELS,
            sample_rate=OUTPUT_RATE,
            bit_depth=16,
        )

    # -- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        identity = load_identity(self.data_dir / "identity.json")
        store = await FileClientPairingStore.open(self.data_dir / "pairing-store.json")
        self.client = SendspinClient(
            identity=identity,
            client_name=self.options.device_name,
            roles=[Roles.SOURCE],
            pairing_store=store,
            device_info=DeviceInfo(
                product_name="Sendspin Bluetooth Source",
                manufacturer="Home Assistant Add-on",
                software_version=__version__,
            ),
            source_support=ClientHelloSourceSupport(features=ClientHelloSourceFeatures(line_sense=True)),
            pairing_support=PairingSupport(
                pairing_code_display=self._show_pairing_code,
                offer_static_pairing_code=False,
                pair_pending_message="Kopplungscode steht im Log bzw. in den HA-Benachrichtigungen.",
            ),
        )
        self.client.add_pairing_abort_listener(self._on_pairing_abort)
        self.client.add_disconnect_listener(self._on_disconnect)
        _LOGGER.info("Sendspin-ID: %s", identity.peer_id)

        self._listener = ClientListener(
            client_id=identity.peer_id,
            on_connection=self._handle_server_connection,
            port=self.options.listen_port,
            client_name=self.options.device_name,
            advertise_mdns=self._advertise_mdns,
        )
        await self._listener.start()
        _LOGGER.info("Warte auf Music Assistant (mDNS _sendspin._tcp, Port %d)", self.options.listen_port)
        if self.options.server_url:
            self._tasks.append(asyncio.create_task(self._connect_loop(self.options.server_url)))
        self._tasks.append(asyncio.create_task(self._reconcile_loop()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._stop_capture()
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.disconnect()
        if self._listener is not None:
            await self._listener.stop()

    async def _handle_server_connection(self, ws: web.WebSocketResponse) -> None:
        assert self.client is not None
        try:
            await self.client.attach_websocket(ws)
        except Exception:  # noqa: BLE001 - keep listener alive
            _LOGGER.exception("Fehler in der Sendspin-Verbindung")

    async def _connect_loop(self, url: str) -> None:
        assert self.client is not None
        backoff = 2.0
        while True:
            try:
                if not self.client.connected:
                    await self.client.connect(url)
                    _LOGGER.info("Mit %s verbunden", url)
                    backoff = 2.0
                done = asyncio.Event()
                unsub = self.client.add_disconnect_listener(done.set)
                if self.client.connected:
                    await done.wait()
                unsub()
            except (TimeoutError, OSError, ClientError, RuntimeError, HandshakeAbortedError) as err:
                _LOGGER.warning("Verbindung zu %s fehlgeschlagen: %s (neuer Versuch in %.0f s)", url, err, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120.0)

    # -- pairing ----------------------------------------------------------------------

    async def _show_pairing_code(self, pairing_code: str | None, *, grouped: str | None) -> None:
        if grouped is not None:
            _LOGGER.warning("Music Assistant möchte koppeln – Kopplungscode: %s", grouped)
        if self._on_pairing_code is not None:
            with contextlib.suppress(Exception):
                await self._on_pairing_code(pairing_code, grouped)

    def _on_pairing_abort(self, reason: PairAbortReason) -> None:
        _LOGGER.warning("Kopplung mit Music Assistant abgebrochen: %s", reason.value)

    def _on_disconnect(self) -> None:
        self._capture = None
        self._reported = None

    # -- source role --------------------------------------------------------------------

    def _connection(self):
        client = self.client
        if client is None or not client.connected:
            return None
        return getattr(client, "_admitted_connection", None)

    def set_signal(self, present: bool) -> None:
        self._signal = SignalState.PRESENT if present else SignalState.ABSENT

    @property
    def streaming(self) -> bool:
        """True while Music Assistant has asked for audio and the stream is open."""
        return self._capture is not None

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await self._reconcile()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("reconcile failed", exc_info=True)
            await asyncio.sleep(0.2)

    async def _reconcile(self) -> None:
        conn = self._connection()
        if conn is None:
            self._capture = None
            return
        if not conn._is_role_active("source") or not conn.is_time_synchronized():  # noqa: SLF001
            return
        # signal state (line_sense)
        key = (id(conn), self._signal)
        if self._reported != key:
            await conn.send_source_signal(self._signal)
            self._reported = key
            _LOGGER.info("Signal an Music Assistant: %s", self._signal.value)
        # open / close the capture stream as the server requests
        async with self._capture_lock:
            if self._capture is not None and not conn.is_source_stream_active():
                self._capture = None
                _LOGGER.info("Music Assistant hat die Übertragung beendet")
            if self._capture is None and conn.is_source_start_authorized():
                capture = self.client.create_source_capture(self._audio_format)
                await capture.start()
                self._capture = capture
                _LOGGER.info(
                    "Music Assistant hat die Übertragung gestartet (%s, %d Hz)",
                    capture.codec.value, capture.audio_format.sample_rate,
                )

    async def feed(self, pcm: bytes, captured_monotonic_ns: int, now_monotonic_ns: int) -> None:
        capture = self._capture
        if capture is None or self.client is None:
            return
        if capture is not self._clock_owner:
            self._clock.reset()
            self._clock_owner = capture
        age_us = max(0, (now_monotonic_ns - captured_monotonic_ns) // 1000)
        frames = len(pcm) // (OUTPUT_CHANNELS * 2)
        ts = self._clock.timestamp(self.client.now_us() - age_us, frames)
        try:
            await capture.feed(pcm, capture_timestamp_us=ts)
        except RuntimeError as err:
            _LOGGER.debug("Capture beendet: %s", err)
            self._capture = None

    async def _stop_capture(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            with contextlib.suppress(Exception):
                await capture.stop()
