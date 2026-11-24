
"""BLE transport: device discovery, GATT survey, and Solar notification pushes.

Device detection scans for advertisements whose name contains "haylou" or
"solar". survey() connects, reads every readable characteristic and returns
the full GATT table for layout classification (see layout.py).

Notification path (Solar/Appscomm stack, details in reference/solar_plus.py):
  service 0x6006
    8001  write-without-response   command channel (frames are written here)
    8002  notify                   async acks from the watch; NEVER write to it
    8003  write-without-response   reply-only, unused
    8004  notify                   async pushes, unused

solar_connection() is the context manager used by the bridge: it connects
(20 s timeout), validates 6006/8001/8002, subscribes to 8002 and yields
(client, write_char). An optional disconnected_event is set by bleak when
the link drops so the send loop can react immediately.

build_solar_packet() encodes the legacy 0x79 frame:
  6F 79 71 | len(2,LE) | body | 8F
  body = inner_len(2,LE) | message_id(4,LE) | 00 | 02 00 01 <type>
         | title TLV | content TLV | time TLV | 00 00 00
  TLV  = len(2,LE counts tag+data) | tag | data
         tags: 02 title, 03 content, 04 timestamp (%Y%m%dT%H%M%S local)
  limits: title <=90, content <=357 UTF-8 bytes, truncated on a character
  boundary exactly like the Haylou Fun SDK.
  SocialType: 1 SMS, 2 Social, 3 Email, 4 Calendar (default 2).

Frames are written with response=False (write-without-response); the watch
acknowledges asynchronously on 8002. The early experiments sent a 0x03
"kick" to 8002 — that is wrong, 8002 is notify-only.
"""

from __future__ import annotations

import asyncio
import struct
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

NAME_HINTS = ("haylou", "solar")
CONNECT_TIMEOUT = 20.0

SOLAR_SERVICE = "00006006-0000-1000-8000-00805f9b34fb"
SOLAR_WRITE = "00008001-0000-1000-8000-00805f9b34fb"
SOLAR_ACK = "00008002-0000-1000-8000-00805f9b34fb"

SOLAR_ACK_PACKET = bytes.fromhex("6f 01 81 02 00 79 00 8f")


