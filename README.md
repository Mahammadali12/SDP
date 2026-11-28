# linkband_capture

Minimal, append-only raw capture for the Looxid Link Band 2.0 (EEG / PPG / ACC),
built to replace the vendor dashboard for thesis data collection.

- Python + `bleak` (native BLE). No GUI, no database, no cloud, no filtering.
- Stores what the device sent, with the host arrival time as the reference clock.
- Flushes and fsyncs every 2 s. A crash loses at most ~2 s and never the session.
- Records through disconnects instead of stopping at the first one.
- Tells you, per stream, exactly how much data you did not get.

## Install

```sh
uv venv --python 3.13 .venv-lb
uv pip install --python .venv-lb/bin/python -r requirements.txt
```

`bleak==0.22.3` is the same version the vendor SDK pins. On Linux it pulls
`dbus-fast`, which has prebuilt wheels for CPython 3.13 — no Rust toolchain
needed. Python 3.14 also has wheels; 3.11 has no `pip` on this machine.

## Use

```sh
# 1. find the band (non-zero exit if nothing matches "LXB-")
.venv-lb/bin/python -m linkband_capture scan

# 2. record
.venv-lb/bin/python -m linkband_capture record --device LXB-02630008 --duration 600 --name sub01_rest

# 3. audit
.venv-lb/bin/python -m linkband_capture report sessions/sub01_rest
```

While recording, one status line per second goes to **stderr**:

```
state=recording eeg=12500(249.8/s) ppg=2500(50.0/s) acc=1500(30.0/s) bat=87% t=50s
```

If stdin is a TTY, every line you type becomes a marker row in `events.jsonl`
and is echoed back to the terminal. Under a pipe or a `--duration` script,
markers are disabled automatically rather than blocking.

`--mock` runs the whole pipeline against a synthetic band that reproduces the
failure modes measured in the vendor's own recordings: 9.3 % EEG / 15 % PPG /
41 % ACC packet loss, one 60 s disconnect, ACC dying at t=64 s, and the exact
PPG timestamp corruption. Use it to check the tool, never as data.

```sh
# decoder self-checks
.venv-lb/bin/python -m unittest discover -s tests -v
```

## Output

```
sessions/sub01_rest/
  eeg.csv  ppg.csv  acc.csv
  events.jsonl
  session.json
```

Every CSV row:

| column | meaning |
|---|---|
| `stream` | `eeg` / `ppg` / `acc` |
| `sample_index` | monotonic per stream, never reused |
| `packet_index` | monotonic per stream, counts notifications |
| `pos_in_packet` | index of the sample inside its notification |
| `dev_ts_raw` | the 32-bit device timestamp, **verbatim**, one value per packet |
| `host_monotonic_ns` | host `time.monotonic_ns()` when the notification arrived |
| `host_wall_iso` | host UTC wall clock for the same instant |

Plus `ch1_raw`/`ch2_raw`/`ch1_uv`/`ch2_uv`/`leadoff_ch1`/`leadoff_ch2` (EEG),
`red_raw`/`ir_raw` (PPG), `x_raw`/`y_raw`/`z_raw` (ACC).

Notes that matter:

- `host_monotonic_ns` is identical for every sample in one notification. That is
  the truth: the device did not tell us when each sample happened.
- EEG µV uses the vendor's own scale, 0.040064 µV/LSB
  (`4.033/12/(2**23-1)*1e6`). PPG and ACC have **no documented scale** and are
  stored as raw counts; `session.json` lists them under `units_unverified`.
- `dev_ts_raw` for PPG is unusable. Measured in real data: it jumps by ±10⁵ s
  between 0.56 s packets. Recorded anyway, never used for alignment.

`events.jsonl` is one JSON object per line: `session_start`, `connected`
(with MTU and battery), `disconnected`, `reconnected`, `reconnect_failed`,
`stream_stalled`, `proto_error`, `disk_error`, `marker`, plus session events.

`session.json` holds the device, nominal rates, `units_unverified`, the sample
counts, every disconnect with its start time and duration, and the observed
notification payload lengths (a short length means MTU truncation).

## Report

Per stream: samples, expected from host duration × nominal rate, loss over active
time, capture over the whole session, gap count, longest gap, longest contiguous
run. Then device-clock monotonicity, drift against the host clock, max forward
and backward step, value range, how many samples sit on the file's exact max and
min, and a glitch count (|Δ| > 100 × median |Δ|).

Verdict rules:

| check | passes when |
|---|---|
| `disconnects == 0` | no disconnect in a run of ≥ 600 s (n/a below that) |
| `<stream> loss < 1%` | loss over active time is under 1 % |
| `<stream> device timestamps monotonic` | no backward step in `dev_ts_raw` |
| `<stream> alive to the end` | last sample is within 5 s of the session end |

Exit code 0 on PASS, 1 on FAIL.

## Errors

Plain sentences, no stack traces.

| situation | message |
|---|---|
| Bluetooth off / no adapter | `Bluetooth is off, or this machine has no usable adapter.` |
| permission denied | `Bluetooth permission denied. Check your user's bluetooth group membership and polkit rules.` |
| device not found | `Device not found. Is the band powered on and in range?` |
| connected elsewhere | `Device is already connected to something else (close the vendor dashboard).` |
| connect timeout | `Connection timed out. The band may be off, out of range, or busy.` |
| auth/encryption rejected | `The band rejected the connection: authentication/encryption failed. Re-pair it.` |
| service layout wrong | `Characteristic missing for: <streams>. This is not the expected Link Band service layout.` |
| write failed (`--start-write`) | `write to the EEG start characteristic failed: <reason>` |
| disk full | `disk full while writing: <errno detail>` |
| output dir exists | `output directory already exists: <path>` |
| nothing found by `scan` | `no Link Band found (no device whose name starts with 'LXB-')`, exit 2 |

## What this does not do

No filtering, interpolation, resampling, timestamp repair, outlier removal,
unit conversion for PPG/ACC, GUI, database, cloud upload, ML, or automatic
artifact rejection. It records and it audits.

## Status

Designed, implemented, and validated against **the mock and the vendor's own
recorded export**. It has **never run against a physical band** — no `LXB-`
device was in range when the code was written. See `NOTES.md`.
