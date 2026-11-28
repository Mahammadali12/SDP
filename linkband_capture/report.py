"""`report`: audit a session directory against the acceptance criteria."""

import csv
import json
import statistics
import sys
from pathlib import Path

from . import proto

GLITCH_FACTOR = 100.0
GAP_FACTOR = 3.0
ALIVE_SLACK_S = 5.0
TEN_MINUTES = 600.0

SIGNALS = {
    "eeg": ["ch1_raw", "ch2_raw"],
    "ppg": ["red_raw", "ir_raw"],
    "acc": ["x_raw", "y_raw", "z_raw"],
}
INT_COLS = {"host_monotonic_ns", "dev_ts_raw", "packet_index"}


def _load(path, columns):
    """Column-oriented load. Dict-per-row would cost ~1 GB on a 1 h session."""
    data = {c: [] for c in columns}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in columns if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"{path}: missing columns {missing}")
        for row in reader:
            for c in columns:
                data[c].append(int(row[c]) if c in INT_COLS else float(row[c]))
    return data


def _stream_stats(data, stream, rate, active_s, total_s):
    cols = SIGNALS[stream]
    host = data["host_monotonic_ns"]
    n = len(host)
    out = {"stream": stream, "n": n}
    out["expected"] = active_s * rate
    out["loss"] = 1.0 - n / out["expected"] if out["expected"] > 0 else None
    out["capture"] = n / (total_s * rate) if total_s > 0 else None
    if n < 2:
        return out

    # Every sample in a notification shares one host timestamp, so gaps must be
    # measured between notifications, not between rows.
    arrivals = []
    prev = None
    for i, p in enumerate(data["packet_index"]):
        if p != prev:
            arrivals.append([host[i], data["dev_ts_raw"][i], 0])
            prev = p
        arrivals[-1][2] += 1

    modal = statistics.mode([a[2] for a in arrivals])
    expected_dt = modal / rate * 1e9
    deltas = [b[0] - a[0] for a, b in zip(arrivals, arrivals[1:])]

    out["packets"] = len(arrivals)
    out["gaps"] = sum(1 for d in deltas if d > GAP_FACTOR * expected_dt)
    out["longest_gap"] = max(deltas) / 1e9 if deltas else 0.0
    run = best = arrivals[0][2]
    for a, d in zip(arrivals[1:], deltas):
        run = a[2] if d > GAP_FACTOR * expected_dt else run + a[2]
        best = max(best, run)
    out["longest_run"] = best
    out["host_span"] = (host[-1] - host[0]) / 1e9

    dd = [b[1] - a[1] for a, b in zip(arrivals, arrivals[1:])]
    out["dev_backward"] = sum(1 for d in dd if d < 0)
    out["dev_max_back"] = max(0, -min(dd)) / proto.DEVICE_CLOCK_HZ
    out["dev_max_fwd"] = max(0, max(dd)) / proto.DEVICE_CLOCK_HZ
    dev_span = (arrivals[-1][1] - arrivals[0][1]) / proto.DEVICE_CLOCK_HZ
    host_pkt_span = (arrivals[-1][0] - arrivals[0][0]) / 1e9
    out["drift_ppm"] = (dev_span - host_pkt_span) / host_pkt_span * 1e6 if host_pkt_span else None

    out["signals"] = {}
    for c in cols:
        v = data[c]
        dv = [abs(b - a) for a, b in zip(v, v[1:])]
        med = statistics.median(dv) if dv else 0.0
        hi, lo = max(v), min(v)
        out["signals"][c] = {
            "min": lo,
            "max": hi,
            "mean": statistics.fmean(v),
            "at_max": sum(1 for x in v if x == hi),
            "at_min": sum(1 for x in v if x == lo),
            "glitches": sum(1 for d in dv if d > GLITCH_FACTOR * med) if med > 0 else 0,
        }
    return out