@dataclass(frozen=True)
class Advertisement:
    address: str
    name: str
    rssi: int
    manufacturer: dict[int, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class Characteristic:
    uuid: str
    properties: tuple[str, ...]
    value: str | None = None


@dataclass(frozen=True)
class Service:
    uuid: str
    characteristics: tuple[Characteristic, ...]


@dataclass(frozen=True)
class GattTable:
    address: str
    services: tuple[Service, ...]

    def uuids(self) -> set[str]:
        found = {service.uuid for service in self.services}
        for service in self.services:
            found.update(char.uuid for char in service.characteristics)
        return found


def looks_like_haylou(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in NAME_HINTS)


async def scan(seconds: float = 10.0) -> list[Advertisement]:
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows: list[Advertisement] = []

    for device, advert in found.values():
        rows.append(
            Advertisement(
                address=device.address,
                name=advert.local_name or device.name or "?",
                rssi=advert.rssi,
                manufacturer=dict(advert.manufacturer_data),
            )
        )

    rows.sort(key=lambda row: -row.rssi)
    return rows


async def find_haylou(seconds: float = 15.0) -> Advertisement | None:
    rows = await scan(seconds)
    for row in rows:
        if looks_like_haylou(row.name):
            return row
    return None


async def survey(address: str, read: bool = True) -> GattTable:
    async with BleakClient(address, timeout=CONNECT_TIMEOUT) as client:
        services: list[Service] = []

        for service in client.services:
            characteristics: list[Characteristic] = []

            for char in service.characteristics:
                value: str | None = None

                if read and "read" in char.properties:
                    try:
                        raw = await client.read_gatt_char(char)
                        value = raw.hex(" ")
                    except Exception:
                        value = None

                characteristics.append(
                    Characteristic(char.uuid, tuple(char.properties), value)
                )

            services.append(Service(service.uuid, tuple(characteristics)))

        return GattTable(address=address, services=tuple(services))


TITLE_LIMIT = 90
CONTENT_LIMIT = 357


def _utf8_limit(value: str, max_bytes: int) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return encoded
    return encoded[:max_bytes].decode("utf-8", "ignore").encode("utf-8")


def _solar_tlv(tag: int, value: bytes) -> bytes:
    length = len(value) + 1

    if length > 0xFFFF:
        raise ValueError("notification field is too long")

    return struct.pack("<H", length) + bytes((tag,)) + value


def build_solar_packet(
    title: str,
    content: str,
    message_type: int = 2,
    message_id: int | None = None,
    timestamp: datetime | None = None,
) -> bytes:
    if message_type not in (1, 2, 3, 4):
        raise ValueError("message_type must be 1, 2, 3, or 4")

    if message_id is None:
        message_id = int(time.time())

    if not 0 <= message_id <= 0xFFFFFFFF:
        raise ValueError("message_id must fit in 4 bytes")

    timestamp = timestamp or datetime.now()
    timestamp_text = timestamp.strftime("%Y%m%dT%H%M%S")

    fields = (
        _solar_tlv(2, _utf8_limit(title, TITLE_LIMIT))
        + _solar_tlv(3, _utf8_limit(content, CONTENT_LIMIT))
        + _solar_tlv(4, timestamp_text.encode("utf-8"))
    )

    inner_length = 12 + len(fields)
    body_length = inner_length + 2

    if body_length > 0xFFFF:
        raise ValueError("notification packet is too large")

    body = (
        struct.pack("<H", inner_length)
        + struct.pack("<I", message_id)
        + b"\x00"
        + bytes((0x02, 0x00, 0x01, message_type))
        + fields
        + b"\x00\x00\x00"
    )

    return (
        b"\x6f\x79\x71"
        + struct.pack("<H", body_length)
        + body
        + b"\x8f"
    )


@asynccontextmanager
async def solar_connection(
    address: str,
    disconnected_event: asyncio.Event | None = None,
) -> AsyncIterator[tuple[BleakClient, BleakGATTCharacteristic]]:
    """
    Connect to the Solar-compatible device and validate its notification GATT.

    If supplied, disconnected_event is set by Bleak when the connection drops.
    """
    if disconnected_event is not None:
        disconnected_event.clear()

    def on_disconnect(_client: BleakClient) -> None:
        if disconnected_event is not None:
            disconnected_event.set()

    async with BleakClient(
        address,
        timeout=CONNECT_TIMEOUT,
        disconnected_callback=on_disconnect,
    ) as client:
        service = client.services.get_service(SOLAR_SERVICE)

        if service is None:
            raise RuntimeError(f"Solar service not found: {SOLAR_SERVICE}")

        write_char = service.get_characteristic(SOLAR_WRITE)
        ack_char = service.get_characteristic(SOLAR_ACK)

        if write_char is None or ack_char is None:
            raise RuntimeError("Solar notification characteristics are missing")

        if "write-without-response" not in write_char.properties:
            raise RuntimeError("8001 does not support write-without-response")

        if "notify" not in ack_char.properties:
            raise RuntimeError("8002 does not support notifications")

        def on_ack(_sender: int, data: bytearray) -> None:
            # 8002 is notify-only. Never write to this characteristic.
            if bytes(data) == SOLAR_ACK_PACKET:
                return

        await client.start_notify(ack_char, on_ack)

        try:
            if not client.is_connected:
                raise RuntimeError("BLE disconnected while enabling notifications")

            if disconnected_event is not None and disconnected_event.is_set():
                raise RuntimeError("BLE disconnected while enabling notifications")

            yield client, write_char

        finally:
            if client.is_connected:
                with suppress(Exception):
                    await client.stop_notify(ack_char)


async def send_solar_notification(
    client: BleakClient,
    write_char: BleakGATTCharacteristic,
    title: str,
    content: str,
    message_type: int = 2,
    message_id: int | None = None,
) -> None:
    if not client.is_connected:
        raise RuntimeError("BLE device is not connected")

    packet = build_solar_packet(
        title=title,
        content=content,
        message_type=message_type,
        message_id=message_id,
    )

    await client.write_gatt_char(write_char, packet, response=False)
