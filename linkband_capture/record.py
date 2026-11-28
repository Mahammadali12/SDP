"""The recording loop: connect, subscribe, write rows, reconnect, report status."""

import asyncio
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone

from . import proto
from .transport import BleTransport, MockTransport, TransportError
from .writer import SessionWriter, fsync_error_text, iso

FSYNC_EVERY = 2.0
STALL_AFTER = 5.0


class Recorder:
    def __init__(self, transport, out_dir, name, duration=None, meta=None):
        self.transport = transport
        self.out_dir = out_dir
        self.name = name
        self.duration = duration
        self.meta = meta or {}
        self.queue = asyncio.Queue()
        self.disconnected = False
        self.down_at = None
        self.stop = False
        self.writer = None
        self.counters = {s: {"sample": 0, "packet": 0, "last": None, "stalled": False} for s in proto.STREAMS}
        self.rates = {s: deque(maxlen=6) for s in proto.STREAMS}
        self.lengths = {s: {} for s in proto.STREAMS}
        self.disconnects = []
        self.last_sync = 0.0
        self.last_tick = 0.0
        self.t0 = None
        self.t0_ns = None
        self.loop = None

    # -- callbacks -------------------------------------------------
    def on_packet(self, stream, payload, mono_ns, wall):
        self.queue.put_nowait(("packet", stream, payload, mono_ns, wall))

    def on_disconnect(self):
        self.down_at = time.monotonic()
        self.disconnected = True

    # -- lifecycle -------------------------------------------------
    def open(self):
        self.writer = SessionWriter(
            self.out_dir,
            self.name,
            {
                "device": self.meta.get("device"),
                "device_name": self.meta.get("device_name"),
                "transport": self.meta.get("transport"),
                "nominal_rates_hz": proto.RATES,
                "device_clock_hz": proto.DEVICE_CLOCK_HZ,
                "eeg_uv_per_lsb": proto.EEG_LSB_UV,
                "units_unverified": ["ppg_raw", "acc_raw"],
                "host_time_is_reference": True,
                "filtering": "none",
                "byte_layout_source": "link_band_sdk/python_core/app/core/device.py:609-800",
            },
        )
        self.writer.event("session_start", name=self.name, transport=self.meta.get("transport"))

    def close(self, status, error=None):
        self.writer.close(
            status,
            sample_counts={s: self.counters[s]["sample"] for s in proto.STREAMS},
            disconnect_count=len(self.disconnects),
            disconnects=self.disconnects,
            payload_lengths={s: {str(k): v for k, v in self.lengths[s].items()} for s in proto.STREAMS},
            duration_s=round(time.monotonic() - self.t0, 3) if self.t0 else None,
            start_monotonic_ns=self.t0_ns,
            error=error,
        )

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.t0 = time.monotonic()
        self.t0_ns = time.monotonic_ns()
        self.open()
        self._start_marker_reader()
        connected = False
        ever_connected = False
        backoff = 1.0

        while not self.stop:
            self._tick()  # self-limited to 1 Hz: status line, stall detection, fsync
            if not connected:
                try:
                    await self.transport.connect()
                    await self.transport.start(self.on_packet)
                except TransportError as exc:
                    self.writer.event("connect_failed", message=str(exc), retry_in_s=backoff if ever_connected else None)
                    if not ever_connected:
                        self.close("failed", error=str(exc))
                        raise
                    await self._wait(backoff)
                    backoff = min(backoff * 2, 10.0)
                    continue
                connected = True
                ever_connected = True
                backoff = 1.0
                if self.disconnected:
                    down = round(time.monotonic() - self.down_at, 3)
                    self.disconnects[-1]["duration_s"] = down
                    self.writer.event("reconnected", downtime_s=down)
                else:
                    self.writer.event("connected", mtu=self.transport.mtu, battery=self.transport.battery)
                self.disconnected = False
                self.down_at = None
                if (self.transport.mtu or 9999) < proto.MIN_USEFUL_MTU:
                    print(
                        f"warning: negotiated MTU {self.transport.mtu} < {proto.MIN_USEFUL_MTU}; "
                        "notifications will be truncated",
                        file=sys.stderr,
                    )

            if self.disconnected:
                connected = False
                at = round((self.down_at or time.monotonic()) - self.t0, 3)
                self.disconnects.append({"start_s": at, "duration_s": None})
                self.writer.event("disconnected", at_elapsed_s=at)
                await self.transport.disconnect()
                continue

            if self.duration and time.monotonic() - self.t0 >= self.duration:
                break

            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            self._handle(item)

        await self.transport.stop()
        await self.transport.disconnect()
        self.close("complete")
        return self.writer

    async def _wait(self, seconds):
        """Sleep while keeping the status line and markers alive."""
        end = time.monotonic() + seconds
        while not self.stop and time.monotonic() < end:
            self._tick()
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                self._handle(item)
            except asyncio.TimeoutError:
                pass

    # -- per item --------------------------------------------------
    def _handle(self, item):
        if item[0] == "marker":
            self.writer.event("marker", text=item[1])
            print(f"\n[marker] {item[1]}", file=sys.stderr)
            return
        _, stream, payload, mono_ns, wall = item
        c = self.counters[stream]
        self.lengths[stream][len(payload)] = self.lengths[stream].get(len(payload), 0) + 1
        try:
            ts_raw, samples = proto.DECODERS[stream](payload)
        except proto.ProtoError as exc:
            self.writer.event("proto_error", stream=stream, message=str(exc), payload_bytes=len(payload))
            return
        c["last"] = time.monotonic()
        c["stalled"] = False
        iso_wall = iso(wall)
        idx = c["sample"]
        pkt = c["packet"]
        for pos, sample in enumerate(samples):
            row = {
                "stream": stream,
                "sample_index": idx + pos,
                "packet_index": pkt,
                "pos_in_packet": pos,
                "dev_ts_raw": ts_raw,
                "host_monotonic_ns": mono_ns,
                "host_wall_iso": iso_wall,
            }
            row.update(sample)
            if stream == "eeg":
                row["ch1_uv"] = sample["ch1_raw"] * proto.EEG_LSB_UV
                row["ch2_uv"] = sample["ch2_raw"] * proto.EEG_LSB_UV
            try:
                self.writer.row(stream, row)
            except OSError as exc:
                self.writer.event("disk_error", message=fsync_error_text(exc))
                raise SystemExit(fsync_error_text(exc)) from exc
        c["sample"] = idx + len(samples)
        c["packet"] = pkt + 1
        if time.monotonic() - self.last_sync >= FSYNC_EVERY:
            self._sync()

    # -- 1 Hz tick -------------------------------------------------
    def _sync(self):
        try:
            self.writer.flush(sync=True)
        except OSError as exc:
            self.writer.event("disk_error", message=fsync_error_text(exc))
            raise SystemExit(fsync_error_text(exc)) from exc
        self.last_sync = time.monotonic()

    def _tick(self):
        now = time.monotonic()
        if now - self.last_tick < 1.0:
            return
        self.last_tick = now
        parts = []
        for s in proto.STREAMS:
            n = self.counters[s]["sample"]
            r = self.rates[s]
            r.append((now, n))
            rate = (r[-1][1] - r[0][1]) / (r[-1][0] - r[0][0]) if len(r) > 1 else 0.0
            parts.append(f"{s}={n}({rate:.1f}/s)")
            last = self.counters[s]["last"]
            if not self.disconnected and last is not None and not self.counters[s]["stalled"] and now - last > STALL_AFTER:
                self.counters[s]["stalled"] = True
                self.writer.event("stream_stalled", stream=s, silent_for_s=round(now - last, 1))
        state = "reconnecting" if self.disconnected else "recording"
        bat = f" bat={self.transport.battery}%" if self.transport.battery is not None else ""
        print(f"state={state} {' '.join(parts)}{bat} t={now - self.t0:.0f}s", file=sys.stderr)
        if now - self.last_sync >= FSYNC_EVERY:
            self._sync()

    def _start_marker_reader(self):
        if not sys.stdin or not sys.stdin.isatty():
            return
        import threading

        loop = self.loop

        def pump():
            for line in sys.stdin:
                line = line.strip()
                if line:
                    loop.call_soon_threadsafe(self.queue.put_nowait, ("marker", line))

        threading.Thread(target=pump, daemon=True).start()


def auto_name():
    return "session_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _set_stop(rec):
    rec.stop = True


async def run_record(args):
    meta = {"device": args.device, "device_name": args.device, "transport": "mock" if args.mock else "ble"}
    transport = MockTransport() if args.mock else BleTransport(args.device, start_write=args.start_write)
    rec = Recorder(transport, args.out, args.name or auto_name(), duration=args.duration, meta=meta)
    transport.on_disconnect = rec.on_disconnect

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _set_stop, rec)
        except NotImplementedError:
            pass

    try:
        await rec.run()
    except TransportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except FileExistsError as exc:
        print(f"error: output directory already exists: {exc.filename}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {fsync_error_text(exc)}", file=sys.stderr)
        return 1
    print(f"session written to {rec.writer.dir}", file=sys.stderr)
    print(
        f"  eeg={rec.counters['eeg']['sample']} "
        f"ppg={rec.counters['ppg']['sample']} "
        f"acc={rec.counters['acc']['sample']}"
    )
    return 0
