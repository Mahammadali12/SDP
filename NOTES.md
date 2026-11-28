# NOTES — unknowns, assumptions, risks

Everything the tool does *not* know, and every assumption it makes to keep
working anyway. Read this before trusting any number it prints.

## UNKNOWN — verified as unknown, not guessed

1. **Whether the firmware needs a start command.** The vendor's Electron app
   never writes anything; it only calls `start_notify` on the three
   characteristics (`link_band_sdk/python_core/app/core/device.py:501-531`). The
   older Tk GUI additionally wrote `b'start'` / `b'stop'` to
   `0065cacb-9e52-21bf-a849-99a80d83830e` (`original_gui_sdk.py:315,323`). Which
   one the current firmware expects is unknown. `--start-write` sends `b'start'`
   for testing. **Never run against hardware yet.**
2. **Device timestamp epoch and accuracy.** Stored verbatim as `dev_ts_raw`, a
   uint32 counter. Seconds = `raw / 32768`. The epoch is unknown and the counter
   wraps every 2³²/32768 = **36.4 hours**.
3. **PPG and ACC units.** The SDK documents a µV scale for EEG only. PPG is
   stored as raw 24-bit counts, ACC as raw int16, both listed under
   `units_unverified` in `session.json`. Do not convert.
4. **Why PPG packet timestamps are garbage.** In `linkband_sessions.json` the
   packet-to-packet base delta is +23 696 s … +24 720 s at 32.768 kHz for a
   packet that holds 0.56 s of samples, and occasionally negative. Either a
   firmware defect or a decode defect in the SDK. **Do not use PPG
   `dev_ts_raw` for anything.** Anchor PPG to `host_monotonic_ns` and the
   50 Hz spacing inside a packet.
5. **Why ACC has 1/64 g resolution.** The vendor's own exports carry 8 effective
   bits per axis (x, y, z quantised to 0.015625 g). Whether that is the sensor,
   the firmware, or the vendor's decode is unknown.
6. **Negotiated MTU on this machine.** Read at connect and written to
   `session.json`. No hardware has been tested, so the real value is unknown.
7. **True nominal rates under loss.** 250/50/30 Hz are vendor constants. Inside a
   notification the spacing is exactly 4.000 / 20.000 / 33.333 ms, so the rates
   are right *when samples are present*. They say nothing about the true rate
   when the device is skipping.
8. **`signalQuality`.** Not a device field. It is computed host-side by the SDK
   (`signal_processing.py:362`, `'good' if good_quality_samples >= 1000 else
   'poor'`) and is constant in every recording examined. This tool does not read
   or write it.

## Assumptions

1. **Host arrival time is the reference clock** for every sample. Device
   timestamps are recorded but never used for alignment.
2. **A notification contains whole sample frames.** If `(len - 4) % frame != 0`
   the notification is logged as `proto_error` and **dropped**, never decoded
   around. A truncated notification therefore shows up as loss, not as garbage
   values.
3. **Gap** = inter-sample host interval > 3 × nominal interval.
4. **Glitch** = |Δ| from the previous sample > 100 × the channel's median |Δ|.
   Chosen after finding isolated `0x600000` / `0x66` samples in real fp2 data.
5. **Loss** is computed over *active* time (session duration minus logged
   disconnect windows). `capture` is computed over the full session duration.
   A stream that dies mid-session shows high loss and fails "alive to the end".
6. Disconnect windows come from `events.jsonl`; a run killed with SIGKILL has no
   end event, so `report` falls back to the span of the recorded samples and
   prints `(estimated: session was not closed cleanly)`.

## Risks to data

1. **Up to 2 s of rows are unsynced.** fsync runs every 2 s; a hard crash can
   lose that window. Nothing in memory is needed to reconstruct the rest.
2. **MTU below 187 truncates notifications.** They are dropped as `proto_error`
   and the loss is visible, but the session is degraded. The tool prints a
   warning at connect.
3. **PPG cannot be aligned to EEG by device time.** It has to be aligned by host
   arrival, which is only as good as the notification delivery jitter.
4. **ACC may stop without an error.** Seen in real data: ACC died at t=63.9 s
   while EEG continued. `stream_stalled` events and the "alive to the end" check
   catch it, but nothing recovers it.
5. **No GATT writes are performed** unless `--start-write` is passed, so the
   "write failure" error path is unreachable by default.
6. **`session.json` is rewritten at close.** A SIGKILL leaves `status: running`
   and no `duration_s`; the CSVs are still valid and `report` still works.
7. **Nothing here has run against the physical band.** Every number below comes
   from the mock or from the vendor's own export. See README for what was
   actually executed.
