# Changelog

## 0.6.0

- Erkennt, wenn ein Zuspieler (z. B. Google TV) seinen Ton an einen anderen
  Bluetooth-Empfänger auf dem HA-Rechner schickt (meist das HA-Audiosystem),
  und übernimmt den Stream automatisch (Option `claim_streams`).
- Geräteverwaltung zeigt das an und hat die Schaltfläche „Ton übernehmen“.

## 0.5.0

- Neue Geräteverwaltung in der HA-Seitenleiste („Bluetooth-Quelle“):
  Status, Kopplungsmodus auf Zeit, Trennen, Entfernen, Verzögerung pro Gerät –
  alles ohne Neustart.
- Verzögerung pro Gerät wird in `/data/devices.json` gespeichert und sofort an
  ein verbundenes Gerät gemeldet.

## 0.4.0

- `server_url: auto`: Das Add-on verbindet sich selbst mit Music Assistant auf diesem Rechner (127.0.0.1, dann LAN-IP), statt auf mDNS zu warten.
- Mit gesetzter `server_url` wird nicht mehr per mDNS angekündigt (Sendspin-Spezifikation).
- Fix: falsche Meldung „nicht (mehr) gekoppelt“ direkt nach dem Entfernen.

## 0.3.0

- Neue Option `remove_devices`: gekoppelte Bluetooth-Geräte beim Start entkoppeln.
- Gekoppelte Geräte werden beim Start im Log aufgelistet.

## 0.2.3

- Log zeigt Verbinden/Trennen von Music Assistant.

## 0.2.2

- Kein Traceback mehr beim Stoppen/Neustarten während der Wiedergabe (Bluetooth-Verbindung wird vor dem Trennen von D-Bus freigegeben).

## 0.2.1

- Geräteklasse direkt über die Bluetooth-Management-Schnittstelle setzen statt über btmgmt (das im Container hing). Die tatsächliche Klasse steht danach im Log.

## 0.2.0

- Fix Ruckeln: Zeitstempel werden jetzt aus der Sampleanzahl gebildet statt aus der schwankenden Bluetooth-Ankunftszeit. Music Assistant hat bei jedem verspäteten Paket (> 15 ms) Stille eingefügt.

## 0.1.2

- btmgmt ohne Terminal-Eingabe starten (Geräteklasse wurde wegen Timeout nicht gesetzt).

## 0.1.1

- Fix: Start schlug mit „No module named btsource“ fehl (s6-overlay setzt die Umgebung zurück).

## 0.1.0

- Erste Version: A2DP-Empfang (SBC, AAC) direkt über BlueZ, Sendspin-Quellrolle
  (`source@v1`) für Music Assistant 2.10+, Signalerkennung für Auto-Play,
  Delay Reporting für Lippensynchronität, Kopplungscode als HA-Benachrichtigung.
