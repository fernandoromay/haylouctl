#!/usr/bin/env python3
"""REFERENCE: Haylou Solar Plus (RT3/LS16) notification push, legacy Huawo 0x79 protocol.

Unified from the three working test scripts (v1, v2, batch) and matching the
production builders in reference/desktop_bridge.py and
haylouctl/transport.py. This is the authoritative description of how a
notification frame is built and delivered.

=============================================================================
1. GATT layout (vendor service 0x6006)
=============================================================================
  00006006-0000-1000-8000-00805f9b34fb   vendor service
    00008001-...  write-without-response   COMMAND channel (we write frames here)
    00008002-...  notify                   ACK channel (watch replies here)
    00008003-...  write-without-response   reply-only, unused for pushes
    00008004-...  notify                   async pushes (weather etc.), unused

8002 is NOTIFY-ONLY. Never write to it. The early tests sent a 0x03 "ACK kick"
to 8002; that is wrong and production deliberately does not do it. Just
subscribe to 8002 and read the watch's asynchronous reply.

=============================================================================
2. Frame anatomy (PushMessage.sendBytesLegacy, from Haylou Fun APK)
=============================================================================
  6F 79 71 | LEN(2,LE) | BODY | 8F
  ^prefix  ^  outer length = len(BODY) = inner_length + 2

  BODY:
    inner_length (2,LE)      = 12 + len(title TLV) + len(body TLV) + len(time TLV)
    message_id   (4,LE)      = unique id (production uses epoch milliseconds)
    00                       = reserved
    02 00 01 <message_type>  = type block; message_type = SocialType (see below)
    title TLV
    body TLV
    time TLV
    00 00 00                 = three reserved zero bytes from the SDK buffer

  TLV:  LEN(2,LE) | TAG(1) | DATA
        LEN counts tag + data (i.e. len(DATA) + 1).
        Tags: 0x02 = title, 0x03 = content, 0x04 = timestamp.
        (v1 wrongly used 0x01 for the title; v2 fixed it to 0x02.)

  Timestamp: local time formatted "%Y%m%dT%H%M%S"
             (SDK does SimpleDateFormat "yyyyMMdd HHmmss" then ' ' -> 'T').

  Limits enforced by the SDK: title <= 90 UTF-8 bytes, content <= 357 UTF-8
  bytes. Truncate on a UTF-8 character boundary, never mid-codepoint.

=============================================================================
3. SocialType (message_type byte)
=============================================================================
  1 = SMS        2 = Social      3 = Email      4 = Calendar
  Production default: 2 (Social), which is what a generic desktop
  notification maps to.

=============================================================================
4. Delivery sequence
=============================================================================
  1. Connect (BLE, ~20 s timeout; watch must be bonded/paired on this host).
  2. start_notify(8002) and keep the callback printing raw hex.
  3. write_gatt_char(8001, frame, response=False)   # write WITHOUT response
  4. Wait for the watch's 8002 notification (application ACK, a few hundred
     ms to a few seconds). Do not write anything back.
  5. Batch pushes need a delay between them (the watch vibrates/coalesces;
     production uses a min-interval of ~8 s).

=============================================================================
5. Usage
=============================================================================
  python3 reference/solar_plus.py \
      --title "Linux test" --content "Hello" --type 2

  python3 reference/solar_plus.py --batch
      # sends one push of each SocialType: SMS, Social, Email, Calendar
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import struct
import time

ADDRESS = "11:08:07:22:01:00"
SERVICE_UUID = "00006006-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "00008001-0000-1000-8000-00805f9b34fb"
ACK_UUID = "00008002-0000-1000-8000-00805f9b34fb"

# Haylou Fun SocialType enum, verified against the watch.
SOCIAL_TYPES = {
    1: "SMS",
    2: "Social",
    3: "Email",
    4: "Calendar",
}
DEFAULT_MESSAGE_TYPE = 2  # Social: generic desktop notification

# SDK-imposed field limits, in UTF-8 bytes.
TITLE_LIMIT = 90
CONTENT_LIMIT = 357


def _utf8_limit(value: str, max_bytes: int) -> bytes:
    """Encode and truncate on a UTF-8 character boundary."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return encoded
    return encoded[:max_bytes].decode("utf-8", "ignore").encode("utf-8")


def _tlv(tag: int, value: bytes) -> bytes:
    """SDK TLV: uint16-LE length of (tag + data), then tag, then data."""
    return struct.pack("<H", len(value) + 1) + bytes((tag,)) + value


