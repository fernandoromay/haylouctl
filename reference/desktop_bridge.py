#!/usr/bin/env python3
"""Forward Linux desktop notifications to a Haylou Solar Plus over BLE.

Requires: bleak, dbus-monitor, and a running desktop session bus.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import datetime as dt
import re
import shutil
import signal
import struct
import time
from collections.abc import AsyncIterator

ADDRESS = "11:08:07:22:01:00"
SERVICE_UUID = "00006006-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "00008001-0000-1000-8000-00805f9b34fb"
ACK_UUID = "00008002-0000-1000-8000-00805f9b34fb"

# Haylou Fun SocialType values verified with the Solar Plus:
# SMS=1, Social=2, Email=3, Calendar=4.
DEFAULT_MESSAGE_TYPE = 2

STRING_RE = re.compile(r'^\s*string\s+("(?:\\.|[^"\\])*")\s*$')


def _utf8_limit(value: str, max_bytes: int) -> bytes:
    """Truncate on a UTF-8 character boundary."""
    return value.encode("utf-8")[:max_bytes].decode("utf-8", "ignore").encode("utf-8")


def _tlv(tag: int, value: bytes) -> bytes:
    # uint16 LE length includes the one-byte tag.
    return struct.pack("<H", len(value) + 1) + bytes((tag,)) + value


def build_notification_frame(
    title: str,
    body: str,
    *,
    message_type: int = DEFAULT_MESSAGE_TYPE,
    message_id: int | None = None,
    timestamp: dt.datetime | None = None,
) -> bytes:
    """Build the tested 0x79 legacy notification frame."""
    if message_type not in (1, 2, 3, 4):
        raise ValueError("message_type must be 1 (SMS), 2 (Social), 3 (Email), or 4 (Calendar)")

    title_bytes = _utf8_limit(title, 90)
    body_bytes = _utf8_limit(body, 357)
    if timestamp is None:
        timestamp = dt.datetime.now().astimezone()
    timestamp_bytes = timestamp.strftime("%Y%m%dT%H%M%S").encode("ascii")
    if message_id is None:
        message_id = int(time.time() * 1000) & 0xFFFFFFFF

    title_field = _tlv(0x02, title_bytes)
    body_field = _tlv(0x03, body_bytes)
    timestamp_field = _tlv(0x04, timestamp_bytes)

    inner_length = 12 + len(title_field) + len(body_field) + len(timestamp_field)
    allocated_body_length = inner_length + 2

    payload = (
        struct.pack("<H", inner_length)
        + struct.pack("<I", message_id & 0xFFFFFFFF)
        + b"\x00"
        + bytes((0x02, 0x00, 0x01, message_type))
        + title_field
        + body_field
        + timestamp_field
        + b"\x00\x00\x00"
    )
    if len(payload) != allocated_body_length:
        raise AssertionError(
            f"SDK frame length mismatch: payload={len(payload)}, expected={allocated_body_length}"
        )

    return (
        b"\x6f\x79\x71"
        + struct.pack("<H", allocated_body_length)
        + payload
        + b"\x8f"
    )


def _parse_dbus_notify(block: str) -> tuple[str, str] | None:
    """Extract summary/body from one dbus-monitor Notify method-call block."""
    if "member=Notify" not in block or "interface=org.freedesktop.Notifications" not in block:
        return None

    strings: list[str] = []
    for line in block.splitlines():
        match = STRING_RE.match(line)
        if match:
            try:
                strings.append(ast.literal_eval(match.group(1)))
            except (SyntaxError, ValueError):
                continue

    # Notify(s app_name, u replaces_id, s app_icon, s summary, s body, ...)
    if len(strings) < 4:
        return None
    summary, body = strings[2], strings[3]
    summary = summary.strip()
    body = body.strip()
    if not summary and not body:
        return None
    return summary or "Desktop notification", body or summary


async def dbus_notifications() -> AsyncIterator[tuple[str, str]]:
    """Monitor org.freedesktop.Notifications.Notify calls on the session bus."""
    command = [
        "dbus-monitor",
        "--session",
        "type='method_call',interface='org.freedesktop.Notifications',member='Notify'",
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None

    block: list[str] = []
    try:
        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("method call "):
                block = [line]
            elif block:
                if line:
                    block.append(line)
                else:
                    parsed = _parse_dbus_notify("\n".join(block))
                    if parsed:
                        yield parsed
                    block = []
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


async def run(args: argparse.Namespace) -> None:
    from bleak import BleakClient

    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    ack_count = 0

    def on_ack(_sender: int, data: bytearray) -> None:
        nonlocal ack_count
        ack_count += 1
        print(f"Watch ACK: {bytes(data).hex(' ')}", flush=True)

    async with BleakClient(args.address, timeout=25) as client:
        write_char = client.services.get_characteristic(WRITE_UUID)
        ack_char = client.services.get_characteristic(ACK_UUID)
        if write_char is None or ack_char is None:
            raise RuntimeError("Solar Plus GATT characteristics 8001/8002 were not found")
        if "write-without-response" not in write_char.properties:
            raise RuntimeError(f"8001 does not support write-without-response: {write_char.properties}")

        await client.start_notify(ACK_UUID, on_ack)
        print(f"Connected to Haylou Solar Plus ({args.address})", flush=True)
        print("Monitoring Linux desktop notifications. Press Ctrl+C to stop.", flush=True)
        print(f"Minimum interval between watch pushes: {args.min_interval:g}s", flush=True)

        async def collect() -> None:
            async for title, body in dbus_notifications():
                # Ignore empty and self-generated bridge messages.
                if title.lower().startswith("[haylou bridge]"):
                    continue
                await queue.put((title, body))
                print(f"Queued: {title}", flush=True)

        async def send_loop() -> None:
            last_sent = 0.0
            while True:
                title, body = await queue.get()
                try:
                    wait = args.min_interval - (time.monotonic() - last_sent)
                    if last_sent and wait > 0:
                        await asyncio.sleep(wait)

                    frame = build_notification_frame(
                        title,
                        body,
                        message_type=args.message_type,
                    )
                    before = ack_count
                    await client.write_gatt_char(WRITE_UUID, frame, response=False)
                    last_sent = time.monotonic()
                    print(
                        f"Sent: {title!r} ({len(frame)} bytes; "
                        f"body={len(body.encode('utf-8'))} UTF-8 bytes)",
                        flush=True,
                    )
                    # The watch's 8002 ACK is asynchronous; do not write 0x03
                    # back to 8002 (that characteristic is notify-only).
                    try:
                        await asyncio.sleep(args.ack_wait)
                    finally:
                        if ack_count == before:
                            print("No application ACK observed for this push.", flush=True)
                except Exception as exc:
                    print(f"BLE send failed for {title!r}: {exc}", flush=True)
                finally:
                    queue.task_done()

        monitor_task = asyncio.create_task(collect())
        sender_task = asyncio.create_task(send_loop())
        try:
            await monitor_task
        finally:
            sender_task.cancel()
            await asyncio.gather(sender_task, return_exceptions=True)
            await client.stop_notify(ACK_UUID)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default=ADDRESS)
    parser.add_argument(
        "--message-type",
        type=int,
        choices=(1, 2, 3, 4),
        default=DEFAULT_MESSAGE_TYPE,
        help="Watch notification category: 1=SMS, 2=Social, 3=Email, 4=Calendar",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=8.0,
        help="Minimum seconds between pushes (default: 8; adjust to test vibration cooldown)",
    )
    parser.add_argument("--ack-wait", type=float, default=0.5)
    args = parser.parse_args()

    if shutil.which("dbus-monitor") is None:
        parser.error("dbus-monitor is required (usually provided by the dbus package)")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
