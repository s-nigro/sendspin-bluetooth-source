"""AudioPipeline: signal detection, gating on the server's start, remote volume."""

import asyncio
import time

import pytest

from btsource import a2dp
from btsource.bluez import TransportInfo
from btsource.config import Options
from btsource.service import AudioPipeline
from test_a2dp import _encode_sbc_packets


class FakeLink:
    def __init__(self):
        self.streaming = False
        self.fed = []
        self.signal = None

    async def feed(self, pcm, captured_ns, now_ns):
        self.fed.append(pcm)

    def set_signal(self, present):
        self.signal = present


def _transport():
    cfg = a2dp.StreamConfig(a2dp.CODEC_SBC, 48000, 2, "SBC test")
    return TransportInfo(path="/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF/fd0",
                         device_path="/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
                         address="AA:BB:CC:DD:EE:FF", config=cfg)


@pytest.mark.asyncio
async def test_signal_and_gating():
    link = FakeLink()
    pipe = AudioPipeline(Options(silence_timeout=20), link)
    pipe.start()
    try:
        t = _transport()
        assert not pipe.signal_present()
        pipe.on_stream_started(t)
        assert not pipe.signal_present()  # connected but nothing audible yet
        packets = _encode_sbc_packets(48000)
        for p in packets[:5]:
            pipe.on_packet(t, p, time.monotonic_ns())
        assert pipe.signal_present()
        assert link.fed == []  # MA has not asked for audio -> nothing sent
        assert pipe.signal_present(now=time.monotonic() + 25) is False  # silence timeout

        link.streaming = True
        for p in packets[5:]:
            pipe.on_packet(t, p, time.monotonic_ns())
        await asyncio.sleep(0.05)
        assert link.fed and sum(len(x) for x in link.fed) > 4 * 4800

        # remote volume halves the level (quadratic curve)
        full = a2dp.peak_level(b"".join(link.fed))
        link.fed.clear()
        pipe.on_volume(t, 90)
        for p in _encode_sbc_packets(48000)[5:]:
            pipe.on_packet(t, p, time.monotonic_ns())
        await asyncio.sleep(0.05)
        reduced = a2dp.peak_level(b"".join(link.fed))
        assert reduced < full * 0.6

        pipe.on_stream_stopped(t)
        assert not pipe.signal_present()
    finally:
        await pipe.stop()


def test_options_validation(tmp_path):
    p = tmp_path / "o.json"
    p.write_text('{"device_name":"  Beamer  ","allowed_devices":["aa:bb:cc:dd:ee:ff"],"delay_report_ms":9000}')
    o = Options.load(p)
    assert o.device_name == "Beamer"
    assert o.allowed_devices == ["AA:BB:CC:DD:EE:FF"]
    assert o.delay_report_ms == 6000
    assert o.is_allowed("aa:bb:cc:dd:ee:ff") and not o.is_allowed("11:22:33:44:55:66")
    p.write_text('{"allowed_devices":["kaputt"]}')
    with pytest.raises(ValueError):
        Options.load(p)
