"""Command line: scan / record / report."""

import argparse
import asyncio
import sys

from .report import print_report


def cmd_scan(args):
    from bleak import BleakScanner

    from .transport import describe_error

    try:
        devices = asyncio.run(BleakScanner.discover(timeout=args.timeout))
    except Exception as exc:
        print(f"error: {describe_error(exc)}", file=sys.stderr)
        return 1
    found = [d for d in devices if d.name and d.name.startswith("LXB-")]
    for d in sorted(found, key=lambda d: -(d.rssi or -999)):
        print(f"{d.name}  {d.address}  {d.rssi} dBm")
    if not found:
        print("error: no Link Band found (no device whose name starts with 'LXB-')", file=sys.stderr)
        return 2
    return 0


def cmd_record(args):
    from .record import run_record

    if not args.mock and not args.device:
        print("error: --device is required (use --mock to record synthetic data)", file=sys.stderr)
        return 1
    if args.mock and not args.device:
        args.device = "mock"
    return asyncio.run(run_record(args))


def cmd_report(args):
    try:
        return print_report(args.session_dir)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser():
    p = argparse.ArgumentParser(prog="linkband", description="Link Band 2.0 raw capture")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="list Link Band devices in range")
    s.add_argument("--timeout", type=float, default=8.0)
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("record", help="record a session to disk")
    r.add_argument("--device", help="device name (LXB-...) or BLE address")
    r.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    r.add_argument("--out", default="sessions", help="parent directory for session output")
    r.add_argument("--name", default=None, help="session directory name")
    r.add_argument("--mock", action="store_true", help="synthetic device with the real failure modes")
    r.add_argument(
        "--start-write",
        action="store_true",
        help="write b'start' to the EEG write characteristic (vendor GUI did this; firmware support UNKNOWN)",
    )
    r.set_defaults(func=cmd_record)

    p2 = sub.add_parser("report", help="audit a recorded session")
    p2.add_argument("session_dir")
    p2.set_defaults(func=cmd_report)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)
