"""BluezManager transport flow with a fake BlueZ: configure -> acquire -> packets -> idle."""

import asyncio
import socket
from types import SimpleNamespace

import pytest
from dbus_fast import Message, MessageType, Variant

from btsource import a2dp, bluez
from btsource.config import Options
from test_a2dp import _encode_sbc_packets

DEV = "/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF"
TRANSPORT = DEV + "/sep1/fd0"


class Recorder(bluez.StreamListener):
    def __init__(self):
        self.started = self.stopped = 0
        self.packets = []
        self.volume = None

    def on_stream_started(self, t):
        self.started += 1

    def on_packet(self, t, data, ns):
        self.packets.append(data)

    def on_stream_stopped(self, t):
        self.stopped += 1

    def on_volume(self, t, v):
        self.volume = v


@pytest.mark.asyncio
async def test_transport_lifecycle():
    rec = Recorder()
    mgr = bluez.BluezManager(Options(delay_report_ms=850), rec)
    mgr.adapter_path = "/org/bluez/hci1"
    ours, remote = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)  # packet boundaries like L2CAP
    for sock in (ours, remote):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
    calls = []

    async def fake_call(path, iface, member, signature="", body=None):
        calls.append((path, iface, member, body))
        if member == "Get":
            name = body[1]
            return SimpleNamespace(body=[Variant("s", "Beamer") if name == "Alias" else Variant("b", True)])
        if member == "TryAcquire":
            return SimpleNamespace(body=[0, 895, 895], unix_fds=[ours.detach()])
        return SimpleNamespace(body=[], unix_fds=[])

    mgr._call = fake_call

    cfg = bytes([0x11, 0x15, 2, 53])  # 48 kHz joint, 16 blocks, 8 subbands, loudness
    mgr.on_set_configuration(TRANSPORT, DEV, "AA:BB:CC:DD:EE:FF", a2dp.parse_sbc_config(cfg), {"State": "idle"})
    await asyncio.sleep(0.05)
    assert any(c[2] == "Set" and c[3][1] == "Delay" and c[3][2].value == 8500 for c in calls)

    # remote starts streaming: State -> pending, then packets arrive
    sig = Message(
        message_type=MessageType.SIGNAL, path=TRANSPORT, interface=bluez.IFACE_PROPS,
        member="PropertiesChanged", signature="sa{sv}as",
        body=[bluez.IFACE_TRANSPORT, {"State": Variant("s", "pending"), "Volume": Variant("q", 100)}, []],
    )
    mgr._on_message(sig)
    await asyncio.sleep(0.05)
    assert rec.started == 1 and rec.volume == 100
    for p in _encode_sbc_packets(48000)[:10]:
        remote.send(p)
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.1)
    assert len(rec.packets) == 10

    # remote pauses -> idle -> fd closed
    mgr._on_message(Message(
        message_type=MessageType.SIGNAL, path=TRANSPORT, interface=bluez.IFACE_PROPS,
        member="PropertiesChanged", signature="sa{sv}as",
        body=[bluez.IFACE_TRANSPORT, {"State": Variant("s", "idle")}, []],
    ))
    await asyncio.sleep(0.05)
    assert rec.stopped == 1 and mgr.transports[TRANSPORT].fd is None

    # disconnect
    mgr.on_clear_configuration(TRANSPORT)
    assert TRANSPORT not in mgr.transports
    remote.close()


def test_endpoint_rejects_disallowed_device():
    mgr = SimpleNamespace(options=Options(allowed_devices=["11:22:33:44:55:66"]))
    ep = bluez.SinkEndpoint(mgr, a2dp.CODEC_SBC, a2dp.SBC_CAPABILITIES)
    from dbus_fast import DBusError
    fn = bluez.SinkEndpoint.SetConfiguration
    fn = getattr(fn, "__wrapped__", fn)
    with pytest.raises(DBusError):
        fn(ep, TRANSPORT, {"Device": Variant("o", DEV), "Configuration": Variant("ay", bytes([0x11, 0x15, 2, 53]))})
