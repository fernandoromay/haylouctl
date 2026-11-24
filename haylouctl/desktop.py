
"""Desktop notification capture from the session bus.

notification_stream() spawns `dbus-monitor --session` filtered to
org.freedesktop.Notifications.Notify method calls and scrapes its text
output: each call arrives as a block of `type value` lines terminated by
the int32 expire_timeout line. parse_notify_call() pulls out the four
strings (app_name, app_icon, summary, body) via STRING_PATTERN.

Known limitation: dbus-monitor does not escape quotes inside strings, so a
body containing '"' (e.g. <a href="...">) is truncated at parse time.
href tags are rare in desktop notifications (~1-5%); accepted. A future
agent wanting full fidelity should parse via dbus-next instead of
scraping dbus-monitor text.

strip_markup() reduces freedesktop HTML to the plain text the watch can
render: <br>/block tags become newlines, remaining tags are dropped,
entities are unescaped AFTER stripping so a literal "&lt;b&gt;" survives
as "<b>". Applied to title and body at parse time, so logs and the watch
both see clean text.

collect_notifications() is the queue feeder: it drops filtered apps
(app name match, casefolded) and everything while DND is on, then queues.
"""

from __future__ import annotations

import ast
import asyncio
import html
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass


# dbus-monitor does not escape quotes inside strings, so a body containing
# '"' (e.g. <a href="...">) gets truncated at parse time. href tags are
# rare in desktop notifications (~1-5%); accepted limitation. A future
# agent wanting full fidelity should parse via dbus-next instead of
# scraping dbus-monitor text.
STRING_PATTERN = re.compile(r'\bstring\s+("(?:\\.|[^"\\])*")')
EXPIRE_PATTERN = re.compile(r"^\s*int32\s+-?\d+\s*$")
BREAK_PATTERN = re.compile(
    r"<(?:br|/p|/div|/li|/h[1-6]|/tr)\s*/?>",
    re.IGNORECASE,
)
TAG_PATTERN = re.compile(r"<[^>]+>")


def strip_markup(text: str) -> str:
    """Reduce a freedesktop notification body to plain text.

    The watch renders plain text only, so HTML tags are dropped and
    entities are unescaped. Tags are stripped before unescaping so a
    literal "&lt;b&gt;" survives as "<b>".
    """
    text = BREAK_PATTERN.sub("\n", text)
    text = TAG_PATTERN.sub("", text)
    return html.unescape(text).strip()


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

    title = strip_markup(title)
    body = strip_markup(body)

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
