"""Add-on options (read from /data/options.json)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


@dataclass
class Options:
    device_name: str = "Camping Sound"
    adapter: str = ""
    discoverable: bool = True
    discoverable_timeout: int = 0
    """Minutes the adapter stays discoverable after start; 0 = always."""
    allowed_devices: list[str] = field(default_factory=list)
    """MAC addresses allowed to pair/stream; empty = everyone (while discoverable)."""
    remove_devices: list[str] = field(default_factory=list)
    """MAC addresses to unpair from the adapter at start (to pair them again)."""
    delay_report_ms: int = 1000
    """Delay reported to the sending device so it can hold back the picture."""
    enable_aac: bool = True
    apply_remote_volume: bool = True
    silence_timeout: int = 20
    """Seconds of silence after which the input reports 'no signal' (0 = never)."""
    stream_codec: str = "pcm"
    server_url: str = ""
    listen_port: int = 8935
    set_device_class: bool = True
    web_port: int = 8936
    """Port of the device management page (HA ingress); 0 = off."""
    log_level: str = "info"

    @classmethod
    def load(cls, path: str | Path) -> Options:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        opts = cls(**known)
        opts.validate()
        return opts

    def validate(self) -> None:
        self.device_name = (self.device_name or "Sendspin Bluetooth").strip()[:248]
        self.adapter = (self.adapter or "").strip()
        self.allowed_devices = [m.strip().upper() for m in self.allowed_devices if m.strip()]
        for mac in self.allowed_devices:
            if not _MAC_RE.match(mac):
                raise ValueError(f"allowed_devices: '{mac}' ist keine gültige MAC-Adresse")
        self.remove_devices = [m.strip().upper() for m in self.remove_devices if m.strip()]
        for mac in self.remove_devices:
            if not _MAC_RE.match(mac):
                raise ValueError(f"remove_devices: '{mac}' ist keine gültige MAC-Adresse")
        self.delay_report_ms = max(0, min(3000, int(self.delay_report_ms)))
        self.silence_timeout = max(0, int(self.silence_timeout))
        if self.stream_codec not in ("pcm", "flac", "opus"):
            self.stream_codec = "pcm"
        self.server_url = (self.server_url or "").strip()

    def is_allowed(self, address: str) -> bool:
        return not self.allowed_devices or address.upper() in self.allowed_devices
