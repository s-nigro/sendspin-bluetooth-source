"""A2DP sink codec handling: capabilities, configuration parsing, RTP depacketizing, decoding.

Everything here is pure (no D-Bus), so it can be unit tested.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import av
import numpy as np

_LOGGER = logging.getLogger(__name__)

A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"
A2DP_SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"

CODEC_SBC = 0x00
CODEC_AAC = 0x02

OUTPUT_RATE = 48_000
OUTPUT_CHANNELS = 2

# --- SBC (A2DP spec 4.3.2) -------------------------------------------------
_SBC_FREQ = {0x80: 16000, 0x40: 32000, 0x20: 44100, 0x10: 48000}
_SBC_MODE = {0x08: "mono", 0x04: "dual", 0x02: "stereo", 0x01: "joint"}
_SBC_BLOCKS = {0x80: 4, 0x40: 8, 0x20: 12, 0x10: 16}
_SBC_SUBBANDS = {0x08: 4, 0x04: 8}
_SBC_ALLOC = {0x02: "snr", 0x01: "loudness"}

SBC_MIN_BITPOOL = 2
SBC_MAX_BITPOOL = 53
# Every rate/mode/block/subband/allocation; bitpool 2..53.
SBC_CAPABILITIES = bytes([0xFF, 0xFF, SBC_MIN_BITPOOL, SBC_MAX_BITPOOL])

# --- AAC (A2DP spec 4.5.2) -------------------------------------------------
_AAC_OBJ_MPEG2_LC = 0x80
_AAC_OBJ_MPEG4_LC = 0x40
_AAC_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 64000, 88200, 96000)
AAC_MAX_BITRATE = 320_000


def _aac_capabilities() -> bytes:
    rates_mask = 0
    for rate in (44100, 48000):
        rates_mask |= 1 << (11 - _AAC_RATES.index(rate))
    byte0 = _AAC_OBJ_MPEG2_LC | _AAC_OBJ_MPEG4_LC
    byte1 = (rates_mask >> 4) & 0xFF
    byte2 = ((rates_mask & 0x0F) << 4) | 0x08 | 0x04  # mono + stereo
    vbr = 0x80
    br = AAC_MAX_BITRATE
    return bytes([byte0, byte1, byte2, vbr | ((br >> 16) & 0x7F), (br >> 8) & 0xFF, br & 0xFF])


AAC_CAPABILITIES = _aac_capabilities()


@dataclass(frozen=True)
class StreamConfig:
    """Negotiated A2DP stream configuration."""

    codec: int
    sample_rate: int
    channels: int
    description: str


def _single_bit(value: int, table: dict[int, object], what: str) -> int:
    bits = [b for b in table if value & b]
    if len(bits) != 1:
        raise ValueError(f"{what}: expected exactly one bit set, got 0x{value:02x}")
    return bits[0]


def parse_sbc_config(cfg: bytes) -> StreamConfig:
    if len(cfg) != 4:
        raise ValueError(f"SBC configuration must be 4 bytes, got {len(cfg)}")
    freq = _SBC_FREQ[_single_bit(cfg[0] & 0xF0, _SBC_FREQ, "SBC rate")]
    mode = _SBC_MODE[_single_bit(cfg[0] & 0x0F, _SBC_MODE, "SBC channel mode")]
    blocks = _SBC_BLOCKS[_single_bit(cfg[1] & 0xF0, _SBC_BLOCKS, "SBC blocks")]
    subbands = _SBC_SUBBANDS[_single_bit(cfg[1] & 0x0C, _SBC_SUBBANDS, "SBC subbands")]
    alloc = _SBC_ALLOC[_single_bit(cfg[1] & 0x03, _SBC_ALLOC, "SBC allocation")]
    min_bp, max_bp = cfg[2], cfg[3]
    if not (SBC_MIN_BITPOOL <= min_bp <= max_bp):
        raise ValueError(f"SBC bitpool {min_bp}..{max_bp} invalid")
    channels = 1 if mode == "mono" else 2
    desc = f"SBC {freq} Hz {mode} b{blocks} s{subbands} {alloc} bitpool {min_bp}-{max_bp}"
    return StreamConfig(CODEC_SBC, freq, channels, desc)


def parse_aac_config(cfg: bytes) -> StreamConfig:
    if len(cfg) != 6:
        raise ValueError(f"AAC configuration must be 6 bytes, got {len(cfg)}")
    obj = cfg[0]
    if not obj & (_AAC_OBJ_MPEG2_LC | _AAC_OBJ_MPEG4_LC):
        raise ValueError(f"AAC object type 0x{obj:02x} not supported")
    rates_mask = (cfg[1] << 4) | (cfg[2] >> 4)
    rates = [r for i, r in enumerate(_AAC_RATES) if rates_mask & (1 << (11 - i))]
    if len(rates) != 1:
        raise ValueError(f"AAC rate mask 0x{rates_mask:03x} invalid")
    ch_bits = cfg[2] & 0x0C
    if ch_bits == 0x08:
        channels = 1
    elif ch_bits == 0x04:
        channels = 2
    else:
        raise ValueError(f"AAC channel bits 0x{ch_bits:02x} invalid")
    vbr = bool(cfg[3] & 0x80)
    bitrate = ((cfg[3] & 0x7F) << 16) | (cfg[4] << 8) | cfg[5]
    desc = f"AAC {rates[0]} Hz {channels}ch {'VBR' if vbr else 'CBR'} {bitrate // 1000} kbit/s"
    return StreamConfig(CODEC_AAC, rates[0], channels, desc)


def parse_config(codec: int, cfg: bytes) -> StreamConfig:
    if codec == CODEC_SBC:
        return parse_sbc_config(cfg)
    if codec == CODEC_AAC:
        return parse_aac_config(cfg)
    raise ValueError(f"Unsupported codec {codec}")


def _pick(value: int, preference: list[int]) -> int:
    for bit in preference:
        if value & bit:
            return bit
    raise ValueError("no common capability")


def select_sbc_config(caps: bytes) -> bytes:
    """Choose a configuration from the remote's SBC capabilities (we initiate)."""
    freq = _pick(caps[0] & 0xF0, [0x10, 0x20, 0x40, 0x80])
    mode = _pick(caps[0] & 0x0F, [0x01, 0x02, 0x04, 0x08])
    blocks = _pick(caps[1] & 0xF0, [0x10, 0x20, 0x40, 0x80])
    subbands = _pick(caps[1] & 0x0C, [0x04, 0x08])
    alloc = _pick(caps[1] & 0x03, [0x01, 0x02])
    min_bp = max(SBC_MIN_BITPOOL, caps[2])
    max_bp = min(SBC_MAX_BITPOOL, caps[3])
    if min_bp > max_bp:
        raise ValueError("no common SBC bitpool")
    return bytes([freq | mode, blocks | subbands | alloc, min_bp, max_bp])


