
"""GTK4 settings window that controls haylouctl.service.

Two views swapped on a Gtk.Stack:
  empty   no devices.paired — centered "No device yet" + Pair button
  form    device dropdown (+ pair button), notifications (DND, minimum
          interval, blocked apps), connection (auto-reconnect), Save +
          Start/Stop buttons, and a live status label.

PairDialog scans for Haylou devices on a background thread (results
delivered via GLib.idle_add), lets the user pick one and set a name,
then saves via config.save_paired_device. The caller reloads settings
so the new device is auto-selected.

Service state is polled from `systemctl --user show
--property=ActiveState` every 1.5 s; the start button toggles the unit
with --no-block after saving settings first.

Window geometry persistence (GTK4 has no move()/get_position(); this is
X11-only via python-xlib, best-effort):
  restore  on map: set_default_size() for the size, then an idle callback
           moves the frame with an X configure request — under muffin a
           configure request on the client window lands the frame at the
           requested coordinates (configuring the frame parent is ignored).
  save     on close-request and unmap: absolute frame position from
           translate_coords minus _NET_FRAME_EXTENTS (left/top), plus the
           allocated size; written to the config "window" section.
  fallback every xlib call swallows exceptions: if python-xlib or X11 is
           unavailable (e.g. Wayland) only the size is restored.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GdkX11", "4.0")
from gi.repository import GdkX11, GLib, Gtk

from . import config, transport


SERVICE_NAME = "haylouctl.service"


def _query_frame_position(surface) -> tuple[int, int] | None:
    """Absolute root position of the window frame, or None off X11."""
    try:
        import Xlib.display

        xid = GdkX11.X11Surface.get_xid(surface)
        display = Xlib.display.Display()
        root = display.screen().root
        window = display.create_resource_object("window", xid)
        reply = root.translate_coords(window, 0, 0)
        client_x, client_y = reply.x, reply.y
        extents = window.get_full_property(
            display.intern_atom("_NET_FRAME_EXTENTS"),
            0,
        )
        left = top = 0
        if extents is not None and len(extents.value) >= 3:
            left, top = int(extents.value[0]), int(extents.value[2])
        display.close()
        return client_x - left, client_y - top
    except Exception:
        return None


def _move_frame_to(surface, x: int, y: int) -> bool:
    try:
        import Xlib.display

        xid = GdkX11.X11Surface.get_xid(surface)
        display = Xlib.display.Display()
        window = display.create_resource_object("window", xid)
        window.configure(x=x, y=y)
        display.sync()
        display.close()
        return True
    except Exception:
        return False


class HaylouWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application):
        super().__init__(application=application, title="Haylou Control")
        self.settings = config.load_config()
        self.last_service_state: str | None = None

        window_geometry = self.settings.get("window") or {}
        width = window_geometry.get("width")
        height = window_geometry.get("height")
        if width and height:
            self.set_default_size(width, height)
        else:
            self.set_default_size(520, 520)

        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)
        self.connect("close-request", self._on_close_request)

        self._build_interface()
        self._load_settings()

        GLib.timeout_add(1500, self._poll_service_status)
        self._poll_service_status()

    def _build_interface(self) -> None:
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.set_child(self.stack)

        self.stack.add_named(self._build_empty_state(), "empty")
        self.stack.add_named(self._build_form(), "form")

    def _build_empty_state(self) -> Gtk.Widget:
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )

        label = Gtk.Label(label="No device yet")
        label.add_css_class("title-2")
        box.append(label)

        self.pair_button = Gtk.Button(label="Pair a device")
        self.pair_button.add_css_class("suggested-action")
        self.pair_button.connect("clicked", self._open_pair_dialog)
        box.append(self.pair_button)

        return box

    def _build_form(self) -> Gtk.Widget:
        root = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
        )
        root.set_margin_top(20)
        root.set_margin_bottom(20)
        root.set_margin_start(20)
        root.set_margin_end(20)

        device_heading = Gtk.Label(label="Device")
        device_heading.set_xalign(0)
        device_heading.add_css_class("heading")
        root.append(device_heading)

        device_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )

        self.device_model = Gtk.StringList.new([])
        self.device_dropdown = Gtk.DropDown.new(self.device_model, None)
        self.device_dropdown.set_hexpand(True)
        device_row.append(self.device_dropdown)

        self.add_device_button = Gtk.Button(label="+")
        self.add_device_button.set_tooltip_text("Pair another device")
        self.add_device_button.connect("clicked", self._open_pair_dialog)
        device_row.append(self.add_device_button)

        root.append(device_row)

        notification_heading = Gtk.Label(label="Notifications")
        notification_heading.set_xalign(0)
        notification_heading.add_css_class("heading")
        root.append(notification_heading)

        self.dnd_check = Gtk.CheckButton(label="Do not disturb")
        root.append(self.dnd_check)

        interval_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )

        interval_label = Gtk.Label(label="Minimum interval (seconds)")
        interval_label.set_xalign(0)
        interval_label.set_hexpand(True)
        interval_row.append(interval_label)

        self.interval_spin = Gtk.SpinButton.new(
            Gtk.Adjustment(
                value=2.0,
                lower=0.0,
                upper=3600.0,
                step_increment=0.5,
                page_increment=5.0,
                page_size=0.0,
            ),
            0.5,
            1,
        )
        interval_row.append(self.interval_spin)
        root.append(interval_row)

        filter_label = Gtk.Label(label="Blocked applications")
        filter_label.set_xalign(0)
        root.append(filter_label)

        self.filter_entry = Gtk.Entry()
        self.filter_entry.set_placeholder_text(
            "Application names, separated by commas"
        )
        root.append(self.filter_entry)

        connection_heading = Gtk.Label(label="Connection")
        connection_heading.set_xalign(0)
        connection_heading.add_css_class("heading")
        root.append(connection_heading)

        self.reconnect_check = Gtk.CheckButton(
            label="Automatically reconnect"
        )
        root.append(self.reconnect_check)

        button_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )

        self.save_button = Gtk.Button(label="Save settings")
        self.save_button.connect("clicked", self._save_settings)
        button_row.append(self.save_button)

        self.start_button = Gtk.Button(label="Start bridge")
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", self._toggle_bridge)
        button_row.append(self.start_button)

        root.append(button_row)

        status_heading = Gtk.Label(label="Status")
        status_heading.set_xalign(0)
        status_heading.add_css_class("heading")
        root.append(status_heading)

        self.status_label = Gtk.Label(label="Checking service...")
        self.status_label.set_xalign(0)
        self.status_label.set_wrap(True)
        self.status_label.set_selectable(True)
        root.append(self.status_label)

        return root

    def _load_settings(self) -> None:
        notifications = self.settings["notifications"]
        devices = self.settings["devices"]

        self.dnd_check.set_active(notifications["dnd"])
        self.interval_spin.set_value(notifications["min-interval"])
        self.filter_entry.set_text(", ".join(notifications["filter"]))
        self.reconnect_check.set_active(devices["auto-reconnect"])

        paired = list(devices["paired"])
        last = devices["last"]

        if not paired and last:
            paired.append({
                "address": last,
                "model": "Unknown",
                "name": "Configured device",
            })

        self.devices = paired

        if not paired:
            self.stack.set_visible_child_name("empty")
            return

        self.stack.set_visible_child_name("form")

        self.device_model.splice(
            0,
            self.device_model.get_n_items(),
            [
                f"{device['name']} ({device['address']})"
                for device in paired
            ],
        )

        selected = 0

        if last:
            for index, device in enumerate(paired):
                if device["address"] == last:
                    selected = index
                    break

        self.device_dropdown.set_sensitive(True)
        self.device_dropdown.set_selected(selected)

    def _save_settings(self, _button: Gtk.Button | None = None) -> bool:
        try:
            settings = config.load_config()

            settings["notifications"]["dnd"] = (
                self.dnd_check.get_active()
            )
            settings["notifications"]["min-interval"] = (
                self.interval_spin.get_value()
            )
            settings["notifications"]["filter"] = [
                name.strip()
                for name in self.filter_entry.get_text().split(",")
                if name.strip()
            ]

            settings["devices"]["auto-reconnect"] = (
                self.reconnect_check.get_active()
            )

            selected = self.device_dropdown.get_selected()

            if selected < len(self.devices):
                settings["devices"]["last"] = (
                    self.devices[selected]["address"]
                )

            config.save_config(settings)
            self.settings = settings

            self.status_label.set_text("Settings saved.")
            return True

        except Exception as error:
            self.status_label.set_text(
                f"Could not save settings: {error}"
            )
            return False

    def _on_map(self, *_args) -> None:
        GLib.idle_add(self._restore_position)

    def _on_unmap(self, *_args) -> None:
        self._save_geometry()

    def _on_close_request(self, *_args) -> bool:
        self._save_geometry()
        return False

    def _restore_position(self) -> bool:
        geometry = self.settings.get("window") or {}
        x, y = geometry.get("x"), geometry.get("y")

        if x is None or y is None:
            return False

        surface = self.get_surface()
        if surface is not None:
            _move_frame_to(surface, x, y)

        return False

    def _save_geometry(self) -> None:
        surface = self.get_surface()
        if surface is None or surface.is_destroyed():
            return

        position = _query_frame_position(surface)
        width, height = self.get_width(), self.get_height()

        if position is None or width <= 0 or height <= 0:
            return

        x, y = position
        self.settings["window"] = {"x": x, "y": y, "width": width, "height": height}

        try:
            config.save_window_geometry(x, y, width, height)
        except OSError:
            pass

    def _systemctl(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["systemctl", "--user", *arguments],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    def _get_service_state(self) -> str:
        try:
            result = self._systemctl(
                "show",
                "--property=ActiveState",
                "--value",
                SERVICE_NAME,
            )

            if result.returncode != 0:
                return "unavailable"

            return result.stdout.strip() or "unknown"

        except (OSError, subprocess.TimeoutExpired):
            return "unavailable"

    def _poll_service_status(self) -> bool:
        state = self._get_service_state()

        if state == self.last_service_state:
            return True

        self.last_service_state = state

        if state == "active":
            self.start_button.set_label("Stop bridge")
            self.start_button.remove_css_class("suggested-action")
            self.start_button.add_css_class("destructive-action")
            self.start_button.set_sensitive(True)
            self.status_label.set_text("Bridge running.")

        elif state == "activating":
            self.start_button.set_label("Starting...")
            self.start_button.set_sensitive(False)
            self.status_label.set_text("Starting bridge...")

        elif state == "deactivating":
            self.start_button.set_label("Stopping...")
            self.start_button.set_sensitive(False)
            self.status_label.set_text("Stopping bridge...")

        elif state == "failed":
            self.start_button.set_label("Start bridge")
            self.start_button.remove_css_class("destructive-action")
            self.start_button.add_css_class("suggested-action")
            self.start_button.set_sensitive(True)
            self.status_label.set_text(
                "Bridge failed. Check its systemd journal."
            )

        elif state == "inactive":
            self.start_button.set_label("Start bridge")
            self.start_button.remove_css_class("destructive-action")
            self.start_button.add_css_class("suggested-action")
            self.start_button.set_sensitive(bool(self.devices))
            self.status_label.set_text("Bridge stopped.")

        else:
            self.start_button.set_label("Start bridge")
            self.start_button.remove_css_class("destructive-action")
            self.start_button.add_css_class("suggested-action")
            self.start_button.set_sensitive(bool(self.devices))
            self.status_label.set_text(
                "Service unavailable. Check systemd user service installation."
            )

        return True

    def _toggle_bridge(self, _button: Gtk.Button) -> None:
        if not self._save_settings():
            return

        state = self._get_service_state()

        if state in ("active", "activating"):
            action = "stop"
            message = "Stopping bridge..."
        else:
            action = "start"
            message = "Starting bridge..."

        try:
            result = self._systemctl(
                "--no-block",
                action,
                SERVICE_NAME,
            )

            if result.returncode != 0:
                error = result.stderr.strip() or result.stdout.strip()
                self.status_label.set_text(
                    f"Could not {action} bridge: {error}"
                )
                return

            self.status_label.set_text(message)
            self.last_service_state = None
            self._poll_service_status()

        except (OSError, subprocess.TimeoutExpired) as error:
            self.status_label.set_text(
                f"Systemd command failed: {error}"
            )

    def _open_pair_dialog(self, _button: Gtk.Button) -> None:
        dialog = PairDialog(self)
        dialog.connect("close-request", self._on_pair_dialog_close_request)
        dialog.present()

    def _on_pair_dialog_close_request(self, dialog: Gtk.Window) -> bool:
        if getattr(dialog, "paired", False):
            self.settings = config.load_config()
            self._load_settings()
        return False


class PairDialog(Gtk.Window):
    """Scan for Haylou devices, pick one, name it, save to config."""

    def __init__(self, parent: Gtk.Window):
        super().__init__(title="Pair a device", transient_for=parent)
        self.set_modal(True)
        self.set_default_size(400, 480)
        self.paired = False
        self._devices: list[transport.Advertisement] = []
        self._scanning = False
        self._auto_name = ""

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
        )
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        self.set_child(box)

        self.status_label = Gtk.Label(label="Scanning for Haylou devices...")
        self.status_label.set_xalign(0)
        box.append(self.status_label)

        self.spinner = Gtk.Spinner()
        self.spinner.set_halign(Gtk.Align.CENTER)
        box.append(self.spinner)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("boxed-list")
        scrolled.set_child(self.listbox)
        box.append(scrolled)

        name_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        name_label = Gtk.Label(label="Name")
        name_label.set_xalign(0)
        name_row.append(name_label)

        self.name_entry = Gtk.Entry()
        self.name_entry.set_hexpand(True)
        self.name_entry.set_placeholder_text("Device name")
        name_row.append(self.name_entry)
        box.append(name_row)

        self.error_label = Gtk.Label(label="")
        self.error_label.set_xalign(0)
        self.error_label.add_css_class("error")
        self.error_label.set_visible(False)
        box.append(self.error_label)

        button_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            halign=Gtk.Align.END,
        )

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        button_row.append(cancel)

        self.pair_button = Gtk.Button(label="Pair")
        self.pair_button.add_css_class("suggested-action")
        self.pair_button.set_sensitive(False)
        self.pair_button.connect("clicked", self._on_pair_clicked)
        button_row.append(self.pair_button)

        box.append(button_row)

        self.listbox.connect("row-selected", self._on_row_selected)
        self._start_scan()

    def _start_scan(self) -> None:
        if self._scanning:
            return

        self._scanning = True
        self._devices = []
        self._auto_name = ""
        self._clear_list()
        self.pair_button.set_sensitive(False)
        self.error_label.set_visible(False)
        self.name_entry.set_text("")
        self.status_label.set_text("Scanning for Haylou devices...")
        self.spinner.start()
        self.spinner.set_visible(True)

        thread = threading.Thread(target=self._scan_worker, daemon=True)
        thread.start()

    def _scan_worker(self) -> None:
        try:
            rows = asyncio.run(transport.scan(10.0))
        except Exception as error:
            GLib.idle_add(self._on_scan_done, None, str(error))
            return

        devices = [
            row for row in rows if transport.looks_like_haylou(row.name)
        ]
        GLib.idle_add(self._on_scan_done, devices, None)

    def _on_scan_done(
        self,
        devices: list[transport.Advertisement] | None,
        error: str | None,
    ) -> bool:
        self._scanning = False
        self.spinner.stop()
        self.spinner.set_visible(False)

        if error is not None:
            self.status_label.set_text("Scan failed.")
            self.error_label.set_text(error)
            self.error_label.set_visible(True)
            self.pair_button.set_label("Retry")
            self.pair_button.set_sensitive(True)
            return False

        self._devices = devices or []

        if not self._devices:
            self.status_label.set_text("No Haylou devices found.")
            self.error_label.set_text(
                "Is the watch advertising? Screen on, off the charger, "
                "phone Bluetooth off."
            )
            self.error_label.set_visible(True)
            self.pair_button.set_label("Retry")
            self.pair_button.set_sensitive(True)
            return False

        self.status_label.set_text("Select your device:")
        self.pair_button.set_label("Pair")

        for device in self._devices:
            row = Gtk.ListBoxRow()
            label = Gtk.Label(
                label=f"{device.name}  ·  {device.address}  ·  "
                f"{device.rssi} dBm",
                xalign=0,
            )
            label.set_margin_top(8)
            label.set_margin_bottom(8)
            label.set_margin_start(8)
            label.set_margin_end(8)
            row.set_child(label)
            row.device = device
            self.listbox.append(row)

        return False

    def _clear_list(self) -> None:
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)

    def _on_row_selected(
        self,
        _listbox: Gtk.ListBox,
        row: Gtk.ListBoxRow | None,
    ) -> None:
        if row is None:
            self.pair_button.set_sensitive(False)
            return

        device = row.device
        self.pair_button.set_sensitive(True)

        # Prefill unless the user edited the previous auto-filled name.
        current = self.name_entry.get_text()
        if not current or current == self._auto_name:
            self._auto_name = transport.strip_brand(device.name)
            self.name_entry.set_text(self._auto_name)

    def _on_pair_clicked(self, _button: Gtk.Button) -> None:
        if self.pair_button.get_label() == "Retry":
            self.pair_button.set_label("Pair")
            self._start_scan()
            return

        row = self.listbox.get_selected_row()
        if row is None:
            return

        device = row.device
        model = transport.strip_brand(device.name)
        name = self.name_entry.get_text().strip() or model

        try:
            config.save_paired_device(device.address, model, name)
        except Exception as error:
            self.error_label.set_text(f"Could not save device: {error}")
            self.error_label.set_visible(True)
            return

        self.paired = True
        self.close()


class HaylouApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.haylou.Control")

    def do_activate(self) -> None:
        window = self.props.active_window

        if window is None:
            window = HaylouWindow(self)

        window.present()


def main() -> int:
    application = HaylouApplication()
    return application.run(None)
