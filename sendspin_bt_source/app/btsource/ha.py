"""Supervisor calls: pairing-code notifications and restarting the HA audio system."""

from __future__ import annotations

import logging
import os

import aiohttp

_LOGGER = logging.getLogger(__name__)

_NOTIFICATION_ID = "sendspin_bt_source_pairing"


class Notifier:
    def __init__(self, device_name: str) -> None:
        self._token = os.environ.get("SUPERVISOR_TOKEN")
        self._device_name = device_name

    async def __call__(self, raw_code: str | None, code: str | None) -> None:
        if not self._token:
            return
        if code:
            service = "create"
            data = {
                "notification_id": _NOTIFICATION_ID,
                "title": f"Sendspin Bluetooth Source: {self._device_name}",
                "message": (
                    "Music Assistant möchte die Bluetooth-Quelle koppeln.\n\n"
                    f"Kopplungscode: **{code}**\n\n"
                    "Diesen Code in Music Assistant eingeben."
                ),
            }
        else:
            service = "dismiss"
            data = {"notification_id": _NOTIFICATION_ID}
        url = f"http://supervisor/core/api/services/persistent_notification/{service}"
        try:
            async with aiohttp.ClientSession() as session, session.post(
                url,
                json=data,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 400:
                    _LOGGER.debug("Benachrichtigung fehlgeschlagen: HTTP %s", resp.status)
        except aiohttp.ClientError as err:
            _LOGGER.debug("Benachrichtigung fehlgeschlagen: %s", err)


async def restart_audio() -> bool:
    """Restart the HA audio plugin (PulseAudio) via the Supervisor, like ``ha audio restart``.

    PulseAudio registers its own A2DP sink endpoints on every adapter. Senders that only
    know SBC take the first SBC sink BlueZ lists, which is whoever registered first, so
    letting PulseAudio register again after us puts our endpoint in front.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        _LOGGER.warning("Kein Supervisor-Zugang – HA-Audiosystem nicht neu gestartet")
        return False
    _LOGGER.info("Starte das HA-Audiosystem neu, damit dieser Empfänger Vorrang hat "
                 "(Boxen der Bluetooth Bridge verbinden sich kurz neu)")
    try:
        async with aiohttp.ClientSession() as session, session.post(
            "http://supervisor/audio/restart",
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            if resp.status >= 400:
                text = (await resp.text())[:200]
                _LOGGER.warning("Neustart des HA-Audiosystems abgelehnt: HTTP %s %s", resp.status, text)
                return False
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("Neustart des HA-Audiosystems fehlgeschlagen: %s", err)
        return False
    _LOGGER.info("HA-Audiosystem neu gestartet")
    return True