def select_aac_config(caps: bytes) -> bytes:
    obj = _pick(caps[0], [_AAC_OBJ_MPEG2_LC, _AAC_OBJ_MPEG4_LC])
    rates_mask = (caps[1] << 4) | (caps[2] >> 4)
    for rate in (48000, 44100):
        bit = 1 << (11 - _AAC_RATES.index(rate))
        if rates_mask & bit:
            break
    else:
        raise ValueError("no common AAC rate")
    ch = 0x04 if caps[2] & 0x04 else 0x08
    br = min(AAC_MAX_BITRATE, ((caps[3] & 0x7F) << 16) | (caps[4] << 8) | caps[5]) or AAC_MAX_BITRATE
    vbr = caps[3] & 0x80
    return bytes(
        [obj, (bit >> 4) & 0xFF, ((bit & 0x0F) << 4) | ch, vbr | ((br >> 16) & 0x7F), (br >> 8) & 0xFF, br & 0xFF]
    )


# --- RTP --------------------------------------------------------------------


@dataclass(frozen=True)
class RtpPacket:
    sequence: int
    timestamp: int
    marker: bool
    payload: bytes


def parse_rtp(data: bytes) -> RtpPacket:
    if len(data) < 12:
        raise ValueError("RTP packet too short")
    b0, b1 = data[0], data[1]
    if b0 >> 6 != 2:
        raise ValueError("not RTP version 2")
    padding = bool(b0 & 0x20)
    extension = bool(b0 & 0x10)
    csrc = b0 & 0x0F
    offset = 12 + 4 * csrc
    if extension:
        if len(data) < offset + 4:
            raise ValueError("RTP extension truncated")
        ext_len = int.from_bytes(data[offset + 2 : offset + 4], "big")
        offset += 4 + 4 * ext_len
    end = len(data)
    if padding:
        end -= data[-1]
    if offset > end:
        raise ValueError("RTP header longer than packet")
    return RtpPacket(
        sequence=int.from_bytes(data[2:4], "big"),
        timestamp=int.from_bytes(data[4:8], "big"),
        marker=bool(b1 & 0x80),
        payload=data[offset:end],
    )


def loas_wrap(audio_mux_element: bytes) -> bytes:
    """Prefix a LATM AudioMuxElement with a LOAS header, the framing FFmpeg's decoder expects."""
    length = len(audio_mux_element)
    if length >= 1 << 13:
        raise ValueError("LATM frame too large for LOAS")
    header = (0x2B7 << 13) | length
    return header.to_bytes(3, "big") + audio_mux_element


