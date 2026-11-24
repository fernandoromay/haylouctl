
from __future__ import annotations

import ast
import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass


STRING_PATTERN = re.compile(r'\bstring\s+("(?:\\.|[^"\\])*")')
EXPIRE_PATTERN = re.compile(r"^\s*int32\s+-?\d+\s*$")


@dataclass(frozen=True)
class DesktopNotification:
    app_name: str
    title: str
    body: str


def is_filtered(
    notification: DesktopNotification,
    filtered_apps: set[str],
) -> bool:
    return notification.app_name.strip().casefold() in filtered_apps


def parse_notify_call(block: list[str]) -> DesktopNotification | None:
    if not block or "member=Notify" not in block[0]:
        return None

    values: list[str] = []

    for line in block[1:]:
        match = STRING_PATTERN.search(line)
        if match is None:
            continue

        try:
            value = ast.literal_eval(match.group(1))
        except (SyntaxError, ValueError):
            continue

        if isinstance(value, str):
            values.append(value)

    # Notify arguments:
    # app_name, replaces_id, app_icon, summary, body, actions, hints,
    # expire_timeout.
    if len(values) < 4:
        print(
            f"DBUS PARSE ERROR: expected 4 strings, got {len(values)}",
            flush=True,
        )
        return None

    app_name, _app_icon, title, body = values[:4]

    if not title and not body:
        return None

    return DesktopNotification(
        app_name=app_name,
        title=title or app_name or "Notification",
        body=body,
    )


async def notification_stream() -> AsyncIterator[DesktopNotification]:
    process = await asyncio.create_subprocess_exec(
        "dbus-monitor",
        "--session",
        "type='method_call',interface='org.freedesktop.Notifications',member='Notify'",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    if process.stdout is None:
        raise RuntimeError("Could not read dbus-monitor output")

    print("D-Bus notification monitor started.", flush=True)

    block: list[str] = []

    try:
        async for raw_line in process.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip()

            if line.startswith("method call "):
                block = [line]
                continue

            if not block:
                continue

            block.append(line)

            if EXPIRE_PATTERN.match(line):
                notification = parse_notify_call(block)
                block = []

                if notification is not None:
                    print(
                        f"DBUS RECEIVED: {notification.app_name}: "
                        f"{notification.title} | {notification.body}",
                        flush=True,
                    )
                    yield notification

    finally:
        if process.returncode is None:
            process.terminate()

            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


async def collect_notifications(
    queue: asyncio.Queue[DesktopNotification],
    filtered_apps: set[str],
    dnd: bool,
) -> None:
    async for notification in notification_stream():
        if is_filtered(notification, filtered_apps):
            print(
                f"DBUS FILTERED: {notification.app_name}: "
                f"{notification.title}",
                flush=True,
            )
            continue

        if dnd:
            print(
                f"DBUS DND: {notification.app_name}: "
                f"{notification.title}",
                flush=True,
            )
            continue

        await queue.put(notification)
