# Changelog

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
