"""Link Band 2.0 BLE protocol: characteristic UUIDs and payload decoders.

Byte layouts mirror the vendor SDK exactly:
  EEG  link_band_sdk/python_core/app/core/device.py:609-666
  PPG  link_band_sdk/python_core/app/core/device.py:700-736
  ACC  link_band_sdk/python_core/app/core/device.py:767-801
Nothing here is inferred; anything the SDK does not document is marked UNKNOWN
in NOTES.md instead of being guessed at.
"""

EEG_CHAR = "00ab4d15-66b4-0d8a-824f-8d6f8966c6e5"
PPG_CHAR = "6c739642-23ba-818b-2045-bfe8970263f6"
ACC_CHAR = "d3d46a35-4394-e9aa-5a43-e7921120aaed"
BATTERY_CHAR = "00002a19-0000-1000-8000-00805f9b34fb"
# Only the vendor Tk GUI ever wrote to this one (original_gui_sdk.py:315).
EEG_WRITE_CHAR = "0065cacb-9e52-21bf-a849-99a80d83830e"

STREAMS = ("eeg", "ppg", "acc")
CHARS = {"eeg": EEG_CHAR, "ppg": PPG_CHAR, "acc": ACC_CHAR}

DEVICE_CLOCK_HZ = 32768.0
RATES = {"eeg": 250.0, "ppg": 50.0, "acc": 30.0}

# device.py:653. 4.033 V over 12 bits of gain across a signed 24-bit ADC.
EEG_LSB_UV = 4.033 / 12 / (2**23 - 1) * 1e6

# Payload bytes per sample frame, after the shared 4-byte packet timestamp.
FRAME_BYTES = {"eeg": 7, "ppg": 6, "acc": 6}

# Smallest MTU that carries a full-size notification. Measured payloads are
# 179 B (EEG), 172 B (PPG), 184 B (ACC); ATT overhead is 3 bytes.
MIN_USEFUL_MTU = 187


class ProtoError(ValueError):
    """Payload does not match the documented layout. Never decoded around."""


def _split(payload, stream):
    size = FRAME_BYTES[stream]
    if len(payload) < 4:
        raise ProtoError(f"{stream}: {len(payload)} B, need >=4 for the packet timestamp")
    body = len(payload) - 4
    if body % size:
        raise ProtoError(
            f"{stream}: {len(payload)} B, {body} B after the timestamp is not a multiple of {size}"
        )
    return int.from_bytes(payload[0:4], "little"), 4, body // size


def _s24(hi, mid, lo):
    v = (hi << 16) | (mid << 8) | lo
    return v - 0x1000000 if v & 0x800000 else v


def decode_eeg(payload):
    """-> (device_ts_raw, [ {leadoff_ch1, leadoff_ch2, ch1_raw, ch2_raw} ])"""
    ts, off, n = _split(payload, "eeg")
    out = []
    for i in range(n):
        b = off + i * 7
        lead = payload[b]
        out.append(
            {
                "leadoff_ch1": bool(lead & 0x01),
                "leadoff_ch2": bool(lead & 0x04),
                "ch1_raw": _s24(payload[b + 1], payload[b + 2], payload[b + 3]),
                "ch2_raw": _s24(payload[b + 4], payload[b + 5], payload[b + 6]),
            }
        )
    return ts, out


def decode_ppg(payload):
    """-> (device_ts_raw, [ {red_raw, ir_raw} ]). 24-bit unsigned counts."""
    ts, off, n = _split(payload, "ppg")
    out = []
    for i in range(n):
        b = off + i * 6
        out.append(
            {
                "red_raw": (payload[b] << 16) | (payload[b + 1] << 8) | payload[b + 2],
                "ir_raw": (payload[b + 3] << 16) | (payload[b + 4] << 8) | payload[b + 5],
            }
        )
    return ts, out


def decode_acc(payload):
    """-> (device_ts_raw, [ {x_raw, y_raw, z_raw} ]). int16 LE, units UNKNOWN."""
    ts, off, n = _split(payload, "acc")
    out = []
    for i in range(n):
        b = off + i * 6
        out.append(
            {
                "x_raw": int.from_bytes(payload[b : b + 2], "little", signed=True),
                "y_raw": int.from_bytes(payload[b + 2 : b + 4], "little", signed=True),
                "z_raw": int.from_bytes(payload[b + 4 : b + 6], "little", signed=True),
            }
        )
    return ts, out


DECODERS = {"eeg": decode_eeg, "ppg": decode_ppg, "acc": decode_acc}