def build_report(session_dir):
    d = Path(session_dir)
    meta = json.loads((d / "session.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (d / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]

    rates = meta["nominal_rates_hz"]
    data = {}
    for stream in proto.STREAMS:
        p = d / f"{stream}.csv"
        data[stream] = _load(p, ["host_monotonic_ns", "dev_ts_raw", "packet_index"] + SIGNALS[stream]) if p.exists() else None

    total_s = meta.get("duration_s")
    if total_s is None:  # killed before close(): rebuild from what reached disk
        stamps = [x for s in proto.STREAMS if data[s] for x in (data[s]["host_monotonic_ns"][:1] + data[s]["host_monotonic_ns"][-1:])]
        total_s = (max(stamps) - min(stamps)) / 1e9 if stamps else 0.0
        meta["duration_s_estimated"] = True

    disconnects = [e for e in events if e["kind"] == "disconnected"]
    down_s = sum((x.get("duration_s") or 0.0) for x in meta.get("disconnects") or [])
    active_s = max(total_s - down_s, 0.0)

    stats = {}
    for s in proto.STREAMS:
        if data[s] is None or not data[s]["host_monotonic_ns"]:
            stats[s] = {"stream": s, "n": 0, "expected": active_s * rates[s], "loss": 1.0, "capture": 0.0}
        else:
            stats[s] = _stream_stats(data[s], s, rates[s], active_s, total_s)

    start_ns = meta.get("start_monotonic_ns")
    if start_ns is None:
        firsts = [data[s]["host_monotonic_ns"][0] for s in proto.STREAMS if data[s] and data[s]["host_monotonic_ns"]]
        start_ns = min(firsts) if firsts else None

    checks = []
    if total_s >= TEN_MINUTES:
        checks.append(("disconnects == 0 (10 min run)", len(disconnects) == 0, f"{len(disconnects)} disconnect(s), {down_s:.1f} s down"))
    else:
        checks.append(("disconnects == 0 (run shorter than 10 min)", None, f"{len(disconnects)} disconnect(s), {down_s:.1f} s down"))
    for s in proto.STREAMS:
        st = stats[s]
        checks.append((f"{s} loss < 1% of active time", (st["loss"] if st["loss"] is not None else 1.0) < 0.01, f"{(st['loss'] or 0) * 100:.2f}%"))
        if st["n"] >= 2:
            checks.append((f"{s} device timestamps monotonic", st["dev_backward"] == 0, f"{st['dev_backward']} backward jump(s)"))
            last_elapsed = (data[s]["host_monotonic_ns"][-1] - start_ns) / 1e9
            checks.append(
                (
                    f"{s} alive to the end",
                    total_s - last_elapsed <= ALIVE_SLACK_S,
                    f"last sample {last_elapsed:.1f} s into a {total_s:.1f} s session",
                )
            )
    verdict = all(ok for _, ok, _ in checks if ok is not None)
    return meta, events, stats, checks, verdict, total_s, active_s, down_s


def print_report(session_dir):
    meta, events, stats, checks, verdict, total_s, active_s, down_s = build_report(session_dir)
    w = sys.stdout.write
    w(f"session  {meta.get('session_name')}   transport={meta.get('transport')}\n")
    w(f"device   {meta.get('device')}\n")
    est = " (estimated: session was not closed cleanly)" if meta.get("duration_s_estimated") else ""
    w(f"duration {total_s:.2f} s{est}   active {active_s:.2f} s   down {down_s:.2f} s   disconnects {len(meta.get('disconnects') or [])}\n")
    for i, x in enumerate(meta.get("disconnects") or [], 1):
        dur = x["duration_s"]
        w(f"  #{i} at t={x['start_s']:.3f} s for {dur:.3f} s\n" if dur is not None else f"  #{i} at t={x['start_s']:.3f} s, never reconnected\n")
    for e in events:
        if e["kind"] in ("stream_stalled", "proto_error", "disk_error"):
            w(f"  event {e['kind']}: {e.get('stream', '')} {e.get('message', '')} {e.get('silent_for_s', '')}\n")

    w("\nstream   samples  expected    loss   capture   gaps  longest_gap  longest_run\n")
    for s in proto.STREAMS:
        st = stats[s]
        loss = f"{st['loss'] * 100:6.2f}%" if st["loss"] is not None else "    n/a"
        cap = f"{st['capture'] * 100:6.2f}%" if st["capture"] is not None else "    n/a"
        w(
            f"{s:<8} {st['n']:>7} {st['expected']:>9.0f} {loss:>7} {cap:>8} "
            f"{st.get('gaps', 0):>6} {st.get('longest_gap', 0):>11.3f}s {st.get('longest_run', 0):>11}\n"
        )

    for s in proto.STREAMS:
        st = stats[s]
        if st["n"] < 2:
            w(f"\n{s} device time: no samples\n")
            continue
        drift = f"drift {st['drift_ppm']:+.0f} ppm" if st["dev_backward"] == 0 else "drift n/a (clock not monotonic)"
        w(
            f"\n{s} device time: {'monotonic' if st['dev_backward'] == 0 else 'NOT MONOTONIC'} "
            f"({st['dev_backward']} backward)  {drift}  "
            f"max step +{st['dev_max_fwd']:.3f}s / -{st['dev_max_back']:.3f}s\n"
        )
        for c, v in st["signals"].items():
            w(
                f"  {c:<9} range {v['min']:.3f} .. {v['max']:.3f}  mean {v['mean']:.3f}  "
                f"at_max {v['at_max']}  at_min {v['at_min']}  glitches {v['glitches']}\n"
            )

    w("\nVERDICT " + ("PASS" if verdict else "FAIL") + "\n")
    for name, ok, detail in checks:
        tag = "  n/a" if ok is None else ("  ok " if ok else " FAIL")
        w(f"[{tag}] {name}: {detail}\n")
    return 0 if verdict else 1
