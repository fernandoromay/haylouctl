
"""Command line interface: scan, survey, pair, bridge, gui, uninstall
(prog: haylouctl).

  scan      list advertising BLE devices sorted by RSSI; marks names that
            look like a Haylou/Solar.
  survey    connect to a watch (given MAC or auto-detected), dump the GATT
            table and classify the layout; exit 2 if unknown.
  pair      scan for Haylou devices, pick one, and save it to
            devices.paired (existing entries with the same address are
            replaced). Empty name falls back to the brand-stripped model.
  bridge    run the notification daemon — the entry point used by
            systemd's haylouctl.service. Address resolution order:
            argument, config devices.last, then a fresh scan.
  gui       open the GTK4 settings window (needs GTK4 bindings).
  uninstall stop the service and remove every file install.sh created;
            keeps ~/.config/haylouctl and says so.

bridge argument validation (min-interval >= 0, 0 < reconnect-delay <=
max-reconnect-delay) happens before anything touches BLE.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from . import bridge, config, layout, transport

SERVICE_NAME = "haylouctl.service"
CONFIG_DIR = Path.home() / ".config" / "haylouctl"
INSTALL_PATHS = (
    Path.home() / ".local" / "share" / "haylouctl",
    Path.home() / ".local" / "bin" / "haylouctl",
    Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME,
    Path.home() / ".local" / "share" / "applications" / "haylouctl.desktop",
    Path.home()
    / ".local"
    / "share"
    / "icons"
    / "hicolor"
    / "scalable"
    / "apps"
    / "haylouctl.svg",
)


def cmd_scan(args: argparse.Namespace) -> int:
    rows = asyncio.run(transport.scan(args.seconds))

    if not rows:
        print("no BLE devices found", file=sys.stderr)
        return 1

    for row in rows:
        mark = "*" if transport.looks_like_haylou(row.name) else " "
        print(f"{mark} {row.address}  {row.rssi:4d} dBm  {row.name}")

    return 0


def _prompt_name(default: str) -> str:
    """Ask for a device name, pre-filling `default` when readline allows it."""
    try:
        import readline
    except ImportError:
        try:
            answer = input("Give it a name: ").strip()
        except EOFError:
            return default
        return answer or default

    def prefill() -> None:
        readline.insert_text(default)

    readline.set_startup_hook(prefill)
    try:
        # Write the prompt ourselves: input(prompt) + redisplay() doubles it.
        sys.stdout.write("Name: ")
        sys.stdout.flush()
        answer = input().strip()
    except EOFError:
        return default
    finally:
        readline.set_startup_hook(None)

    return answer or default


def cmd_pair(args: argparse.Namespace) -> int:
    print("Scanning for Haylou devices...", flush=True)

    try:
        rows = asyncio.run(transport.scan(args.seconds))
    except Exception as error:
        print(f"scan failed: {error}", file=sys.stderr)
        return 1

    devices = [row for row in rows if transport.looks_like_haylou(row.name)]

    if not devices:
        print(
            "no Haylou devices found; is it advertising? "
            "(screen on, off the charger, phone Bluetooth off)",
            file=sys.stderr,
        )
        return 1

    if args.address is not None:
        address = args.address.casefold()
        selected = next(
            (row for row in devices if row.address.casefold() == address),
            None,
        )

        if selected is None:
            print(
                f"{args.address} is not advertising (or is not a Haylou)",
                file=sys.stderr,
            )
            return 1
    else:
        for index, row in enumerate(devices, start=1):
            print(f"{index}) {row.name}   {row.address}  {row.rssi:4d} dBm")

        try:
            raw = input("Select device [1]: ").strip()
        except EOFError:
            print("Aborted.", file=sys.stderr)
            return 1

        try:
            choice = int(raw) if raw else 1
        except ValueError:
            print("invalid selection", file=sys.stderr)
            return 2

        if not 1 <= choice <= len(devices):
            print("invalid selection", file=sys.stderr)
            return 2

        selected = devices[choice - 1]

    model = transport.strip_brand(selected.name)
    name = _prompt_name(model)

    try:
        config.save_paired_device(selected.address, model, name)
    except Exception as error:
        print(f"could not save device: {error}", file=sys.stderr)
        return 1

    print(f"Paired {name} at {selected.address}")
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


def _systemctl(*arguments: str) -> None:
    try:
        subprocess.run(
            ["systemctl", "--user", *arguments],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def cmd_uninstall(args: argparse.Namespace) -> int:
    existing = [path for path in INSTALL_PATHS if path.exists() or path.is_symlink()]

    if not existing:
        print("Nothing to uninstall.")
        return 0

    if not args.yes:
        try:
            answer = input(
                "Are you sure you want to remove Haylou Control? [y/N] "
            ).strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1

    _systemctl("disable", "--now", SERVICE_NAME)

    for path in existing:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as error:
            print(f"could not remove {path}: {error}", file=sys.stderr)

    _systemctl("daemon-reload")

    desktop_db = Path.home() / ".local" / "share" / "applications"
    if shutil.which("update-desktop-database"):
        subprocess.run(
            ["update-desktop-database", str(desktop_db)],
            capture_output=True,
            check=False,
        )

    print("Haylou Control was successfully uninstalled.")
    print(f"Your configuration was kept at {CONFIG_DIR}/")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="haylouctl")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="list advertising BLE devices")
    scan.add_argument("--seconds", type=float, default=10.0)
    scan.set_defaults(func=cmd_scan)

    pair = sub.add_parser(
        "pair", help="scan for Haylou devices and save one to the config"
    )
    pair.add_argument(
        "address",
        nargs="?",
        help="watch MAC; omit to pick from a list",
    )
    pair.add_argument("--seconds", type=float, default=10.0)
    pair.set_defaults(func=cmd_pair)

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

    uninstall = sub.add_parser(
        "uninstall", help="remove haylouctl from the system (keeps config)"
    )
    uninstall.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="skip the confirmation prompt",
    )
    uninstall.set_defaults(func=cmd_uninstall)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
