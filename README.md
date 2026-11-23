# Sendspin Bluetooth Source (Home Assistant Add-on)

Bluetooth-Empfänger (A2DP-Sink) für Home Assistant. Den Ton von Beamer, TV oder
Handy stellt das Add-on über die Sendspin-Quellrolle (`source@v1`) als
**Live Input in Music Assistant 2.10+** bereit.

## Installation

Home Assistant → Einstellungen → Add-ons → Add-on-Store → ⋮ → Repositories →
`https://github.com/s-nigro/sendspin-bluetooth-source` hinzufügen, dann
**Sendspin Bluetooth Source** installieren.

[![Repository zu Home Assistant hinzufügen](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fs-nigro%2Fsendspin-bluetooth-source)

## Aufbau

- Add-on: [`sendspin_bt_source/`](sendspin_bt_source/), Anleitung in
  [`sendspin_bt_source/DOCS.md`](sendspin_bt_source/DOCS.md)
- Code: [`sendspin_bt_source/app/btsource/`](sendspin_bt_source/app/btsource/)
  - `bluez.py`: Adapter, Kopplungs-Agent, A2DP-Endpunkte (AAC/SBC), Transport-I/O
  - `a2dp.py`: Codec-Konfiguration, RTP, Dekodierung (PyAV) auf 48 kHz Stereo
  - `sendspin_link.py`: Sendspin-Client (aiosendspin) mit Quellrolle und Kopplung
  - `service.py`: Pipeline, Signalerkennung, Lautstärke
  - `calibration.py`: Latenzmessung mit ESP32-Mikrofon (Messton, Uhrenabgleich, Auswertung)
  - `webui.py`/`webui.html`: Geräteverwaltung und Einmessen (HA-Ingress)
- ESPHome-Komponente fürs Mess-Mikrofon: [`components/sendspin_latency_mic/`](components/sendspin_latency_mic/)

## Tests

```bash
uv venv --python 3.13 .venv
```

```bash
uv pip install --python .venv/bin/python "aiosendspin[source,server]==10.3.0" dbus-fast pytest pytest-asyncio
```

```bash
.venv/bin/python -m pytest -q tests
```

Die Tests prüfen die Codec-Aushandlung, das Dekodieren von SBC und AAC-LATM
(inkl. Resampling) und die Pipeline. Ein End-to-End-Test läuft gegen einen
echten Sendspin-Server: Kopplung, Signal, Stream-Start und Tonübertragung. Die
BlueZ-Seite lässt sich nur auf echter Hardware testen.
