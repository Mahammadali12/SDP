"""Append-only session output: 3 CSVs, events.jsonl, session.json.

Rows are written as they arrive and never held in memory. fsync happens on a
2 s cadence from the recorder, so a hard crash loses at most ~2 s.
"""

import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

COMMON = [
    "stream",
    "sample_index",
    "packet_index",
    "pos_in_packet",
    "dev_ts_raw",
    "host_monotonic_ns",
    "host_wall_iso",
]
COLUMNS = {
    "eeg": COMMON + ["ch1_raw", "ch2_raw", "ch1_uv", "ch2_uv", "leadoff_ch1", "leadoff_ch2"],
    "ppg": COMMON + ["red_raw", "ir_raw"],
    "acc": COMMON + ["x_raw", "y_raw", "z_raw"],
}


def iso(epoch_s):
    return datetime.fromtimestamp(epoch_s, timezone.utc).isoformat(timespec="microseconds")


def fsync_error_text(exc):
    if getattr(exc, "errno", None) == 28:  # ENOSPC
        return f"disk full while writing: {exc}"
    return f"write failed: {exc}"


class SessionWriter:
    def __init__(self, out_dir, name, meta):
        self.dir = Path(out_dir) / name
        self.dir.mkdir(parents=True, exist_ok=False)
        self.meta = dict(meta)
        self.meta["session_name"] = name
        self.meta["started_at"] = iso(time.time())
        self.meta["status"] = "running"
        self.counts = {s: 0 for s in COLUMNS}
        self._csv = {}
        self._rows = {}
        for stream, cols in COLUMNS.items():
            f = open(self.dir / f"{stream}.csv", "w", newline="", encoding="utf-8")
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="raise")
            w.writeheader()
            self._csv[stream] = (f, w)
        self._events = open(self.dir / "events.jsonl", "a", encoding="utf-8")
        self._write_session_json()

    def row(self, stream, values):
        f, w = self._csv[stream]
        w.writerow(values)
        self.counts[stream] += 1

    def event(self, kind, **fields):
        rec = {"kind": kind, "host_monotonic_ns": time.monotonic_ns(), "host_wall_iso": iso(time.time())}
        rec.update(fields)
        self._events.write(json.dumps(rec) + "\n")
        self._events.flush()

    def flush(self, sync=False):
        for f, _ in self._csv.values():
            f.flush()
        self._events.flush()
        if sync:
            for f, _ in self._csv.values():
                os.fsync(f.fileno())
            os.fsync(self._events.fileno())

    def _write_session_json(self):
        self.meta["sample_counts"] = dict(self.counts)
        tmp = self.dir / "session.json"
        tmp.write_text(json.dumps(self.meta, indent=2) + "\n", encoding="utf-8")

    def close(self, status="complete", **extra):
        self.flush(sync=True)
        for f, _ in self._csv.values():
            f.close()
        self._events.close()
        self.meta.update(extra)
        self.meta["status"] = status
        self.meta["ended_at"] = iso(time.time())
        self._write_session_json()
