"""Two transports behind one interface: real BLE (bleak) and a mock.

The mock reproduces the failure modes measured in the vendor's own recordings,
so `record --mock` exercises the same code paths as the real band.
"""

import asyncio
import random
import time

from . import proto


class TransportError(Exception):
    """Carries a message already fit to print. Never a stack trace."""


class CharacteristicMissing(TransportError):
    pass


def describe_error(exc):
    """Map a bleak/BlueZ failure onto one plain sentence."""
    text = str(exc)
    low = text.lower()
    if isinstance(exc, TransportError):
        return text
    if "no bluetooth adapters" in low or "not ready" in low or "not powered" in low:
        return "Bluetooth is off, or this machine has no usable adapter."
    if "notauthorized" in low or "notpermitted" in low or "permission denied" in low or "operation not permitted" in low:
        return "Bluetooth permission denied. Check your user's bluetooth group membership and polkit rules."
    if "not found" in low or "no device" in low or "does not exist" in low:
        return "Device not found. Is the band powered on and in range?"
    if "inprogress" in low or "in progress" in low or "already" in low:
        return "Device is already connected to something else (close the vendor dashboard)."
    if "timeout" in low or "timed out" in low:
        return "Connection timed out. The band may be off, out of range, or busy."
    if "insufficient authentication" in low or "encryption" in low:
        return "The band rejected the connection: authentication/encryption failed. Re-pair it."
    return text


class Transport:
    """Interface. on_disconnect is called once when the link drops."""

    battery = None
    mtu = None
    on_disconnect = None

    async def connect(self):
        raise NotImplementedError

    async def start(self, handler):
        raise NotImplementedError

    async def stop(self):
        pass

    async def disconnect(self):
        pass


class BleTransport(Transport):
    def __init__(self, device_ref, start_write=False):
        self.device_ref = device_ref
        self.start_write = start_write
        self.client = None

    def _on_disconnect(self, _client):
        if self.on_disconnect:
            self.on_disconnect()

    async def connect(self):
        from bleak import BleakClient

        self.client = BleakClient(self.device_ref, disconnected_callback=self._on_disconnect, timeout=20.0)
        try:
            await self.client.connect()
        except Exception as exc:
            raise TransportError(describe_error(exc)) from exc

        missing = [s for s, c in proto.CHARS.items() if self.client.services.get_characteristic(c) is None]
        if missing:
            await self.client.disconnect()
            raise CharacteristicMissing(
                f"Characteristic missing for: {', '.join(missing)}. "
                "This is not the expected Link Band service layout."
            )
        self.mtu = getattr(self.client, "mtu_size", None)
        try:
            self.battery = (await self.client.read_gatt_char(proto.BATTERY_CHAR))[0]
        except Exception:
            self.battery = None
        if self.start_write:
            try:
                await self.client.write_gatt_char(proto.EEG_WRITE_CHAR, b"start", response=False)
            except Exception as exc:
                raise TransportError(f"write to the EEG start characteristic failed: {describe_error(exc)}") from exc

    async def start(self, handler):
        def make(stream):
            def cb(_sender, data):
                handler(stream, bytes(data), time.monotonic_ns(), time.time())

            return cb

        for stream, char in proto.CHARS.items():
            try:
                await self.client.start_notify(char, make(stream))
            except Exception as exc:
                raise TransportError(f"could not subscribe to {stream}: {describe_error(exc)}") from exc

    async def stop(self):
        if not self.client or not self.client.is_connected:
            return
        for char in proto.CHARS.values():
            try:
                await self.client.stop_notify(char)
            except Exception:
                pass

    async def disconnect(self):
        if self.client and self.client.is_connected:
            try:
                await self.client.disconnect()
            except Exception:
                pass


# ---------------------------------------------------------------- mock


def _enc_eeg(ts_raw, samples):
    out = bytearray(ts_raw.to_bytes(4, "little"))
    for lead, ch1, ch2 in samples:
        out.append(lead)
        out += (ch1 & 0xFFFFFF).to_bytes(3, "big")
        out += (ch2 & 0xFFFFFF).to_bytes(3, "big")
    return bytes(out)


