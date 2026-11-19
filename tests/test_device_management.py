"""Device management: store, BlueZ operations with a fake bus, and the web API."""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from dbus_fast import Variant

from btsource import a2dp, bluez
from btsource.config import Options
from btsource.devices import DeviceStore
from btsource.webui import WebUI

ADAPTER = "/org/bluez/hci1"
PHONE = "CC:27:46:AC:A9:21"
SPEAKER = "F8:5C:7D:B3:39:85"
PHONE_PATH = ADAPTER + "/dev_CC_27_46_AC_A9_21"
SPEAKER_PATH = ADAPTER + "/dev_F8_5C_7D_B3_39_85"


def test_store_persists_and_defaults(tmp_path):
    path = tmp_path / "devices.json"
    store = DeviceStore(path, 1000)
    assert store.delay_for(PHONE) == 1000
    store.remember(PHONE, "iPhone")
    assert store.set_delay(PHONE.lower(), 1250) == 1250
    assert store.set_delay("AA:BB:CC:DD:EE:FF", 99999) == 3000  # clamped
    again = DeviceStore(path, 800)
    assert again.delay_for(PHONE) == 1250
    assert again.known(PHONE)
    assert again.set_delay(PHONE, None) == 800  # back to the add-on default
    again.forget(PHONE)
    assert not DeviceStore(path, 800).known(PHONE)


def _objects():
    return {
        ADAPTER: {bluez.IFACE_ADAPTER: {}},
        PHONE_PATH: {bluez.IFACE_DEVICE: {
            "Address": Variant("s", PHONE), "Alias": Variant("s", "iPhone"),
            "Paired": Variant("b", True), "Trusted": Variant("b", True), "Connected": Variant("b", True),
            "UUIDs": Variant("as", [a2dp.A2DP_SOURCE_UUID]),
        }},
        SPEAKER_PATH: {bluez.IFACE_DEVICE: {
            "Address": Variant("s", SPEAKER), "Alias": Variant("s", "JBL Charge 5"),
            "Paired": Variant("b", True), "Trusted": Variant("b", True), "Connected": Variant("b", False),
            "UUIDs": Variant("as", [a2dp.A2DP_SINK_UUID]),
        }},
        ADAPTER + "/dev_11_11_11_11_11_11": {bluez.IFACE_DEVICE: {  # only discovered nearby
            "Address": Variant("s", "11:11:11:11:11:11"), "Paired": Variant("b", False),
        }},
    }


@pytest_asyncio.fixture
async def manager(tmp_path):
    opts = Options(discoverable=True, discoverable_timeout=5)
    mgr = bluez.BluezManager(opts, bluez.StreamListener(), DeviceStore(tmp_path / "d.json", 1000))
    mgr.adapter_path = ADAPTER
    mgr.adapter_address = "8C:68:AB:20:59:62"
    mgr.calls = []

    async def fake_call(path, iface, member, signature="", body=None):
        mgr.calls.append((path, iface, member, body))
        if member == "GetManagedObjects":
            return SimpleNamespace(body=[_objects()])
        return SimpleNamespace(body=[], unix_fds=[])

    mgr._call = fake_call
    return mgr


@pytest.mark.asyncio
async def test_list_devices_separates_sources(manager):
    devices = await manager.list_devices()
    assert [d["address"] for d in devices] == [PHONE, SPEAKER]
    phone, speaker = devices
    assert phone["source"] and phone["connected"] and phone["delay_ms"] == 1000
    assert not speaker["source"]


@pytest.mark.asyncio
async def test_remove_disconnect_and_live_delay(manager):
    cfg = a2dp.parse_sbc_config(bytes([0x11, 0x15, 2, 53]))
    manager.transports["/t"] = bluez.TransportInfo("/t", PHONE_PATH, PHONE, cfg, name="iPhone")
    assert await manager.set_device_delay(PHONE, 1400) == 1400
    sets = [c for c in manager.calls if c[2] == "Set" and c[3][1] == "Delay"]
    assert sets and sets[-1][3][2].value == 14000  # pushed right away, 1/10 ms units

    await manager.disconnect_device(PHONE)
    assert (PHONE_PATH, bluez.IFACE_DEVICE, "Disconnect", None) in manager.calls

    await manager.remove_device(PHONE.lower())
    assert (ADAPTER, bluez.IFACE_ADAPTER, "RemoveDevice", [PHONE_PATH]) in manager.calls
    assert not manager.store.known(PHONE)


@pytest.mark.asyncio
async def test_pairing_window(manager):
    manager.pairing_open = False
    await manager.open_pairing(120)
    assert manager.pairing_open and manager.pairing_until is not None
    await manager.close_pairing()
    assert not manager.pairing_open and manager.pairing_until is None


class FakeLink:
    ma_connected = True
    streaming = False
    signal_present = True


@pytest.mark.asyncio
async def test_web_api(manager):
    ui = WebUI(manager, FakeLink(), 0, allow_local=True)
    async with TestClient(TestServer(ui.app)) as client:
        page = await client.get("/")
        assert page.status == 200 and "Bluetooth-Quelle" in await page.text()

        state = await (await client.get("/api/state")).json()
        assert state["music_assistant"]["connected"] is True
        assert state["devices"][0]["address"] == PHONE

        r = await client.post(f"/api/devices/{PHONE}/delay", json={"delay_ms": 1300})
        assert (await r.json())["delay_ms"] == 1300
        assert (await client.post(f"/api/devices/{PHONE}/delay", json={"delay_ms": 5000})).status == 400
        assert (await client.post("/api/devices/nope/remove", json={})).status == 400
        assert (await client.post(f"/api/devices/{PHONE}/remove", json={})).status == 200
        assert (await client.post("/api/pairing", json={"seconds": 180})).status == 200
        assert manager.pairing_open


@pytest.mark.asyncio
async def test_web_rejects_lan_clients(manager):
    ui = WebUI(manager, FakeLink(), 0)  # only the ingress proxy is allowed
    async with TestClient(TestServer(ui.app)) as client:
        assert (await client.get("/api/state")).status == 403