# --- Decoding ---------------------------------------------------------------


class A2dpDecoder:
    """Turn A2DP RTP packets into 48 kHz s16 stereo interleaved PCM."""

    def __init__(self, config: StreamConfig) -> None:
        self.config = config
        if config.codec == CODEC_SBC:
            self._ctx = av.CodecContext.create("sbc", "r")
        elif config.codec == CODEC_AAC:
            self._ctx = av.CodecContext.create("aac_latm", "r")
        else:
            raise ValueError(f"Unsupported codec {config.codec}")
        self._resampler = av.AudioResampler(format="s16", layout="stereo", rate=OUTPUT_RATE)
        self._last_seq: int | None = None
        self._pending_ts: int | None = None
        self._pending = b""
        self.lost_packets = 0
        self.decode_errors = 0

    def feed(self, raw: bytes) -> bytes:
        """Decode one packet read from the transport; returns PCM (may be empty)."""
        try:
            pkt = parse_rtp(raw)
        except ValueError:
            self.decode_errors += 1
            return b""
        if self._last_seq is not None:
            gap = (pkt.sequence - self._last_seq - 1) & 0xFFFF
            if 0 < gap < 1000:
                self.lost_packets += gap
        self._last_seq = pkt.sequence
        if self.config.codec == CODEC_SBC:
            return self._decode_sbc(pkt.payload)
        return self._decode_aac(pkt)

    def _decode_sbc(self, payload: bytes) -> bytes:
        if len(payload) < 2:
            return b""
        header = payload[0]
        if header & 0x80:  # fragmented SBC frame, very rare; not supported
            self.decode_errors += 1
            return b""
        return self._decode_bytes(payload[1:], parse=True)

    def _decode_aac(self, pkt: RtpPacket) -> bytes:
        # One AudioMuxElement per packet in practice; join fragments sharing a timestamp.
        if self._pending and pkt.timestamp == self._pending_ts:
            self._pending += pkt.payload
        else:
            self._pending = pkt.payload
            self._pending_ts = pkt.timestamp
        try:
            data = loas_wrap(self._pending)
        except ValueError:
            self._pending = b""
            self.decode_errors += 1
            return b""
        out = self._decode_bytes(data, parse=False, quiet=True)
        if out or pkt.marker or len(self._pending) > 4096:
            self._pending = b""
        return out

    def _decode_bytes(self, data: bytes, *, parse: bool, quiet: bool = False) -> bytes:
        try:
            packets = self._ctx.parse(data) if parse else [av.Packet(data)]
            chunks: list[bytes] = []
            for packet in packets:
                for frame in self._ctx.decode(packet):
                    chunks.extend(self._resample(frame))
            return b"".join(chunks)
        except av.error.FFmpegError as err:
            if not quiet:
                _LOGGER.debug("Decode error: %s", err)
            self.decode_errors += 1
            return b""

    def _resample(self, frame: av.AudioFrame) -> list[bytes]:
        frame.pts = None
        return [bytes(f.planes[0])[: f.samples * 4] for f in self._resampler.resample(frame)]


# --- PCM helpers ------------------------------------------------------------


def apply_gain(pcm: bytes, gain: float) -> bytes:
    if gain >= 0.999 or not pcm:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16)
    return (samples.astype(np.float32) * gain).astype(np.int16).tobytes()


def a2dp_volume_to_gain(volume: int) -> float:
    """A2DP absolute volume 0..127 to a linear gain (perceptual curve)."""
    v = max(0, min(127, volume)) / 127.0
    return v**2


def peak_level(pcm: bytes) -> int:
    if not pcm:
        return 0
    samples = np.frombuffer(pcm, dtype=np.int16)
    return int(np.max(np.abs(samples.astype(np.int32))))


VENDOR_CODECS = {
    (0x004F, 0x0001): "aptX",
    (0x00D7, 0x0024): "aptX HD",
    (0x000A, 0x0002): "aptX LL",
    (0x012D, 0x00AA): "LDAC",
    (0x0075, 0x0102): "Samsung Scalable",
    (0x003A, 0x0001): "Opus",
}


def codec_name(codec: int, capabilities: bytes = b"") -> str:
    """Human name of an A2DP codec (vendor codecs are identified by vendor and codec ID)."""
    if codec == CODEC_SBC:
        return "SBC"
    if codec == CODEC_AAC:
        return "AAC"
    if codec == 0x01:
        return "MP3"
    if codec == 0xFF and len(capabilities) >= 6:
        vendor = int.from_bytes(capabilities[0:4], "little")
        cid = int.from_bytes(capabilities[4:6], "little")
        return VENDOR_CODECS.get((vendor, cid), f"Hersteller-Codec {vendor:04x}:{cid:04x}")
    return f"Codec {codec}"
