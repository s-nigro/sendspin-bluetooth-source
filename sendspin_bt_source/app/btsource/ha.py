"""Home Assistant persistent notifications via the Supervisor proxy (for the pairing code)."""

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