def build_frame(
    title: str,
    content: str,
    *,
    message_type: int = DEFAULT_MESSAGE_TYPE,
    message_id: int | None = None,
    timestamp: dt.datetime | None = None,
) -> bytes:
    """Build a 0x79 legacy notification frame (see module docstring).

    This is byte-for-byte the same builder as production
    (reference/desktop_bridge.build_notification_frame and
    haylouctl/transport.build_solar_packet).
    """
    if message_type not in SOCIAL_TYPES:
        raise ValueError("message_type must be 1 (SMS), 2 (Social), 3 (Email) or 4 (Calendar)")

    title_bytes = _utf8_limit(title, TITLE_LIMIT)
    content_bytes = _utf8_limit(content, CONTENT_LIMIT)

    if timestamp is None:
        timestamp = dt.datetime.now().astimezone()
    timestamp_bytes = timestamp.strftime("%Y%m%dT%H%M%S").encode("ascii")

    if message_id is None:
        message_id = int(time.time() * 1000) & 0xFFFFFFFF
    message_id &= 0xFFFFFFFF

    title_field = _tlv(0x02, title_bytes)
    content_field = _tlv(0x03, content_bytes)
    time_field = _tlv(0x04, timestamp_bytes)

    # SDK allocates 12 + TLV sizes for inner_length; the body buffer it
    # writes is two bytes larger because it starts with inner_length itself.
    inner_length = 12 + len(title_field) + len(content_field) + len(time_field)
    body_length = inner_length + 2

    body = (
        struct.pack("<H", inner_length)
        + struct.pack("<I", message_id)
        + b"\x00"
        + bytes((0x02, 0x00, 0x01, message_type))
        + title_field
        + content_field
        + time_field
        + b"\x00\x00\x00"
    )
    assert len(body) == body_length, (len(body), body_length)

    # Outer header length is the allocated body length, not inner_length.
    frame = b"\x6f\x79\x71" + struct.pack("<H", body_length) + body + b"\x8f"
    assert len(frame) == body_length + 6
    return frame


async def _send_once(
    client,  # bleak.BleakClient
    frame: bytes,
    *,
    ack_timeout: float,
) -> bool:
    """Write one frame to 8001 and wait for the watch's 8002 ACK."""
    ack_event = asyncio.Event()
    ack_payload: dict[str, bytes] = {}

    def on_ack(_sender, data: bytearray) -> None:
        ack_payload["raw"] = bytes(data)
        ack_event.set()

    await client.start_notify(ACK_UUID, on_ack)
    try:
        ack_event.clear()
        await client.write_gatt_char(WRITE_UUID, frame, response=False)
        try:
            await asyncio.wait_for(ack_event.wait(), timeout=ack_timeout)
        except asyncio.TimeoutError:
            print("  no 8002 notification (frame was written)", flush=True)
            return False
        print(f"  8002 ACK: {ack_payload['raw'].hex(' ')}", flush=True)
        return True
    finally:
        try:
            await client.stop_notify(ACK_UUID)
        except Exception:  # noqa: BLE001
            pass


async def run(args: argparse.Namespace) -> None:
    from bleak import BleakClient

    if args.batch:
        tests = [
            (kind, f"Linux {name}", f"Solar Plus reference test: {name}")
            for kind, name in SOCIAL_TYPES.items()
        ]
    else:
        tests = [(args.type, args.title, args.content)]

    print(f"Connecting to {args.address} ...", flush=True)
    async with BleakClient(args.address, timeout=20) as client:
        service = client.services.get_service(SERVICE_UUID)
        write_char = client.services.get_characteristic(WRITE_UUID)
        ack_char = client.services.get_characteristic(ACK_UUID)
        if service is None or write_char is None or ack_char is None:
            raise RuntimeError("Expected GATT service 6006 and characteristics 8001/8002")
        if "write-without-response" not in write_char.properties:
            raise RuntimeError(f"8001 lacks write-without-response: {write_char.properties}")
        if "notify" not in ack_char.properties:
            raise RuntimeError(f"8002 lacks notify: {ack_char.properties}")

        print(f"Connected. 8001 properties: {write_char.properties}", flush=True)

        base_id = int(time.time() * 1000)
        for index, (message_type, title, content) in enumerate(tests, start=1):
            frame = build_frame(
                title,
                content,
                message_type=message_type,
                message_id=base_id + index,
            )
            label = SOCIAL_TYPES[message_type]
            print(
                f"\n[{index}/{len(tests)}] type={message_type} ({label}) "
                f"title={title!r} frame={len(frame)} bytes",
                flush=True,
            )
            print(f"  {frame.hex(' ')}", flush=True)
            await _send_once(client, frame, ack_timeout=args.ack_timeout)

            if index < len(tests):
                await asyncio.sleep(args.delay)

    print("\nDone. Check which notifications appeared on the watch.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reference test for Haylou Solar Plus 0x79 notification pushes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
        "  %(prog)s --title 'Linux test' --content 'Hello' --type 2\n"
        "  %(prog)s --batch            # SMS, Social, Email, Calendar in sequence\n",
    )
    parser.add_argument("--address", default=ADDRESS, help="watch BLE address")
    parser.add_argument("--title", default="Linux test", help="notification title (<=90 UTF-8 bytes)")
    parser.add_argument("--content", default="Test", help="notification content (<=357 UTF-8 bytes)")
    parser.add_argument(
        "--type",
        type=int,
        default=DEFAULT_MESSAGE_TYPE,
        choices=tuple(SOCIAL_TYPES),
        help="SocialType: 1=SMS 2=Social 3=Email 4=Calendar (default 2)",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="send one push of each SocialType instead of a single notification",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.5,
        help="seconds between pushes in batch mode (default 2.5)",
    )
    parser.add_argument(
        "--ack-timeout",
        type=float,
        default=4.0,
        help="seconds to wait for the 8002 ACK (default 4)",
    )
    args = parser.parse_args()

    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
