"""remove_devices: unpair listed MACs on start, list the rest."""

from types import SimpleNamespace

import pytest
from dbus_fast import Variant

from btsource import bluez
from btsource.config import Options

ADAPTER = "/org/bluez/hci1"


def _dev(addr, alias, paired=True):
    return {
        bluez.IFACE_DEVICE: {
            "Address": Variant("s", addr),
            "Alias": Variant("s", alias),
            "Paired": Variant("b", paired),
        }
    }


@pytest.mark.asyncio
async def test_remove_listed_devices_only():
    objects = {
        ADAPTER: {bluez.IFACE_ADAPTER: {}},
        ADAPTER + "/dev_CC_27_46_AC_A9_21": _dev("CC:27:46:AC:A9:21", "iPhone"),
        ADAPTER + "/dev_11_22_33_44_55_66": _dev("11:22:33:44:55:66", "Beamer"),
        "/org/bluez/hci0/dev_CC_27_46_AC_A9_21": _dev("CC:27:46:AC:A9:21", "iPhone"),  # other adapter
    }
    mgr = bluez.BluezManager(Options(remove_devices=["cc:27:46:ac:a9:21", "AA:AA:AA:AA:AA:AA"]), None)
    mgr.options.validate()
    mgr.adapter_path = ADAPTER
    calls = []

    async def fake_call(path, iface, member, signature="", body=None):
        calls.append((path, member, body))
        if member == "GetManagedObjects":
            return SimpleNamespace(body=[objects])
        return SimpleNamespace(body=[])

    mgr._call = fake_call
    await mgr._forget_and_list_devices()
    removed = [c for c in calls if c[1] == "RemoveDevice"]
    assert removed == [(ADAPTER, "RemoveDevice", [ADAPTER + "/dev_CC_27_46_AC_A9_21"])]


def test_remove_devices_validation():
    with pytest.raises(ValueError):
        Options(remove_devices=["kein-mac"]).validate()


def test_server_candidates():
    from btsource.sendspin_link import server_candidates

    assert server_candidates("") == []
    assert server_candidates("ws://x:1/s") == ["ws://x:1/s"]
    assert server_candidates("auto", "192.168.1.5") == [
        "ws://127.0.0.1:8927/sendspin",
        "ws://192.168.1.5:8927/sendspin",
    ]
    assert server_candidates("AUTO", None) == ["ws://127.0.0.1:8927/sendspin"]
