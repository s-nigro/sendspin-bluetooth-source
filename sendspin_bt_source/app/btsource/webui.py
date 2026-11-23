"""Device management page, served through Home Assistant ingress."""

from __future__ import annotations

import logging
import re
import time
from importlib import resources
from typing import TYPE_CHECKING

from aiohttp import web
from dbus_fast import DBusError

if TYPE_CHECKING:
    from .bluez import BluezManager
    from .calibration import Calibrator
    from .sendspin_link import SendspinLink

_LOGGER = logging.getLogger(__name__)

# The Supervisor's ingress proxy. With host_network the port is reachable from the
# LAN too, so everything else is refused.
INGRESS_PEERS = {"172.30.32.2"}
LOCAL_PEERS = {"127.0.0.1", "::1"}
_MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
PAIRING_SECONDS = (60, 180, 600)


class WebUI:
    def __init__(self, bluez: BluezManager, link: SendspinLink, port: int, *, allow_local: bool = False,
                 calibrator: Calibrator | None = None) -> None:
        self.bluez = bluez
        self.link = link
        self.calibrator = calibrator
        self.port = port
        self._allowed = INGRESS_PEERS | (LOCAL_PEERS if allow_local else set())
        self._runner: web.AppRunner | None = None
        self.app = web.Application(middlewares=[self._guard])
        self.app.add_routes([
            web.get("/", self._index),
            web.get("/api/state", self._state),
            web.post("/api/pairing", self._pairing),
            web.post("/api/devices/{mac}/remove", self._remove),
            web.post("/api/devices/{mac}/disconnect", self._disconnect),
            web.post("/api/devices/{mac}/delay", self._delay),
            web.post("/api/devices/{mac}/claim", self._claim),
            web.post("/api/calibration/test", self._cal_test),
            web.post("/api/calibration/measure", self._cal_measure),
            web.post("/api/calibration/default", self._cal_default),
            web.post("/api/calibration/delete", self._cal_delete),
        ])

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host=None, port=self.port)
        await site.start()
        _LOGGER.info("Geräteverwaltung bereit (Seitenleiste „Bluetooth-Quelle“)")

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    @web.middleware
    async def _guard(self, request: web.Request, handler):
        if request.remote not in self._allowed:
            raise web.HTTPForbidden(text="Nur über Home Assistant erreichbar")
        try:
            return await handler(request)
        except DBusError as err:
            return web.json_response({"error": str(err)}, status=502)

    # -- handlers ---------------------------------------------------------------------

    async def _index(self, request: web.Request) -> web.Response:
        html = resources.files(__package__).joinpath("webui.html").read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def _state(self, request: web.Request) -> web.Response:
        b = self.bluez
        remaining = None
        if b.pairing_until is not None:
            remaining = max(0, round(b.pairing_until - time.monotonic()))
        return web.json_response({
            "name": b.options.device_name,
            "adapter": b.adapter_path.rsplit("/", 1)[-1],
            "adapter_address": b.adapter_address,
            "pairing": {
                "open": b.pairing_open,
                "always": b.pairing_always_on,
                "remaining": remaining,
                "choices": PAIRING_SECONDS,
            },
            "default_delay_ms": b.store.default_delay_ms,
            "music_assistant": {
                "connected": self.link.ma_connected,
                "streaming": self.link.streaming,
                "signal": self.link.signal_present,
            },
            "devices": await b.list_devices(),
            "calibration": self.calibrator.state() if self.calibrator else None,
        })

    async def _pairing(self, request: web.Request) -> web.Response:
        body = await _json(request)
        seconds = body.get("seconds")
        if seconds in (0, None, False):
            await self.bluez.close_pairing()
        else:
            if not isinstance(seconds, int) or not 10 <= seconds <= 3600:
                raise web.HTTPBadRequest(text="seconds: 10–3600")
            await self.bluez.open_pairing(seconds)
        return web.json_response({"ok": True})

    async def _remove(self, request: web.Request) -> web.Response:
        await self.bluez.remove_device(_mac(request))
        return web.json_response({"ok": True})

    async def _disconnect(self, request: web.Request) -> web.Response:
        try:
            await self.bluez.disconnect_device(_mac(request))
        except KeyError:
            raise web.HTTPNotFound(text="Gerät unbekannt") from None
        return web.json_response({"ok": True})

    async def _claim(self, request: web.Request) -> web.Response:
        ok = await self.bluez.claim_device(_mac(request))
        if not ok:
            return web.json_response({"error": "Übernahme fehlgeschlagen – Details im Add-on-Log"}, status=409)
        return web.json_response({"ok": True})

    # -- latency measurement ----------------------------------------------------------

    def _cal(self) -> Calibrator:
        if self.calibrator is None:
            raise web.HTTPNotFound(text="Messung nicht verfügbar")
        return self.calibrator

    async def _cal_test(self, request: web.Request) -> web.Response:
        from .calibration import MeasurementError

        cal = self._cal()
        if not cal.configured:
            return web.json_response({"error": "Mess-Mikrofon in den Add-on-Optionen eintragen"}, status=409)
        try:
            sync = await cal.test_connection()
        except (MeasurementError, OSError, ValueError) as err:
            return web.json_response({"error": str(err)}, status=409)
        return web.json_response({"ok": True, "rtt_ms": round(sync.rtt_us / 1000, 1), "pongs": sync.samples})

    async def _cal_measure(self, request: web.Request) -> web.Response:
        body = await _json(request)
        try:
            self._cal().start_measurement(str(body.get("label", "")))
        except RuntimeError as err:
            return web.json_response({"error": str(err)}, status=409)
        return web.json_response({"ok": True})

    async def _cal_default(self, request: web.Request) -> web.Response:
        body = await _json(request)
        value = body.get("delay_ms")
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)
                                  or not 0 <= value <= 6000):
            raise web.HTTPBadRequest(text="delay_ms: 0–6000 oder null")
        effective = self._cal().set_default(None if value is None else round(value))
        for info in list(self.bluez.transports.values()):
            await self.bluez.push_delay(info)
        return web.json_response({"ok": True, "default_delay_ms": effective})

    async def _cal_delete(self, request: web.Request) -> web.Response:
        body = await _json(request)
        index = body.get("index")
        if not isinstance(index, int):
            raise web.HTTPBadRequest(text="index erwartet")
        self._cal().delete(index)
        return web.json_response({"ok": True})

    async def _delay(self, request: web.Request) -> web.Response:
        body = await _json(request)
        value = body.get("delay_ms")
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 6000):
            raise web.HTTPBadRequest(text="delay_ms: 0–6000 oder null")
        effective = await self.bluez.set_device_delay(_mac(request), value)
        return web.json_response({"ok": True, "delay_ms": effective})


def _mac(request: web.Request) -> str:
    mac = request.match_info["mac"].upper()
    if not _MAC_RE.match(mac):
        raise web.HTTPBadRequest(text="ungültige MAC-Adresse")
    return mac


async def _json(request: web.Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="JSON erwartet") from None
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="JSON-Objekt erwartet")
    return body
