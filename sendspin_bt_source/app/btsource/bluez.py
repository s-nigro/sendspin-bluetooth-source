# ruff: noqa: F821, F722  (D-Bus signatures as annotations, as dbus-fast requires)
"""BlueZ integration: adapter setup, pairing agent, A2DP sink endpoints and transports.

The add-on registers its own A2DP sink endpoints on the host's bluetoothd (via the
host system D-Bus), so no audio daemon is needed in the container. Audio arrives as
RTP packets on the transport's L2CAP socket and is handed to a callback.

Note: dbus-fast reads method signatures from annotations, so this module must not use
``from __future__ import annotations``.
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field

from dbus_fast import BusType, DBusError, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

from . import a2dp, mgmt
from .devices import DeviceStore
from .config import Options

_LOGGER = logging.getLogger(__name__)

BLUEZ = "org.bluez"
IFACE_ADAPTER = "org.bluez.Adapter1"
IFACE_DEVICE = "org.bluez.Device1"
IFACE_MEDIA = "org.bluez.Media1"
IFACE_TRANSPORT = "org.bluez.MediaTransport1"
IFACE_ENDPOINT = "org.bluez.MediaEndpoint1"
IFACE_AGENT_MANAGER = "org.bluez.AgentManager1"
IFACE_PROPS = "org.freedesktop.DBus.Properties"
IFACE_OM = "org.freedesktop.DBus.ObjectManager"

AGENT_PATH = "/io/sendspin/btsource/agent"
FOREIGN_GRACE_S = 1.5
ENDPOINT_BASE = "/io/sendspin/btsource/endpoint"

AUDIO_UUIDS = {
    "0000110a-0000-1000-8000-00805f9b34fb",  # A2DP source
    "0000110b-0000-1000-8000-00805f9b34fb",  # A2DP sink
    "0000110d-0000-1000-8000-00805f9b34fb",  # A2DP (advanced audio)
    "0000110c-0000-1000-8000-00805f9b34fb",  # AVRCP target
    "0000110e-0000-1000-8000-00805f9b34fb",  # AVRCP controller
    "0000110f-0000-1000-8000-00805f9b34fb",  # AVRCP controller (alt)
    "00001200-0000-1000-8000-00805f9b34fb",  # PnP information
}

_DEV_RE = re.compile(r"/dev_([0-9A-F]{2}(?:_[0-9A-F]{2}){5})")


def address_from_path(path: str) -> str:
    m = _DEV_RE.search(path)
    return m.group(1).replace("_", ":") if m else "?"


def unwrap(props: dict) -> dict:
    return {k: (v.value if isinstance(v, Variant) else v) for k, v in props.items()}


@dataclass
class TransportInfo:
    path: str
    device_path: str
    address: str
    config: a2dp.StreamConfig
    state: str = "idle"
    volume: int | None = None
    name: str = ""
    fd: int | None = None
    read_mtu: int = 0
    stats: dict = field(default_factory=dict)
    delay_reported: bool | None = None  # last attempt to report our delay to the sender


class StreamListener:
    """Callbacks from the Bluetooth side to the audio pipeline."""

    def on_stream_started(self, transport: TransportInfo) -> None: ...
    def on_packet(self, transport: TransportInfo, data: bytes, arrival_ns: int) -> None: ...
    def on_stream_stopped(self, transport: TransportInfo) -> None: ...
    def on_volume(self, transport: TransportInfo, volume: int) -> None: ...


class PairingAgent(ServiceInterface):
    """NoInputNoOutput agent: accept pairing while discoverable, audio services only."""

    def __init__(self, manager: "BluezManager") -> None:
        super().__init__("org.bluez.Agent1")
        self._m = manager

    def _check(self, device: str, *, pairing: bool) -> None:
        addr = address_from_path(device)
        if not self._m.options.is_allowed(addr):
            _LOGGER.warning("Gerät %s ist nicht in allowed_devices – abgelehnt", addr)
            raise DBusError("org.bluez.Error.Rejected", "device not allowed")
        if pairing and not self._m.pairing_open:
            _LOGGER.warning("Kopplungsanfrage von %s abgelehnt: Kopplungsmodus ist aus", addr)
            raise DBusError("org.bluez.Error.Rejected", "pairing window closed")

    @method()
    def Release(self) -> None:
        _LOGGER.debug("Agent released")

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        self._check(device, pairing=True)
        return "0000"

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s") -> None:
        self._check(device, pairing=True)

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        self._check(device, pairing=True)
        return 0

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:
        self._check(device, pairing=True)
        _LOGGER.info("Kopplung mit %s bestätigt", address_from_path(device))
        self._m.schedule_trust(device)

    @method()
    def RequestAuthorization(self, device: "o") -> None:
        self._check(device, pairing=True)
        self._m.schedule_trust(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s") -> None:
        self._check(device, pairing=False)
        if uuid.lower() not in AUDIO_UUIDS:
            _LOGGER.info("Dienst %s von %s abgelehnt", uuid, address_from_path(device))
            raise DBusError("org.bluez.Error.Rejected", "service not allowed")

    @method()
    def Cancel(self) -> None:
        _LOGGER.debug("Agent request cancelled")


class SinkEndpoint(ServiceInterface):
    """org.bluez.MediaEndpoint1 for one codec in the A2DP sink role."""

    def __init__(self, manager: "BluezManager", codec: int, capabilities: bytes) -> None:
        super().__init__("org.bluez.MediaEndpoint1")
        self._m = manager
        self.codec = codec
        self.capabilities = capabilities

    @method()
    def SetConfiguration(self, transport: "o", properties: "a{sv}") -> None:
        props = unwrap(properties)
        device = props.get("Device", "")
        addr = address_from_path(device or transport)
        if not self._m.options.is_allowed(addr):
            raise DBusError("org.bluez.Error.Rejected", "device not allowed")
        try:
            cfg = a2dp.parse_config(self.codec, bytes(props.get("Configuration", b"")))
        except (ValueError, KeyError) as err:
            _LOGGER.warning("Ungültige Codec-Konfiguration von %s: %s", addr, err)
            raise DBusError("org.bluez.Error.InvalidArguments", str(err)) from err
        self._m.on_set_configuration(transport, device, addr, cfg, props)

    @method()
    def SelectConfiguration(self, capabilities: "ay") -> "ay":
        try:
            if self.codec == a2dp.CODEC_SBC:
                return a2dp.select_sbc_config(bytes(capabilities))
            return a2dp.select_aac_config(bytes(capabilities))
        except ValueError as err:
            raise DBusError("org.bluez.Error.InvalidArguments", str(err)) from err

    @method()
    def ClearConfiguration(self, transport: "o") -> None:
        self._m.on_clear_configuration(transport)

    @method()
    def Release(self) -> None:
        _LOGGER.info("Endpoint released by BlueZ")


class BluezManager:
    def __init__(self, options: Options, listener: StreamListener, store: DeviceStore | None = None) -> None:
        self.options = options
        self.listener = listener
        self.store = store or DeviceStore(None, options.delay_report_ms)
        self.pairing_until: float | None = None  # monotonic deadline of a timed pairing window
        self.bus: MessageBus | None = None
        self.adapter_path = ""
        self.adapter_address = ""
        self.transports: dict[str, TransportInfo] = {}
        self.pairing_open = options.discoverable
        self._endpoints: list[tuple[str, SinkEndpoint]] = []
        self._loop = asyncio.get_running_loop()
        self._discoverable_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._delay_warned = False
        # A2DP streams from senders that BlueZ handed to another sink (e.g. HA audio).
        self.foreign: dict[str, tuple[str, str]] = {}  # transport path -> (address, endpoint)
        self._claims: dict[str, list[float]] = {}
        self.sender_codecs: dict[str, list[str]] = {}  # address -> codecs the sender offers
        self.sender_delay_reporting: dict[str, bool] = {}  # address -> sender supports delay reports
        # Called after our endpoints are (re-)registered with BlueZ.
        self.on_registered = None

    # -- helpers --------------------------------------------------------------

    def _spawn(self, coro) -> None:
        task = self._loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _call(self, path: str, iface: str, member: str, signature: str = "", body=None) -> Message:
        assert self.bus is not None
        reply = await self.bus.call(
            Message(
                destination=BLUEZ,
                path=path,
                interface=iface,
                member=member,
                signature=signature,
                body=body or [],
            )
        )
        if reply.message_type == MessageType.ERROR:
            raise DBusError(reply.error_name, reply.body[0] if reply.body else "")
        return reply

    async def get_prop(self, path: str, iface: str, name: str):
        reply = await self._call(path, IFACE_PROPS, "Get", "ss", [iface, name])
        return reply.body[0].value

    async def set_prop(self, path: str, iface: str, name: str, sig: str, value) -> None:
        await self._call(path, IFACE_PROPS, "Set", "ssv", [iface, name, Variant(sig, value)])

    # -- startup --------------------------------------------------------------

    async def start(self) -> None:
        self.bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        await self._find_adapter()
        await self._configure_adapter()
        await self._forget_and_list_devices()
        await self._add_match("type='signal',sender='org.bluez'")
        self.bus.add_message_handler(self._on_message)
        await self._add_match(
            "type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.bluez'"
        )
        self.bus.export(AGENT_PATH, PairingAgent(self))
        for path, ep in self._make_endpoints():
            self.bus.export(path, ep)
        await self._register_with_bluez()
        await self._scan_transports()
        if self.options.discoverable and self.options.discoverable_timeout > 0:
            self._start_pairing_timer(self.options.discoverable_timeout * 60)
        self._spawn(self._keep_default_agent())

    async def _register_with_bluez(self) -> None:
        await self._register_agent()
        await self._register_endpoints()

    async def _keep_default_agent(self) -> None:
        """Other add-ons (e.g. the BT bridge) briefly register their own agent while
        pairing speakers; reclaim the default so incoming pairings keep working."""
        while True:
            await asyncio.sleep(60)
            try:
                await self._call("/org/bluez", IFACE_AGENT_MANAGER, "RequestDefaultAgent", "o", [AGENT_PATH])
            except DBusError as err:
                _LOGGER.debug("RequestDefaultAgent: %s", err)

    async def _on_bluez_restart(self) -> None:
        _LOGGER.warning("bluetoothd wurde neu gestartet – registriere erneut")
        for info in list(self.transports.values()):
            self._close_transport(info)
        self.transports.clear()
        await asyncio.sleep(2)
        try:
            await self._find_adapter()
            await self._configure_adapter()
            await self._register_with_bluez()
        except (DBusError, RuntimeError) as err:
            _LOGGER.error("Erneute Registrierung fehlgeschlagen: %s", err)
            return
        if self.on_registered is not None:
            await self.on_registered()

    async def _add_match(self, rule: str) -> None:
        assert self.bus is not None
        await self.bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[rule],
            )
        )

    async def _forget_and_list_devices(self) -> None:
        """Unpair the devices from ``remove_devices`` and log what stays paired."""
        reply = await self._call("/", IFACE_OM, "GetManagedObjects")
        prefix = self.adapter_path + "/dev_"
        devices = {
            path: unwrap(ifaces[IFACE_DEVICE])
            for path, ifaces in reply.body[0].items()
            if path.startswith(prefix) and IFACE_DEVICE in ifaces
        }
        removed: set[str] = set()
        for path, props in list(devices.items()):
            address = props.get("Address", address_from_path(path))
            if address.upper() not in self.options.remove_devices:
                continue
            try:
                await self._call(self.adapter_path, IFACE_ADAPTER, "RemoveDevice", "o", [path])
                _LOGGER.info("Gerät %s (%s) entfernt – kann jetzt neu gekoppelt werden",
                             address, props.get("Alias", "?"))
                del devices[path]
                removed.add(address.upper())
            except DBusError as err:
                _LOGGER.warning("Gerät %s konnte nicht entfernt werden: %s", address, err)
        for mac in self.options.remove_devices:
            if mac not in removed and not any(p.get("Address", "").upper() == mac for p in devices.values()):
                _LOGGER.info("Zu entfernendes Gerät %s ist nicht (mehr) gekoppelt", mac)
        paired = [p for p in devices.values() if p.get("Paired")]
        if paired:
            _LOGGER.info("Gekoppelte Geräte an diesem Adapter:")
            for props in paired:
                _LOGGER.info("  %s  %s", props.get("Address", "?"), props.get("Alias", "?"))
        else:
            _LOGGER.info("Keine gekoppelten Geräte an diesem Adapter")

    async def _find_adapter(self) -> None:
        reply = await self._call("/", IFACE_OM, "GetManagedObjects")
        adapters = {
            path: unwrap(ifaces[IFACE_ADAPTER])
            for path, ifaces in reply.body[0].items()
            if IFACE_ADAPTER in ifaces
        }
        if not adapters:
            raise RuntimeError("Kein Bluetooth-Adapter gefunden (ist host_dbus aktiv und BlueZ gestartet?)")
        wanted = self.options.adapter.lower()
        for path, props in sorted(adapters.items()):
            if not wanted or path.rsplit("/", 1)[-1] == wanted or props.get("Address", "").lower() == wanted:
                self.adapter_path = path
                self.adapter_address = props.get("Address", "")
                break
        else:
            names = ", ".join(f"{p.rsplit('/', 1)[-1]} ({v.get('Address')})" for p, v in adapters.items())
            raise RuntimeError(f"Adapter '{self.options.adapter}' nicht gefunden. Vorhanden: {names}")
        _LOGGER.info("Verwende Adapter %s (%s)", self.adapter_path, self.adapter_address)
        if len(adapters) > 1 and not wanted:
            _LOGGER.info("Mehrere Adapter gefunden – mit der Option 'adapter' lässt sich einer festlegen")

    async def _configure_adapter(self) -> None:
        path = self.adapter_path
        await self.set_prop(path, IFACE_ADAPTER, "Powered", "b", True)
        await self.set_prop(path, IFACE_ADAPTER, "Alias", "s", self.options.device_name)
        await self.set_prop(path, IFACE_ADAPTER, "Pairable", "b", self.pairing_open)
        await self.set_prop(path, IFACE_ADAPTER, "PairableTimeout", "u", 0)
        await self.set_prop(path, IFACE_ADAPTER, "DiscoverableTimeout", "u", 0)
        await self.set_prop(path, IFACE_ADAPTER, "Discoverable", "b", self.pairing_open)
        if self.options.set_device_class:
            await self._set_device_class()
        _LOGGER.info(
            "Bluetooth-Name '%s', sichtbar: %s", self.options.device_name, "ja" if self.pairing_open else "nein"
        )

    async def _set_device_class(self) -> None:
        """Advertise as Audio/Video loudspeaker so TVs and projectors list it as a speaker."""
        index = int(self.adapter_path.rsplit("hci", 1)[-1])
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, mgmt.set_device_class, index, 0x04, 0x14)
        except OSError as err:
            _LOGGER.warning("Geräteklasse konnte nicht gesetzt werden: %s", err)
        try:
            cls = await self.get_prop(self.adapter_path, IFACE_ADAPTER, "Class")
        except DBusError:
            return
        if (cls >> 8) & 0x1F == 0x04:
            _LOGGER.info("Geräteklasse: Audio/Video (0x%06x)", cls)
        else:
            _LOGGER.info("Geräteklasse: 0x%06x (kein Audiogerät)", cls)

    async def _register_agent(self) -> None:
        await self._call("/org/bluez", IFACE_AGENT_MANAGER, "RegisterAgent", "os", [AGENT_PATH, "NoInputNoOutput"])
        try:
            await self._call("/org/bluez", IFACE_AGENT_MANAGER, "RequestDefaultAgent", "o", [AGENT_PATH])
        except DBusError as err:
            _LOGGER.warning("Konnte nicht Standard-Agent werden (%s) – Kopplung evtl. über anderen Agenten", err)

    def _make_endpoints(self) -> list[tuple[str, SinkEndpoint]]:
        codecs = []
        if self.options.enable_aac:
            codecs.append((a2dp.CODEC_AAC, a2dp.AAC_CAPABILITIES, "aac"))
        codecs.append((a2dp.CODEC_SBC, a2dp.SBC_CAPABILITIES, "sbc"))
        self._endpoints = [(f"{ENDPOINT_BASE}/{name}", SinkEndpoint(self, codec, caps)) for codec, caps, name in codecs]
        return self._endpoints

    async def _register_endpoints(self) -> None:
        # AAC first: Android picks the highest-priority codec, which also wins over
        # the SBC-only sink endpoint of the HA audio system.
        for path, ep in self._endpoints:
            props = {
                "UUID": Variant("s", a2dp.A2DP_SINK_UUID),
                "Codec": Variant("y", ep.codec),
                "Capabilities": Variant("ay", ep.capabilities),
                "DelayReporting": Variant("b", True),
            }
            await self._call(self.adapter_path, IFACE_MEDIA, "RegisterEndpoint", "oa{sv}", [path, props])
            _LOGGER.info("A2DP-Empfang (%s) registriert", path.rsplit("/", 1)[-1].upper())

    async def stop(self) -> None:
        if self._discoverable_task:
            self._discoverable_task.cancel()
        live = [t for t in self.transports.values() if t.fd is not None]
        for t in live:
            self._close_transport(t, release=False)
        if self.bus is None:
            return
        # Release before disconnecting, otherwise the call dies with the bus.
        for t in live:
            await self._release(t)
        for path, _ep in self._endpoints:
            try:
                await self._call(self.adapter_path, IFACE_MEDIA, "UnregisterEndpoint", "o", [path])
            except DBusError:
                pass
        try:
            await self._call("/org/bluez", IFACE_AGENT_MANAGER, "UnregisterAgent", "o", [AGENT_PATH])
        except DBusError:
            pass
        self.bus.disconnect()

    # -- pairing window ----------------------------------------------------------

    async def _close_pairing_after(self, seconds: int) -> None:
        await asyncio.sleep(seconds)
        self.pairing_until = None
        self._discoverable_task = None
        await self.set_pairing(False)

    def _start_pairing_timer(self, seconds: int) -> None:
        if self._discoverable_task is not None:
            self._discoverable_task.cancel()
        self.pairing_until = time.monotonic() + seconds
        self._discoverable_task = self._loop.create_task(self._close_pairing_after(seconds))

    @property
    def pairing_always_on(self) -> bool:
        return self.options.discoverable and self.options.discoverable_timeout == 0

    async def open_pairing(self, seconds: int) -> None:
        """Make the adapter visible and pairable for ``seconds`` (no-op while always on)."""
        if self.pairing_always_on:
            return
        await self.set_pairing(True)
        self._start_pairing_timer(seconds)

    async def close_pairing(self) -> None:
        if self.pairing_always_on:
            return
        if self._discoverable_task is not None:
            self._discoverable_task.cancel()
            self._discoverable_task = None
        self.pairing_until = None
        await self.set_pairing(False)

    # -- device management -------------------------------------------------------

    async def _adapter_devices(self) -> dict[str, dict]:
        reply = await self._call("/", IFACE_OM, "GetManagedObjects")
        prefix = self.adapter_path + "/dev_"
        return {
            path: unwrap(ifaces[IFACE_DEVICE])
            for path, ifaces in reply.body[0].items()
            if path.startswith(prefix) and IFACE_DEVICE in ifaces
        }

    def _note_sender_codecs(self, objects: dict) -> None:
        """Remember which codecs connected senders offer (their A2DP source SEPs)."""
        found: dict[str, list[str]] = {}
        delay: dict[str, bool] = {}
        for path, ifaces in objects.items():
            if not path.startswith(self.adapter_path + "/dev_") or IFACE_ENDPOINT not in ifaces:
                continue
            sep = unwrap(ifaces[IFACE_ENDPOINT])
            if str(sep.get("UUID", "")).lower() != a2dp.A2DP_SOURCE_UUID:
                continue
            address = address_from_path(path)
            name = a2dp.codec_name(int(sep.get("Codec", -1)), bytes(sep.get("Capabilities", b"")))
            found.setdefault(address, []).append(name)
            if "DelayReporting" in sep:
                delay[address] = delay.get(address, False) or bool(sep["DelayReporting"])
        for address, names in found.items():
            names = sorted(set(names))
            dr = delay.get(address)
            if self.sender_codecs.get(address) != names or self.sender_delay_reporting.get(address) != dr:
                self.sender_codecs[address] = names
                if dr is not None:
                    self.sender_delay_reporting[address] = dr
                _LOGGER.info("Codecs von %s: %s – Verzögerungsmeldung: %s", address, ", ".join(names),
                             {True: "unterstützt", False: "NICHT unterstützt", None: "unbekannt"}[dr])

    async def list_devices(self) -> list[dict]:
        """Devices on our adapter. ``source`` marks phones/TVs (vs. speakers of the BT bridge)."""
        reply = await self._call("/", IFACE_OM, "GetManagedObjects")
        self._note_sender_codecs(reply.body[0])
        out = []
        for path, props in (await self._adapter_devices()).items():
            address = str(props.get("Address", address_from_path(path))).upper()
            uuids = {str(u).lower() for u in props.get("UUIDs", [])}
            live = [t for t in self.transports.values() if t.address.upper() == address]
            if not (props.get("Paired") or live or self.store.known(address)):
                continue  # merely discovered nearby
            out.append({
                "address": address,
                "name": str(props.get("Alias") or props.get("Name") or address),
                "paired": bool(props.get("Paired")),
                "trusted": bool(props.get("Trusted")),
                "connected": bool(props.get("Connected")),
                "source": a2dp.A2DP_SOURCE_UUID in uuids or self.store.known(address) or bool(live),
                "streaming": any(t.fd is not None for t in live),
                "codec": live[-1].config.description if live else None,
                "volume": live[-1].volume if live else None,
                "delay_ms": self.store.delay_for(address),
                "custom_delay": self.store.custom_delay(address) is not None,
                "foreign": next((what for a, what in self.foreign.values() if a == address), None),
                "sender_codecs": self.sender_codecs.get(address, []),
                "delay_reporting": self.sender_delay_reporting.get(address),
                "delay_reported": live[-1].delay_reported if live else None,
            })
        out.sort(key=lambda d: (not d["source"], d["name"].lower()))
        return out

    async def _device_path(self, address: str) -> str:
        address = address.upper()
        for path, props in (await self._adapter_devices()).items():
            if str(props.get("Address", address_from_path(path))).upper() == address:
                return path
        raise KeyError(address)

    async def remove_device(self, address: str) -> None:
        """Unpair a device so it can be paired again."""
        address = address.upper()
        for info in [t for t in self.transports.values() if t.address.upper() == address]:
            self._close_transport(info, release=False)
        try:
            path = await self._device_path(address)
        except KeyError:
            self.store.forget(address)
            return
        await self._call(self.adapter_path, IFACE_ADAPTER, "RemoveDevice", "o", [path])
        self.store.forget(address)
        _LOGGER.info("Gerät %s entfernt – kann jetzt neu gekoppelt werden", address)

    async def disconnect_device(self, address: str) -> None:
        path = await self._device_path(address)
        await self._call(path, IFACE_DEVICE, "Disconnect")
        _LOGGER.info("Gerät %s getrennt", address.upper())

    async def set_device_delay(self, address: str, delay_ms: int | None) -> int:
        """Store a device's delay and report it right away if it is connected."""
        effective = self.store.set_delay(address, delay_ms)
        for info in [t for t in self.transports.values() if t.address.upper() == address.upper()]:
            await self.push_delay(info)
        return effective

    async def set_pairing(self, enabled: bool) -> None:
        self.pairing_open = enabled
        await self.set_prop(self.adapter_path, IFACE_ADAPTER, "Discoverable", "b", enabled)
        await self.set_prop(self.adapter_path, IFACE_ADAPTER, "Pairable", "b", enabled)
        _LOGGER.info("Kopplungsmodus %s", "an" if enabled else "aus (gekoppelte Geräte verbinden weiterhin)")

    def schedule_trust(self, device_path: str) -> None:
        async def _trust() -> None:
            for _ in range(30):
                try:
                    if await self.get_prop(device_path, IFACE_DEVICE, "Paired"):
                        await self.set_prop(device_path, IFACE_DEVICE, "Trusted", "b", True)
                        _LOGGER.info("Gerät %s gekoppelt und als vertrauenswürdig markiert",
                                     address_from_path(device_path))
                        return
                except DBusError:
                    pass
                await asyncio.sleep(1)

        self._spawn(_trust())

    # -- endpoint callbacks --------------------------------------------------------

    def on_set_configuration(self, transport: str, device: str, addr: str, cfg: a2dp.StreamConfig, props: dict) -> None:
        info = TransportInfo(
            path=transport,
            device_path=device,
            address=addr,
            config=cfg,
            state=props.get("State", "idle"),
            volume=props.get("Volume"),
        )
        self.transports[transport] = info
        _LOGGER.info("Verbunden: %s – %s", addr, cfg.description)
        self._spawn(self._after_configuration(info))

    async def _after_configuration(self, info: TransportInfo) -> None:
        try:
            info.name = await self.get_prop(info.device_path, IFACE_DEVICE, "Alias")
        except DBusError:
            info.name = info.address
        self.store.remember(info.address, info.name)
        await self.push_delay(info, quiet=True)
        # Make sure reconnects work without a new pairing.
        try:
            if not await self.get_prop(info.device_path, IFACE_DEVICE, "Trusted"):
                await self.set_prop(info.device_path, IFACE_DEVICE, "Trusted", "b", True)
        except DBusError:
            pass
        if info.state == "pending":
            await self._acquire(info)

    def on_clear_configuration(self, transport: str) -> None:
        info = self.transports.pop(transport, None)
        if info is not None:
            self._close_transport(info)
            _LOGGER.info("Getrennt: %s", info.name or info.address)

    async def push_delay(self, info: TransportInfo, *, quiet: bool = False) -> bool:
        """Report our end-to-end delay so the sender (e.g. Google TV) can delay the picture.

        BlueZ forwards it as an AVDTP delay report; the transport must be open for that,
        so a failure before the stream starts is expected and only logged at debug level.
        """
        delay_ms = self.store.delay_for(info.address)
        value = delay_ms * 10  # 1/10 ms units
        try:
            await self.set_prop(info.path, IFACE_TRANSPORT, "Delay", "q", value)
        except DBusError as err:
            if not quiet:
                info.delay_reported = False
            if quiet:
                _LOGGER.debug("Delay noch nicht meldbar (%s)", err)
            elif not self._delay_warned:
                self._delay_warned = True
                _LOGGER.warning(
                    "Verzögerung konnte nicht gemeldet werden (%s). Das Gerät unterstützt evtl. "
                    "kein Delay Reporting oder die BlueZ-Version des Hosts ist zu alt.", err
                )
            return False
        info.delay_reported = True
        _LOGGER.info("Verzögerung %d ms an %s gemeldet", delay_ms, info.name or info.address)
        return True

    # -- signals -----------------------------------------------------------------------

    def _on_message(self, msg: Message) -> None:
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.member == "NameOwnerChanged" and msg.body and msg.body[0] == BLUEZ:
            if msg.body[2]:  # new owner -> bluetoothd (re)started
                self._spawn(self._on_bluez_restart())
            return
        if msg.member == "PropertiesChanged" and msg.interface == IFACE_PROPS:
            iface, changed = msg.body[0], unwrap(msg.body[1])
            if iface == IFACE_TRANSPORT and msg.path in self.transports:
                self._on_transport_changed(self.transports[msg.path], changed)
            elif iface == IFACE_DEVICE and "Connected" in changed and msg.path.startswith(self.adapter_path):
                state = "verbunden" if changed["Connected"] else "getrennt"
                _LOGGER.debug("Gerät %s %s", address_from_path(msg.path), state)
        elif msg.member == "InterfacesAdded" and msg.interface == IFACE_OM:
            path, ifaces = msg.body[0], msg.body[1]
            if IFACE_TRANSPORT in ifaces:
                self._spawn(self._check_transport(path, unwrap(ifaces[IFACE_TRANSPORT])))
        elif msg.member == "InterfacesRemoved" and msg.interface == IFACE_OM:
            path, ifaces = msg.body[0], msg.body[1]
            if IFACE_TRANSPORT in ifaces:
                if path in self.transports:
                    self.on_clear_configuration(path)
                self.foreign.pop(path, None)

    def _on_transport_changed(self, info: TransportInfo, changed: dict) -> None:
        if "Volume" in changed:
            info.volume = int(changed["Volume"])
            _LOGGER.info("Lautstärke von %s: %d %%", info.name or info.address, round(info.volume * 100 / 127))
            self.listener.on_volume(info, info.volume)
        if "State" in changed:
            old, info.state = info.state, changed["State"]
            _LOGGER.debug("Transport %s: %s -> %s", info.address, old, info.state)
            if info.state == "pending":
                self._spawn(self._acquire(info))
            elif info.state == "idle" and info.fd is not None:
                self._close_transport(info)

    # -- streams that went to another sink ------------------------------------------------

    async def _scan_transports(self) -> None:
        reply = await self._call("/", IFACE_OM, "GetManagedObjects")
        for path, ifaces in reply.body[0].items():
            if IFACE_TRANSPORT in ifaces:
                await self._check_transport(path, unwrap(ifaces[IFACE_TRANSPORT]))

    async def _check_transport(self, path: str, props: dict) -> None:
        """Spot a sender whose audio BlueZ gave to another A2DP sink, and take it over.

        The transport's ``Endpoint`` property names the *remote* SEP, so ownership is
        decided by whether our endpoint got a SetConfiguration for it.
        """
        device = str(props.get("Device", ""))
        if not device.startswith(self.adapter_path + "/"):
            return
        if str(props.get("UUID", "")).lower() != a2dp.A2DP_SINK_UUID:
            return  # e.g. the BT bridge streaming to its speakers
        await asyncio.sleep(FOREIGN_GRACE_S)  # our SetConfiguration arrives alongside the transport
        reply = await self._call("/", IFACE_OM, "GetManagedObjects")
        self._note_sender_codecs(reply.body[0])
        if path in self.transports or IFACE_TRANSPORT not in reply.body[0].get(path, {}):
            return
        address = address_from_path(device)
        if not self.options.is_allowed(address):
            return
        self.foreign[path] = (address, "anderen Empfänger (vermutlich HA-Audiosystem)")
        _LOGGER.warning("Ton von %s geht an einen anderen Bluetooth-Empfänger (vermutlich HA-Audiosystem)", address)
        if self.options.claim_streams:
            await self.claim_device(address, automatic=True)

    async def claim_device(self, address: str, *, automatic: bool = False) -> bool:
        """Get the sender's A2DP stream onto our endpoint.

        BlueZ only lets the owner of the current stream reconfigure it ("Operation not
        permitted" for us), so if configuring directly fails while another sink holds the
        stream, the A2DP link is dropped briefly and we configure it ourselves.
        """
        address = address.upper()
        now = time.monotonic()
        recent = [t for t in self._claims.get(address, []) if now - t < 120]
        if automatic and len(recent) >= 3:
            _LOGGER.error("Übernahme von %s mehrfach fehlgeschlagen – gebe auf (Geräteverwaltung: „Ton übernehmen“)",
                          address)
            return False
        self._claims[address] = [*recent, now]
        try:
            device = await self._device_path(address)
        except KeyError:
            return False
        if await self._configure_from_remote(device, address, quiet=automatic):
            return True
        if automatic or not self._foreign_paths(address):
            # Dropping the link loses the sender's endpoints on HA OS and the sender then
            # disconnects completely, so only do that when asked for in the device page.
            return False
        _LOGGER.info("Trenne die Musikverbindung von %s kurz, um den Ton zu übernehmen", address)
        try:
            await self._call(device, IFACE_DEVICE, "DisconnectProfile", "s", [a2dp.A2DP_SOURCE_UUID])
        except DBusError as err:
            _LOGGER.warning("Trennen der Musikverbindung von %s fehlgeschlagen: %s", address, err)
            return False
        for _ in range(30):  # until the other sink's transport is gone
            if not self._foreign_paths(address):
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.3)
        return await self._configure_from_remote(device, address, quiet=False)

    def _foreign_paths(self, address: str) -> list[str]:
        return [p for p, (a, _) in self.foreign.items() if a == address.upper()]

    async def _configure_from_remote(self, device: str, address: str, *, quiet: bool) -> bool:
        """Configure one of the sender's source SEPs with our endpoint (codec order AAC, SBC)."""
        log = _LOGGER.debug if quiet else _LOGGER.warning
        reply = await self._call("/", IFACE_OM, "GetManagedObjects")
        sources = {
            path: unwrap(ifaces[IFACE_ENDPOINT])
            for path, ifaces in reply.body[0].items()
            if path.startswith(device + "/") and IFACE_ENDPOINT in ifaces
            and str(unwrap(ifaces[IFACE_ENDPOINT]).get("UUID", "")).lower() == a2dp.A2DP_SOURCE_UUID
        }
        if not sources:
            log("Kann den Ton von %s nicht übernehmen: BlueZ zeigt keine Codec-Endpunkte des Geräts. "
                "Wiedergabe am Gerät neu starten oder Gerät neu koppeln.", address)
            return False
        ours = {ep.codec: path for path, ep in self._endpoints}
        order = [a2dp.CODEC_AAC, a2dp.CODEC_SBC] if self.options.enable_aac else [a2dp.CODEC_SBC]
        for codec in order:
            name = "AAC" if codec == a2dp.CODEC_AAC else "SBC"
            for sep_path, sep in sources.items():
                if int(sep.get("Codec", -1)) != codec or codec not in ours:
                    continue
                caps = bytes(sep.get("Capabilities", b""))
                try:
                    cfg = a2dp.select_aac_config(caps) if codec == a2dp.CODEC_AAC else a2dp.select_sbc_config(caps)
                except ValueError as err:
                    _LOGGER.debug("%s von %s unpassend: %s", name, address, err)
                    continue
                try:
                    await self._call(sep_path, IFACE_ENDPOINT, "SetConfiguration", "oa{sv}",
                                     [ours[codec], {"Capabilities": Variant("ay", cfg)}])
                except DBusError as err:
                    log("Übernahme von %s (%s) fehlgeschlagen: %s", address, name, err)
                    continue
                _LOGGER.info("Ton von %s übernommen (%s)", address, name)
                return True
        return False

    # -- audio I/O ----------------------------------------------------------------------

    async def _acquire(self, info: TransportInfo) -> None:
        if info.fd is not None:
            return
        try:
            reply = await self._call(info.path, IFACE_TRANSPORT, "TryAcquire")
        except DBusError as err:
            _LOGGER.debug("TryAcquire fehlgeschlagen (%s), versuche Acquire", err)
            try:
                reply = await self._call(info.path, IFACE_TRANSPORT, "Acquire")
            except DBusError as err2:
                _LOGGER.warning("Audio-Kanal von %s konnte nicht übernommen werden: %s", info.address, err2)
                return
        fd_index, read_mtu, _write_mtu = reply.body
        fd = reply.unix_fds[fd_index]
        os.set_blocking(fd, False)
        info.fd, info.read_mtu = fd, int(read_mtu)
        info.stats = {"packets": 0, "since": time.monotonic()}
        self._loop.add_reader(fd, self._read_ready, info)
        _LOGGER.info("Wiedergabe von %s gestartet (%s)", info.name or info.address, info.config.description)
        await self.push_delay(info)
        self.listener.on_stream_started(info)
        if info.volume is not None:
            self.listener.on_volume(info, info.volume)

    def _read_ready(self, info: TransportInfo) -> None:
        fd = info.fd
        if fd is None:
            return
        while True:
            try:
                data = os.read(fd, max(info.read_mtu, 1024) + 64)
            except BlockingIOError:
                return
            except OSError as err:
                _LOGGER.info("Audio-Kanal von %s geschlossen: %s", info.address, err)
                self._close_transport(info)
                return
            if not data:
                self._close_transport(info)
                return
            info.stats["packets"] = info.stats.get("packets", 0) + 1
            self.listener.on_packet(info, data, time.monotonic_ns())

    def _close_transport(self, info: TransportInfo, *, release: bool = True) -> None:
        if info.fd is None:
            return
        fd, info.fd = info.fd, None
        try:
            self._loop.remove_reader(fd)
        except (ValueError, OSError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        _LOGGER.info("Wiedergabe von %s beendet", info.name or info.address)
        self.listener.on_stream_stopped(info)
        if release and info.path in self.transports:
            self._spawn(self._release(info))

    async def _release(self, info: TransportInfo) -> None:
        try:
            await self._call(info.path, IFACE_TRANSPORT, "Release")
        except (DBusError, EOFError, OSError) as err:
            _LOGGER.debug("Release %s: %r", info.path, err)

    def active_transport(self) -> TransportInfo | None:
        live = [t for t in self.transports.values() if t.fd is not None]
        return live[-1] if live else None

