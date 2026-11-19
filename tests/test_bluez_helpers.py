from btsource.bluez import address_from_path, PairingAgent, AUDIO_UUIDS
from btsource.config import Options
from dbus_fast import DBusError
import pytest


def test_address_from_path():
    assert address_from_path("/org/bluez/hci1/dev_11_22_33_AA_BB_CC/sep1/fd2") == "11:22:33:AA:BB:CC"


class _M:
    def __init__(self, open_, allowed=()):
        self.options = Options(allowed_devices=list(allowed))
        self.pairing_open = open_
        self.trusted = []

    def schedule_trust(self, path):
        self.trusted.append(path)


DEV = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"


def test_agent_pairing_window_and_allowlist():
    PairingAgent(_M(open_=True))._check(DEV, pairing=True)
    m = _M(open_=False)
    with pytest.raises(DBusError):
        PairingAgent(m)._check(DEV, pairing=True)
    PairingAgent(m)._check(DEV, pairing=False)  # reconnect of paired device still ok
    m2 = _M(open_=True, allowed=["11:22:33:44:55:66"])
    with pytest.raises(DBusError):
        PairingAgent(m2)._check(DEV, pairing=True)


def test_audio_uuids_lowercase():
    assert all(u == u.lower() for u in AUDIO_UUIDS)
