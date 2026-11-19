"""Per-device settings (delay) and the devices that have streamed to us, kept in /data."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

MAX_DELAY_MS = 3000


class DeviceStore:
    def __init__(self, path: Path | None, default_delay_ms: int) -> None:
        self.path = path
        self.default_delay_ms = default_delay_ms
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as err:
            _LOGGER.warning("Geräteliste %s unlesbar, starte leer: %s", self.path, err)
            return
        if isinstance(raw, dict):
            self._data = {k.upper(): v for k, v in raw.items() if isinstance(v, dict)}

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- queries --------------------------------------------------------------------

    def known(self, address: str) -> bool:
        return address.upper() in self._data

    def custom_delay(self, address: str) -> int | None:
        value = self._data.get(address.upper(), {}).get("delay_ms")
        return value if isinstance(value, int) else None

    def delay_for(self, address: str) -> int:
        custom = self.custom_delay(address)
        return self.default_delay_ms if custom is None else custom

    def entry(self, address: str) -> dict:
        return dict(self._data.get(address.upper(), {}))

    # -- updates --------------------------------------------------------------------

    def remember(self, address: str, name: str) -> None:
        """Record a device that connected to our A2DP endpoint."""
        entry = self._data.setdefault(address.upper(), {})
        entry["name"] = name
        entry["last_seen"] = int(time.time())
        self._save()

    def set_delay(self, address: str, delay_ms: int | None) -> int:
        """Set a device's delay; ``None`` returns it to the add-on default. Returns the effective delay."""
        entry = self._data.setdefault(address.upper(), {})
        if delay_ms is None:
            entry.pop("delay_ms", None)
        else:
            entry["delay_ms"] = max(0, min(MAX_DELAY_MS, int(delay_ms)))
        self._save()
        return self.delay_for(address)

    def forget(self, address: str) -> None:
        if self._data.pop(address.upper(), None) is not None:
            self._save()
