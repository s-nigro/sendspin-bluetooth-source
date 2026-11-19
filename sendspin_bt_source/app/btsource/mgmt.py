"""Minimal client for the Linux Bluetooth management API (what btmgmt uses).

Only Set Device Class is needed. The socket is opened through libc because
Python's socket module cannot bind HCI sockets to the control channel.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import select
import struct
import time

AF_BLUETOOTH = 31
BTPROTO_HCI = 1
SOCK_RAW = 3
SOCK_CLOEXEC = 0o2000000
HCI_DEV_NONE = 0xFFFF
HCI_CHANNEL_CONTROL = 3

MGMT_OP_SET_DEV_CLASS = 0x000E
MGMT_EV_CMD_COMPLETE = 0x0001
MGMT_EV_CMD_STATUS = 0x0002

MGMT_STATUS = {
    0x01: "unknown command",
    0x03: "failed",
    0x0A: "busy",
    0x0C: "not supported",
    0x0D: "invalid parameters",
    0x0F: "not powered",
    0x11: "invalid index",
    0x14: "permission denied",
}


class MgmtError(OSError):
    pass


class _SockaddrHci(ctypes.Structure):
    _fields_ = [("family", ctypes.c_ushort), ("dev", ctypes.c_ushort), ("channel", ctypes.c_ushort)]


def build_command(opcode: int, index: int, params: bytes) -> bytes:
    return struct.pack("<HHH", opcode, index, len(params)) + params


def parse_reply(data: bytes, opcode: int, index: int) -> int | None:
    """Return the status if ``data`` answers ``opcode`` on ``index``, else None."""
    if len(data) < 9:
        return None
    event, ev_index, _length = struct.unpack_from("<HHH", data)
    if event not in (MGMT_EV_CMD_COMPLETE, MGMT_EV_CMD_STATUS) or ev_index != index:
        return None
    ev_opcode, status = struct.unpack_from("<HB", data, 6)
    if ev_opcode != opcode:
        return None
    return status


def _open_control_socket() -> int:
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    fd = libc.socket(AF_BLUETOOTH, SOCK_RAW | SOCK_CLOEXEC, BTPROTO_HCI)
    if fd < 0:
        err = ctypes.get_errno()
        raise MgmtError(err, f"socket: {os.strerror(err)}")
    addr = _SockaddrHci(AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_CONTROL)
    if libc.bind(fd, ctypes.byref(addr), ctypes.sizeof(addr)) < 0:
        err = ctypes.get_errno()
        os.close(fd)
        raise MgmtError(err, f"bind: {os.strerror(err)}")
    return fd


def set_device_class(index: int, major: int, minor: int, timeout: float = 3.0) -> None:
    """Set the adapter's major/minor device class. Blocking; run it in an executor."""
    fd = _open_control_socket()
    try:
        os.write(fd, build_command(MGMT_OP_SET_DEV_CLASS, index, bytes([major, minor])))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MgmtError(errno.ETIMEDOUT, "no reply from the kernel")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            status = parse_reply(os.read(fd, 1024), MGMT_OP_SET_DEV_CLASS, index)
            if status is None:
                continue  # unrelated event on the control channel
            if status:
                raise MgmtError(errno.EIO, MGMT_STATUS.get(status, f"status 0x{status:02x}"))
            return
    finally:
        os.close(fd)
