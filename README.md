# Haylou Control

A Linux desktop notification bridge for the **Haylou Solar Plus**. It forwards notifications from your desktop to your watch over Bluetooth Low Energy (BLE), with configurable filters, notification intervals, and automatic reconnection.

The project includes a command-line interface and a GTK4 settings window.

## Features

+ Forward Linux desktop notifications to the watch
+ Block notifications from selected applications
+ Set a minimum interval between notifications
+ Enable Do Not Disturb to suppress all notifications
+ Automatically reconnect when the connection drops

## Requirements

+ Linux with Bluetooth support and BlueZ
+ systemd user services
+ Python 3.10 or later
+ [Bleak](https://bleak.readthedocs.io/) for Bluetooth Low Energy communication
+ `dbus-monitor` for capturing desktop notifications
+ GTK4 and PyGObject, including the GdkX11 bindings, for the graphical interface

## Installation

Install the dependencies using your distribution's package manager. Install Bleak if it is not available as a system package:

```bash
python3 -m pip install bleak
```

Clone the repository and run the installer:

```bash
git clone https://github.com/fernandoromay/haylouctl.git
cd haylouctl
./install.sh
```

The installation also enables and starts the systemd user service. No root privileges are required.

## Getting started

Before the bridge can forward notifications, you must pair a device.

### Graphical Interface

Open **Haylou Control** from your application menu, or run:

```bash
haylouctl gui
```

If no device is configured, click **Pair a device** to scan and pair your watch. You can also change settings, save preferences, and start or stop the bridge.

### Command Line

Alternatively, pair your watch from the terminal:

```bash
haylouctl pair
```

Follow the prompts to select your device and save it.

## Bridge Management

The bridge runs as a background service forwarding desktop notifications to your watch. You can manage it using the graphical interface, systemd, or the CLI.

### Graphical Interface

Open `haylouctl gui` to start or stop the bridge and adjust configuration options.

### systemd Service

The installer configures a user service that starts automatically. You can control it manually:

```bash
systemctl --user start haylouctl.service
systemctl --user stop haylouctl.service
systemctl --user status haylouctl.service
```

Inspect its logs:

```bash
journalctl --user -u haylouctl.service -f
```

### Manual CLI Execution

Run the daemon directly in the foreground:

```bash
haylouctl bridge
```

Run `haylouctl bridge --help` to see additional CLI options for intervals, category types, and reconnection delays.

## Troubleshooting

### The watch is not detected

+ Make sure Bluetooth is enabled on the computer
+ Keep the watch screen on and take it off the charger while scanning
+ Disconnect the watch from your phone before connecting from Linux

### The connection fails

Check whether the watch is already connected to another device. The bridge requires the watch's BLE connection to be available to the computer.

### Diagnostic Commands

If you need to inspect advertising BLE devices or check watch layout details:

```bash
haylouctl scan     # List nearby BLE advertising devices
haylouctl survey   # Connect and inspect GATT table layout
```

## Uninstallation

To completely remove **Haylou Control** from your system:

```bash
haylouctl uninstall
```

Your configuration file remains at `~/.config/haylouctl/` unless removed manually.
