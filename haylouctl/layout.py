"""GATT layout classification for `haylouctl survey`.

Three known BLE stacks found on the watches this project probes:

  SOLAR  Appscomm/Huawo stack: service 0x6006 with characteristics
         0x8001-0x8004. Haylou Solar Plus (RT3/LS16) and clones. The only
         stack with a working notification path here — everything else in
         this package targets it (protocol: reference/solar_plus.py).
  RT     ZeTime-style stack: service 0xfee7 or command 0x02ff.
  YOUCY  service 0x60ff or command 0x6001.

classify() checks SOLAR first (it needs both the service and at least one
channel), then RT, then YOUCY, else UNKNOWN. describe() renders the survey
GATT table plus the detected layout as text; cli exits 2 on UNKNOWN.
"""

from __future__ import annotations

from enum import Enum

from .transport import GattTable

RT_SUFFIX = "-3c17-d293-8e48-14fe2e4da212"
RT_COMMAND = "000002ff" + RT_SUFFIX
RT_SERVICE = "0000fee7-0000-1000-8000-00805f9b34fb"
YOUCY_COMMAND = "00006001-0000-1000-8000-00805f9b34fb"
YOUCY_SERVICE = "000060ff-0000-1000-8000-00805f9b34fb"
OTA_SERVICE = "0000d0ff" + RT_SUFFIX
SOLAR_SERVICE = "00006006-0000-1000-8000-00805f9b34fb"
SOLAR_CHANNELS = tuple("0000800%d-0000-1000-8000-00805f9b34fb" % i for i in (1, 2, 3, 4))


class Layout(str, Enum):
    RT = "rt"
    YOUCY = "youcy"
    SOLAR = "solar"
    UNKNOWN = "unknown"


def classify(table: GattTable) -> Layout:
    uuids = table.uuids()
    if SOLAR_SERVICE in uuids and any(uuid in uuids for uuid in SOLAR_CHANNELS):
        return Layout.SOLAR
    if RT_COMMAND in uuids or RT_SERVICE in uuids:
        return Layout.RT
    if YOUCY_COMMAND in uuids or YOUCY_SERVICE in uuids:
        return Layout.YOUCY
    return Layout.UNKNOWN


def describe(table: GattTable, layout: Layout) -> str:
    lines = [f"device  {table.address}", f"layout  {layout.value}", ""]
    for service in table.services:
        lines.append(f"service {service.uuid}")
        for char in service.characteristics:
            props = ",".join(char.properties)
            lines.append(f"    {char.uuid}  ({props})")
            if char.value is not None:
                lines.append(f"        value: {char.value}")
        lines.append("")
    return "\n".join(lines)
