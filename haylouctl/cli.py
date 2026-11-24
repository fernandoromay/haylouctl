
from __future__ import annotations

import argparse
import asyncio
import sys

from . import bridge, config, layout, transport


def cmd_scan(args: argparse.Namespace) -> int:
    rows = asyncio.run(transport.scan(args.seconds))

    if not rows:
        print("no BLE devices found", file=sys.stderr)
        return 1

    for row in rows:
        mark = "*" if transport.looks_like_haylou(row.name) else " "
        print(f"{mark} {row.address}  {row.rssi:4d} dBm  {row.name}")

    return 0


def cmd_survey(args: argparse.Namespace) -> int:
    address = args.address

    if address is None:
        try:
            found = asyncio.run(transport.find_haylou())
        except Exception as error:
            print(f"scan failed: {error}", file=sys.stderr)
            return 1

        if found is None:
            print(
                "no Haylou found; is it advertising? "
                "(screen on, off the charger, phone Bluetooth off)",
                file=sys.stderr,
            )
            return 1

        address = found.address
        print(f"# using {found.address} {found.name}", file=sys.stderr)

    try:
        table = asyncio.run(transport.survey(address, read=not args.no_read))
    except Exception as error:
        print(f"connect failed: {error}", file=sys.stderr)
        print(
            "is the watch connected to a phone? turn that phone's Bluetooth off",
            file=sys.stderr,
        )
        return 1

    found_layout = layout.classify(table)
    print(layout.describe(table, found_layout))

    if found_layout is layout.Layout.UNKNOWN:
        print(
            "WARNING: matches neither known layout "
            f"(missing {layout.RT_COMMAND} and {layout.YOUCY_COMMAND})",
            file=sys.stderr,
        )
        return 2

    return 0


async def _run_bridge(args: argparse.Namespace) -> None:
    address = args.address

    if address is None:
        address = config.get_startup_device()

    if address is None:
        found = await transport.find_haylou()

        if found is None:
            raise RuntimeError("No Haylou device found")

        address = found.address
        print(f"Using {found.name} at {address}", flush=True)

    print(f"Starting notification bridge for {address}", flush=True)

    await bridge.forward_notifications(
        address,
        min_interval=args.min_interval,
        message_type=args.message_type,
        reconnect_delay=args.reconnect_delay,
        max_reconnect_delay=args.max_reconnect_delay,
    )


def cmd_bridge(args: argparse.Namespace) -> int:
    if args.min_interval is not None and args.min_interval < 0:
        print("--min-interval cannot be negative", file=sys.stderr)
        return 2

    if args.reconnect_delay <= 0:
        print("--reconnect-delay must be greater than zero", file=sys.stderr)
        return 2

    if args.max_reconnect_delay < args.reconnect_delay:
        print(
            "--max-reconnect-delay cannot be less than --reconnect-delay",
            file=sys.stderr,
        )
        return 2

    try:
        asyncio.run(_run_bridge(args))
    except KeyboardInterrupt:
        print("\nBridge stopped.", flush=True)
        return 130
    except Exception as error:
        print(f"bridge failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    return 0


def cmd_gui(_args: argparse.Namespace) -> int:
    try:
        from .gui import main as gui_main
    except ImportError as error:
        print(
            f"Could not load GTK interface: {error}\n"
            "Install GTK4 and its Python bindings.",
            file=sys.stderr,
        )
        return 1

    return gui_main()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="haylouctl")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="list advertising BLE devices")
    scan.add_argument("--seconds", type=float, default=10.0)
    scan.set_defaults(func=cmd_scan)

    survey = sub.add_parser(
        "survey", help="connect and dump the GATT table, then classify the layout"
    )
    survey.add_argument(
        "address",
        nargs="?",
        help="watch MAC; omit to auto-detect a Haylou",
    )
    survey.add_argument(
        "--no-read",
        action="store_true",
        help="skip reading readable characteristics",
    )
    survey.set_defaults(func=cmd_survey)

    bridge_parser = sub.add_parser(
        "bridge",
        help="forward Linux desktop notifications to a Solar-compatible watch",
    )
    bridge_parser.add_argument(
        "address",
        nargs="?",
        help="watch MAC; omit to use the last configured device",
    )
    bridge_parser.add_argument(
        "--min-interval",
        type=float,
        default=None,
        help="minimum seconds between notifications (overrides config)",
    )
    bridge_parser.add_argument(
        "--message-type",
        type=int,
        choices=(1, 2, 3, 4),
        default=2,
        help="watch category: 1 SMS, 2 Social, 3 Email, 4 Calendar",
    )
    bridge_parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=5.0,
        help="initial BLE reconnect delay in seconds (default: 5)",
    )
    bridge_parser.add_argument(
        "--max-reconnect-delay",
        type=float,
        default=60.0,
        help="maximum BLE reconnect delay in seconds (default: 60)",
    )
    bridge_parser.set_defaults(func=cmd_bridge)

    gui = sub.add_parser("gui", help="open the GTK desktop interface")
    gui.set_defaults(func=cmd_gui)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
