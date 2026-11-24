
from __future__ import annotations

import json
import os
from pathlib import Path


CONFIG_PATH = Path.home() / ".config" / "haylouctl" / "config.json"

DEFAULT_CONFIG = {
    "notifications": {
        "dnd": False,
        "min-interval": 2.0,
        "filter": [],
    },
    "devices": {
        "paired": [],
        "last": None,
        "auto-reconnect": True,
    },
}


def _copy_defaults() -> dict:
    return json.loads(json.dumps(DEFAULT_CONFIG))


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")

    notifications = config.get("notifications")
    devices = config.get("devices")

    if not isinstance(notifications, dict):
        raise ValueError("'notifications' must be a JSON object")

    if not isinstance(devices, dict):
        raise ValueError("'devices' must be a JSON object")

    dnd = notifications.get("dnd", False)
    if not isinstance(dnd, bool):
        raise ValueError("'notifications.dnd' must be a boolean")

    min_interval = notifications.get("min-interval", 2.0)
    if (
        isinstance(min_interval, bool)
        or not isinstance(min_interval, (int, float))
        or min_interval < 0
    ):
        raise ValueError(
            "'notifications.min-interval' must be a non-negative number"
        )

    applications = notifications.get("filter", [])
    if not isinstance(applications, list) or not all(
        isinstance(name, str) and name.strip()
        for name in applications
    ):
        raise ValueError(
            "'notifications.filter' must be a list of application names"
        )

    paired = devices.get("paired", [])
    if not isinstance(paired, list):
        raise ValueError("'devices.paired' must be a list")

    for index, device in enumerate(paired):
        if not isinstance(device, dict):
            raise ValueError(
                f"'devices.paired[{index}]' must be a JSON object"
            )

        for field in ("address", "model", "name"):
            value = device.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"'devices.paired[{index}].{field}' "
                    "must be a non-empty string"
                )

    last_device = devices.get("last")
    if last_device is not None and (
        not isinstance(last_device, str) or not last_device.strip()
    ):
        raise ValueError("'devices.last' must be a MAC address or null")

    auto_reconnect = devices.get("auto-reconnect", True)
    if not isinstance(auto_reconnect, bool):
        raise ValueError("'devices.auto-reconnect' must be a boolean")

    notifications["dnd"] = dnd
    notifications["min-interval"] = float(min_interval)
    notifications["filter"] = [name.strip() for name in applications]

    devices["paired"] = paired
    devices["last"] = last_device
    devices["auto-reconnect"] = auto_reconnect

    return config


def load_config() -> dict:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not CONFIG_PATH.exists():
        config = _copy_defaults()
        save_config(config)
        return config

    try:
        with CONFIG_PATH.open(encoding="utf-8") as file:
            config = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Cannot load configuration {CONFIG_PATH}: {exc}"
        ) from exc

    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")

    defaults = _copy_defaults()

    for section, values in defaults.items():
        if section not in config:
            config[section] = values
        elif isinstance(config[section], dict):
            for key, value in values.items():
                config[section].setdefault(key, value)

    return validate_config(config)


def save_config(config: dict) -> None:
    validate_config(config)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = CONFIG_PATH.with_suffix(".json.tmp")

    try:
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(config, file, indent=4)
            file.write("\n")

        os.replace(temporary_path, CONFIG_PATH)

    except OSError:
        if temporary_path.exists():
            temporary_path.unlink()
        raise


def save_last_device(address: str) -> None:
    config = load_config()
    config["devices"]["last"] = address
    save_config(config)


def get_startup_device() -> str | None:
    devices = load_config()["devices"]

    if devices["last"]:
        return devices["last"]

    if devices["paired"]:
        return devices["paired"][0]["address"]

    return None