def _enc_ppg(ts_raw, samples):
    out = bytearray(ts_raw.to_bytes(4, "little"))
    for red, ir in samples:
        out += (red & 0xFFFFFF).to_bytes(3, "big")
        out += (ir & 0xFFFFFF).to_bytes(3, "big")
    return bytes(out)


def _enc_acc(ts_raw, samples):
    out = bytearray(ts_raw.to_bytes(4, "little"))
    for x, y, z in samples:
        out += int(x).to_bytes(2, "little", signed=True)
        out += int(y).to_bytes(2, "little", signed=True)
        out += int(z).to_bytes(2, "little", signed=True)
    return bytes(out)


class MockTransport(Transport):
    """Synthetic band. Loss rates, the ACC death and the 60 s stall are the
    numbers measured in the vendor's recordings; the PPG timestamp bug is
    reproduced bit for bit from linkband_sessions.json."""

    DROP = {"eeg": 0.093, "ppg": 0.15, "acc": 0.41}
    DISCONNECT_AT = 30.0
    DISCONNECT_FOR = 60.0
    ACC_DIES_AT = 64.0

    def __init__(self, seed=7):
        self.rng = random.Random(seed)
        self.battery = 87
        self.mtu = 247
        self._tasks = []
        self._down = False
        self._fired = False
        self._t0 = None
        self._ts_base = 302 * 32768
        self._ts = {"ppg": 302 * 32768}

    def _elapsed(self):
        return time.monotonic() - self._t0

    async def connect(self):
        if self._down:
            raise TransportError("device not reachable (mock disconnect in progress)")

    async def start(self, handler):
        if self._t0 is None:
            self._t0 = time.monotonic()
        self._handler = handler
        for stream in proto.STREAMS:
            self._tasks.append(asyncio.create_task(self._pump(stream)))
        self._tasks.append(asyncio.create_task(self._watchdog()))

    async def _watchdog(self):
        while True:
            await asyncio.sleep(0.2)
            if not self._fired and self._elapsed() >= self.DISCONNECT_AT:
                self._fired = True
                self._down = True
                for t in self._tasks:
                    if t is not asyncio.current_task():
                        t.cancel()
                if self.on_disconnect:
                    self.on_disconnect()
                await asyncio.sleep(self.DISCONNECT_FOR)
                self._down = False

    async def _pump(self, stream):
        rate = proto.RATES[stream]
        per_packet = {"eeg": 25, "ppg": 28, "acc": 30}[stream]
        period = per_packet / rate
        while True:
            await asyncio.sleep(period)
            if stream == "acc" and self._elapsed() >= self.ACC_DIES_AT:
                return
            if self.rng.random() < self.DROP[stream]:
                continue
            payload = self._packet(stream, per_packet)
            self._handler(stream, payload, time.monotonic_ns(), time.time())

    def _packet(self, stream, n):
        if stream == "eeg":
            ts = (self._ts_base + int(self._elapsed() * 32768)) & 0xFFFFFFFF
            base = 1_200_000
            samples = [
                (0, base + int(2000 * self.rng.gauss(0, 1)) + i, base + 900_000 + int(2000 * self.rng.gauss(0, 1)))
                for i in range(n)
            ]
            return _enc_eeg(ts, samples)
        if stream == "ppg":
            ts = self._ts["ppg"]
            # Measured in the real export: packet-to-packet deltas of
            # +23696 s .. +24720 s at 32.768 kHz, sometimes negative.
            if self.rng.random() < 0.15:
                step = -107374 * 32768 + self.rng.choice([0, 256])
            else:
                step = (23696 + self.rng.choice([0, 0, 512, 512, 1024])) * 32768
            self._ts["ppg"] = (ts + step) & 0xFFFFFFFF
            samples = [
                (1_440_000 + self.rng.randint(-400, 400), 1_434_000 + self.rng.randint(-900, 900))
                for _ in range(n)
            ]
            return _enc_ppg(ts, samples)
        ts = (self._ts_base + int(self._elapsed() * 32768)) & 0xFFFFFFFF
        samples = [(64 + self.rng.randint(-2, 2), 21 + self.rng.randint(-2, 2), -14 + self.rng.randint(-2, 2)) for _ in range(n)]
        return _enc_acc(ts, samples)
