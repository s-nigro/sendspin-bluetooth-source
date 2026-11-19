import struct

from btsource import mgmt


def test_set_dev_class_command_layout():
    cmd = mgmt.build_command(mgmt.MGMT_OP_SET_DEV_CLASS, 1, bytes([0x04, 0x14]))
    assert cmd == bytes([0x0E, 0x00, 0x01, 0x00, 0x02, 0x00, 0x04, 0x14])


def test_parse_reply():
    ok = struct.pack("<HHHHB", mgmt.MGMT_EV_CMD_COMPLETE, 1, 6, mgmt.MGMT_OP_SET_DEV_CLASS, 0) + b"\x00\x00\x00"
    assert mgmt.parse_reply(ok, mgmt.MGMT_OP_SET_DEV_CLASS, 1) == 0
    busy = struct.pack("<HHHHB", mgmt.MGMT_EV_CMD_STATUS, 1, 3, mgmt.MGMT_OP_SET_DEV_CLASS, 0x0A)
    assert mgmt.parse_reply(busy, mgmt.MGMT_OP_SET_DEV_CLASS, 1) == 0x0A
    # other adapter, other opcode, unrelated event
    assert mgmt.parse_reply(ok, mgmt.MGMT_OP_SET_DEV_CLASS, 0) is None
    other = struct.pack("<HHHHB", mgmt.MGMT_EV_CMD_COMPLETE, 1, 3, 0x0005, 0)
    assert mgmt.parse_reply(other, mgmt.MGMT_OP_SET_DEV_CLASS, 1) is None
    assert mgmt.parse_reply(struct.pack("<HHH", 0x0006, 1, 4) + b"\x00" * 4, mgmt.MGMT_OP_SET_DEV_CLASS, 1) is None
