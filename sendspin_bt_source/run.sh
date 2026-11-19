#!/usr/bin/env bash
set -euo pipefail

# The host D-Bus socket is mounted at /run/dbus (host_dbus: true).
if [ ! -S /run/dbus/system_bus_socket ] && [ -S /var/run/dbus/system_bus_socket ]; then
    mkdir -p /run/dbus
    ln -sf /var/run/dbus/system_bus_socket /run/dbus/system_bus_socket
fi
export DBUS_SYSTEM_BUS_ADDRESS="unix:path=/run/dbus/system_bus_socket"

# s6-overlay resets the environment (S6_KEEP_ENV=0), so Docker ENV is lost here.
export PYTHONPATH=/app
export PYTHONUNBUFFERED=1
cd /app

exec /opt/venv/bin/python -m btsource --options /data/options.json --data-dir /data
