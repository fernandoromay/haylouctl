
from __future__ import annotations

import asyncio
from collections.abc import Callable

from . import config, desktop, transport


INITIAL_RECONNECT_DELAY = 5.0
MAX_RECONNECT_DELAY = 60.0

StatusCallback = Callable[[str], None]


def _status(callback: StatusCallback | None, message: str) -> None:
    print(message, flush=True)
    if callback is not None:
        callback(message)


async def _send_loop(
    address: str,
    queue: asyncio.Queue[desktop.DesktopNotification],
    min_interval: float,
    message_type: int,
    reconnect_delay: float,
    max_reconnect_delay: float,
    auto_reconnect: bool,
    status_callback: StatusCallback | None,
) -> None:
    loop = asyncio.get_running_loop()
    last_sent = 0.0
    message_id = 0
    pending: desktop.DesktopNotification | None = None
    delay = reconnect_delay
    connected_once = False

    while True:
        disconnected = asyncio.Event()

        try:
            async with transport.solar_connection(
                address,
                disconnected_event=disconnected,
            ) as (client, write_char):

                _status(status_callback, f"BLE CONNECTED: {address}")
                config.save_last_device(address)

                connected_once = True
                delay = reconnect_delay

                while client.is_connected and not disconnected.is_set():
                    if pending is None:
                        try:
                            pending = await asyncio.wait_for(
                                queue.get(),
                                timeout=1.0,
                            )
                        except asyncio.TimeoutError:
                            continue

                    if disconnected.is_set() or not client.is_connected:
                        break

                    wait = min_interval - (loop.time() - last_sent)

                    if wait > 0:
                        try:
                            await asyncio.wait_for(
                                disconnected.wait(),
                                timeout=wait,
                            )
                            break
                        except asyncio.TimeoutError:
                            pass

                    if disconnected.is_set() or not client.is_connected:
                        break

                    message_id = (message_id + 1) & 0xFFFFFFFF

                    _status(
                        status_callback,
                        f"BLE SENDING: {pending.title} | {pending.body}",
                    )

                    try:
                        await transport.send_solar_notification(
                            client,
                            write_char,
                            pending.title,
                            pending.body,
                            message_type=message_type,
                            message_id=message_id,
                        )
                    except Exception as error:
                        _status(
                            status_callback,
                            f"BLE SEND FAILED: {type(error).__name__}: {error}",
                        )
                        break

                    last_sent = loop.time()

                    _status(status_callback, "BLE SENT successfully.")

                    queue.task_done()
                    pending = None

                if disconnected.is_set() or not client.is_connected:
                    _status(status_callback, "BLE DISCONNECTED.")

        except asyncio.CancelledError:
            raise

        except Exception as error:
            _status(
                status_callback,
                f"BLE CONNECTION FAILED: {type(error).__name__}: {error}",
            )

        if connected_once and not auto_reconnect:
            _status(
                status_callback,
                "BLE AUTO-RECONNECT DISABLED. Stopping connection attempts.",
            )
            return

        _status(
            status_callback,
            f"BLE RECONNECT: retrying in {delay:g}s.",
        )

        await asyncio.sleep(delay)
        delay = min(delay * 2, max_reconnect_delay)


async def forward_notifications(
    address: str,
    min_interval: float | None = None,
    message_type: int = 2,
    reconnect_delay: float = INITIAL_RECONNECT_DELAY,
    max_reconnect_delay: float = MAX_RECONNECT_DELAY,
    status_callback: StatusCallback | None = None,
) -> None:
    if min_interval is not None and min_interval < 0:
        raise ValueError("min_interval cannot be negative")

    if reconnect_delay <= 0:
        raise ValueError("reconnect_delay must be greater than zero")

    if max_reconnect_delay < reconnect_delay:
        raise ValueError(
            "max_reconnect_delay cannot be less than reconnect_delay"
        )

    settings = config.load_config()
    notification_settings = settings["notifications"]
    device_settings = settings["devices"]

    if min_interval is None:
        min_interval = notification_settings["min-interval"]

    filtered_apps = {
        name.casefold()
        for name in notification_settings["filter"]
    }

    dnd = notification_settings["dnd"]
    auto_reconnect = device_settings["auto-reconnect"]

    _status(
        status_callback,
        f"Notification filter: {len(filtered_apps)} application(s)",
    )
    _status(
        status_callback,
        f"DND: {'enabled' if dnd else 'disabled'}",
    )
    _status(
        status_callback,
        f"Automatic reconnection: {'enabled' if auto_reconnect else 'disabled'}",
    )

    queue: asyncio.Queue[desktop.DesktopNotification] = asyncio.Queue()

    collector_task = asyncio.create_task(
        desktop.collect_notifications(queue, filtered_apps, dnd),
        name="desktop-notification-collector",
    )

    sender_task = asyncio.create_task(
        _send_loop(
            address=address,
            queue=queue,
            min_interval=min_interval,
            message_type=message_type,
            reconnect_delay=reconnect_delay,
            max_reconnect_delay=max_reconnect_delay,
            auto_reconnect=auto_reconnect,
            status_callback=status_callback,
        ),
        name="ble-notification-sender",
    )

    try:
        done, _pending = await asyncio.wait(
            (collector_task, sender_task),
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in done:
            task.result()

    finally:
        for task in (collector_task, sender_task):
            if not task.done():
                task.cancel()

        await asyncio.gather(
            collector_task,
            sender_task,
            return_exceptions=True,
        )
