"""Tests for the pure A2DP helpers: capability blobs, config parsing, RTP and decoding."""

import math
import struct

import av
import numpy as np
import pytest

from btsource import a2dp


def test_capability_blobs():
    assert a2dp.SBC_CAPABILITIES == bytes([0xFF, 0xFF, 2, 53])
    # MPEG-2 + MPEG-4 LC, 44.1k + 48k, mono + stereo, VBR, 320 kbit/s
    assert a2dp.AAC_CAPABILITIES == bytes([0xC0, 0x01, 0x8C, 0x84, 0xE2, 0x00])


def test_parse_sbc_config():
    cfg = a2dp.parse_sbc_config(bytes([0x10 | 0x01, 0x10 | 0x04 | 0x01, 2, 53]))
    assert (cfg.codec, cfg.sample_rate, cfg.channels) == (a2dp.CODEC_SBC, 48000, 2)
    with pytest.raises(ValueError):
        a2dp.parse_sbc_config(bytes([0x30 | 0x01, 0x15, 2, 53]))  # two rates set


def test_parse_aac_config():
    cfg = a2dp.parse_aac_config(bytes([0x80, 0x01, 0x04, 0x83, 0xE8, 0x00]))
    assert (cfg.codec, cfg.sample_rate, cfg.channels) == (a2dp.CODEC_AAC, 44100, 2)
    cfg = a2dp.parse_aac_config(bytes([0x40, 0x00, 0x84, 0x04, 0xE2, 0x00]))
    assert cfg.sample_rate == 48000


def test_select_roundtrip():
    sbc = a2dp.select_sbc_config(a2dp.SBC_CAPABILITIES)
    assert a2dp.parse_sbc_config(sbc).sample_rate == 48000
    aac = a2dp.select_aac_config(a2dp.AAC_CAPABILITIES)
    parsed = a2dp.parse_aac_config(aac)
    assert (parsed.sample_rate, parsed.channels) == (48000, 2)


def test_parse_rtp_with_csrc_and_padding():
    hdr = bytes([0x80 | 0x20 | 0x01, 0x80 | 96]) + struct.pack(">HII", 7, 1234, 1) + b"CSRC"
    pkt = a2dp.parse_rtp(hdr + b"payload" + b"\x00\x00\x03")
    assert (pkt.sequence, pkt.timestamp, pkt.marker, pkt.payload) == (7, 1234, True, b"payload")


def test_loas_wrap():
    out = a2dp.loas_wrap(b"\x01\x02\x03")
    assert out[:3] == ((0x2B7 << 13) | 3).to_bytes(3, "big")


def _sine(rate, seconds=0.5, freq=1000):
    n = int(rate * seconds)
    t = np.arange(n) / rate
    mono = (np.sin(2 * math.pi * freq * t) * 12000).astype(np.int16)
    return np.repeat(mono[:, None], 2, axis=1)


def _encode_sbc_packets(rate):
    enc = av.CodecContext.create("sbc", "w")
    enc.sample_rate = rate
    enc.layout = "stereo"
    enc.format = "s16"
    enc.bit_rate = 328000
    enc.open()
    pcm = _sine(rate)
    frames = []
    step = 1024
    for i in range(0, len(pcm) - step, step):
        f = av.AudioFrame.from_ndarray(pcm[i : i + step].reshape(1, -1), format="s16", layout="stereo")
        f.sample_rate = rate
        for p in enc.encode(f):
            frames.append(bytes(p))
    # A2DP media payload: 1 header byte with frame count, then N SBC frames.
    packets = []
    seq = 0
    for i in range(0, len(frames), 4):
        group = frames[i : i + 4]
        payload = bytes([len(group)]) + b"".join(group)
        rtp = bytes([0x80, 96]) + struct.pack(">HII", seq, seq * 512, 0)
        packets.append(rtp + payload)
        seq += 1
    return packets


@pytest.mark.parametrize("rate", [44100, 48000])
def test_sbc_decode_to_48k_stereo(rate):
    cfg = a2dp.StreamConfig(a2dp.CODEC_SBC, rate, 2, "test")
    dec = a2dp.A2dpDecoder(cfg)
    out = b"".join(dec.feed(p) for p in _encode_sbc_packets(rate))
    samples = np.frombuffer(out, dtype=np.int16).reshape(-1, 2)
    # ~0.5 s of audio at 48 kHz, minus encoder/resampler priming
    assert 18000 < len(samples) <= 24000
    assert dec.decode_errors == 0 and dec.lost_packets == 0
    assert a2dp.peak_level(out) > 8000
    # dominant frequency stays at 1 kHz after resampling
    spec = np.abs(np.fft.rfft(samples[2000:18000, 0]))
    assert abs(np.argmax(spec) * 48000 / 16000 - 1000) < 20


def test_gain_and_volume():
    pcm = np.array([10000, -10000], dtype=np.int16).tobytes()
    out = np.frombuffer(a2dp.apply_gain(pcm, 0.5), dtype=np.int16)
    assert list(out) == [5000, -5000]
    assert a2dp.a2dp_volume_to_gain(127) == 1.0
    assert a2dp.a2dp_volume_to_gain(0) == 0.0


def _encode_aac_latm_packets(rate):
    """AAC-LC in LOAS/LATM with in-band config (like Android's TT_MP4_LATM_MCP1)."""
    import io

    buf = io.BytesIO()
    out = av.open(buf, "w", format="latm")
    stream = out.add_stream("aac", rate=rate, layout="stereo")
    stream.bit_rate = 256000
    pcm = _sine(rate)
    step = 1024
    for i in range(0, len(pcm) - step, step):
        f = av.AudioFrame.from_ndarray(pcm[i : i + step].reshape(1, -1), format="s16", layout="stereo")
        f.sample_rate = rate
        for p in stream.encode(f):
            out.mux(p)
    for p in stream.encode(None):
        out.mux(p)
    out.close()
    data = buf.getvalue()
    packets, pos, seq = [], 0, 0
    while pos + 3 <= len(data):
        hdr = int.from_bytes(data[pos : pos + 3], "big")
        assert hdr >> 13 == 0x2B7
        length = hdr & 0x1FFF
        element = data[pos + 3 : pos + 3 + length]
        pos += 3 + length
        rtp = bytes([0x80, 0x80 | 96]) + struct.pack(">HII", seq, seq * 1024, 0)
        packets.append(rtp + element)  # A2DP AAC: RTP + bare AudioMuxElement
        seq += 1
    return packets


@pytest.mark.parametrize("rate", [44100, 48000])
def test_aac_latm_decode(rate):
    cfg = a2dp.StreamConfig(a2dp.CODEC_AAC, rate, 2, "test")
    dec = a2dp.A2dpDecoder(cfg)
    out = b"".join(dec.feed(p) for p in _encode_aac_latm_packets(rate))
    samples = np.frombuffer(out, dtype=np.int16).reshape(-1, 2)
    assert len(samples) > 18000
    assert a2dp.peak_level(out) > 8000
    spec = np.abs(np.fft.rfft(samples[4000:20000, 0]))
    assert abs(np.argmax(spec) * 48000 / 16000 - 1000) < 20
