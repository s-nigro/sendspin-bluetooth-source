# Sendspin Bluetooth Source

Macht Home Assistant zu einem **Bluetooth-Lautsprecher**. Beamer, TV oder Handy
koppeln sich damit, und der Ton landet als **Live Input** in Music Assistant.
Von dort spielst du ihn auf beliebigen Sendspin-Boxen oder Gruppen ab.

```
Beamer / Handy ──Bluetooth (AAC/SBC)──▶ Add-on ──Sendspin source@v1──▶ Music Assistant ──▶ Boxen
```

Voraussetzung: **Music Assistant 2.10 oder neuer** (eingebautes Plugin
„Sendspin Source“).

## So funktioniert es

- Das Add-on meldet sich bei BlueZ von HA OS als A2DP-Empfänger an. Ein eigener
  Audio-Daemon ist nicht nötig. Angeboten werden AAC (bevorzugt von Android/Google
  TV) und SBC (funktioniert mit jedem Gerät).
- Kopplung ohne PIN. Gekoppelte Geräte werden als vertrauenswürdig markiert und
  verbinden sich später automatisch wieder.
- Gegenüber Music Assistant ist das Add-on ein Sendspin-Client mit Quellrolle.
  MA findet es per mDNS. Nach einmaliger Kopplung erscheint es als Live Input.
- **Signalerkennung:** Sobald echter Ton ankommt, meldet das Add-on „Signal da“.
  So kann MA automatisch auf deiner Wunschgruppe starten. Nach der eingestellten
  Stille meldet es „kein Signal“, und MA stoppt nach einer weiteren Minute.
- **Lippensynchronität:** Das Add-on meldet dem Beamer per Bluetooth (AVDTP
  Delay Report), wie viel später der Ton ankommt. Google TV/Android verzögert
  dann das Bild entsprechend.

## Installation

1. **Einstellungen → Add-ons → Add-on-Store → ⋮ → Repositories**.
2. `https://github.com/s-nigro/sendspin-bluetooth-source` hinzufügen.
3. **Sendspin Bluetooth Source** auswählen und installieren. Das Image wird
   dabei auf dem CM5 gebaut, das dauert einige Minuten.

**Updates:** Sobald im Repository die `version` in `config.yaml` erhöht wird,
zeigt Home Assistant ein Update an. Falls es nicht sofort erscheint:
Add-on-Store → ⋮ → **Nach Updates suchen**.

## Einrichtung

1. **Adapter wählen:** Unter `adapter` den Dongle eintragen (`hci1` oder seine
   MAC-Adresse). Welche Adapter es gibt, steht beim Start im Log.
2. Add-on starten. Im Log sollte stehen:
   `A2DP-Empfang (AAC) registriert` und `Warte auf Music Assistant …`.
3. **In Music Assistant koppeln:** Einstellungen → Player. Dort erscheint
   „Camping Sound“ (dein `device_name`). **Start setup** wählen. Der
   Kopplungscode erscheint als **HA-Benachrichtigung** und im Add-on-Log. Code in
   MA eingeben.
4. **Auto-Play (optional):** In den Einstellungen des Geräts in MA
   *Automatically play line-in on* auf deine Camping-Gruppe stellen.
5. **Beamer koppeln:** Auf Google TV unter Einstellungen → Fernbedienungen &
   Zubehör → Zubehör koppeln (je nach Modell auch „Bluetooth“ oder
   „Audioausgang“) „Camping Sound“ wählen.
6. Video starten. Der Ton kommt aus den Boxen.

Handy: ganz normal in den Bluetooth-Einstellungen mit „Camping Sound“ verbinden.

## Lippensynchronität einstellen

Die Gesamtverzögerung ergibt sich aus Bluetooth, dem Add-on und dem Puffer von
Music Assistant/Sendspin. Den Puffer stellst du in MA unter dem Plugin
*Sendspin Source* → erweiterte Einstellungen → **Target latency** ein.

1. Auf dem Beamer ein Lippensynchronitäts-Testvideo abspielen (z. B. „AV sync
   test“ auf YouTube).
2. Hört man den Ton **nach** dem Bild: `delay_report_ms` erhöhen. Kommt er
   **vor** dem Bild: verringern. Schritte von 100 ms, danach feiner.
3. Am einfachsten in der Geräteverwaltung pro Gerät einstellen (ohne
   Neustart). Der Beamer übernimmt den Wert spätestens beim nächsten Start der
   Wiedergabe.

Ein guter Startwert ist `Target latency` + 200 ms. Erreicht das Bild nie den
Ton, unterstützt der Beamer oder die App kein Delay Reporting. Dann hilft nur
eine kleinere `Target latency` in MA. Im Log steht dann
„Verzögerung konnte nicht gemeldet werden“.

Hinweis: Manche Apps auf Google TV (z. B. mit eigenem Player) ignorieren die
gemeldete Verzögerung. YouTube, Netflix und der Standard-Player beachten sie
in der Regel.

## Einmessen mit einem ESP32-Mikrofon

Statt die Verzögerung per Testvideo zu schätzen, kann das Add-on sie **messen**:
Es spielt eine kurze Tonfolge über Music Assistant auf einer Box ab, und ein
ESP32 mit Mikrofon neben der Box hört zu. Gemessen wird die Zeit vom Eintreffen
des Tons im Add-on bis zum Schall aus der Box (etwa ±10–30 ms genau).

### 1. ESP32 vorbereiten (ESPHome)

Getestet ist der Aufbau für ESP32-S3-Boards mit Mikrofon (z. B. Waveshare
ESP32-S3-AUDIO-Board). Das Mikrofon muss in der ESPHome-Konfiguration bereits
eingerichtet sein (`microphone:`). Dann ergänzen:

```yaml
external_components:
  - source: github://s-nigro/sendspin-bluetooth-source@main
    components: [sendspin_latency_mic]

sendspin_latency_mic:
  key: !secret latency_mic_key     # mind. 8 Zeichen, gleicher Wert im Add-on
  # microphone: mein_mikrofon      # nur nötig, wenn es mehrere gibt
  # port: 8939
```

In `secrets.yaml`: `latency_mic_key: "ein-langes-geheimnis"`. Flashen. Das
Mikrofon ist nur während einer Messung aktiv; ein Sprachassistent auf demselben
Gerät funktioniert weiter. Ab ESPHome 2026.11 nutzt die Komponente den genauen
Aufnahme-Zeitstempel des Mikrofons, ältere Versionen schätzen ihn.

### 2. Add-on einstellen

In den Add-on-Optionen `calibration_mic` (IP-Adresse des ESP32, z. B.
`192.168.1.50`) und `calibration_key` (derselbe Schlüssel) eintragen und das
Add-on neu starten. In der Geräteverwaltung unter **Einmessen** mit
„Verbindung testen“ prüfen.

### 3. Boxen einmessen

1. In Music Assistant den Live Input „Camping Sound“ auf **einer** Box
   abspielen (kein Bluetooth-Zuspieler nötig).
2. ESP32 daneben legen, normale Lautstärke.
3. Namen eintragen (z. B. „JBL“) und **Messen** tippen. Es piept 6×.
4. Für jede Box wiederholen.

Die Tabelle zeigt pro Box die Latenz und unter **Ausgleich**, wie viel
Verzögerung die Box in Music Assistant bräuchte, um gleichzeitig mit der
langsamsten zu spielen. Diesen Wert in MA in den Einstellungen des Players
eintragen.

### 4. Zuspieler

Danach die ganze **Gruppe** messen und bei dieser Messung **Als Standard**
wählen. Der Wert gilt dann für alle Zuspieler ohne eigene Einstellung (ersetzt
`delay_report_ms`). Abweichungen einzelner Apps (z. B. Safari) weiter pro Gerät
korrigieren.

## Ein Dongle oder zwei?

Läuft über denselben Dongle auch die *Sendspin Bluetooth Bridge* zu den Boxen,
teilen sich Empfang (Beamer → HA) und Senden (HA → Boxen) einen Funkkanal. Das
kann funktionieren, führt aber leicht zu Aussetzern. **Empfehlung:** einen
zweiten günstigen USB-Dongle nur für dieses Add-on verwenden (`adapter`).

Beachte: `device_name` setzt den Namen des gewählten Adapters. Teilst du den
Adapter mit der Bridge, heißt er für alle Geräte so.

## Geräteverwaltung

In der HA-Seitenleiste erscheint **Bluetooth-Quelle** (falls nicht: beim Add-on
„In Seitenleiste anzeigen“ aktivieren). Dort geht alles ohne Neustart:

- **Status:** Verbindung zu Music Assistant, Signal, gekoppelte Zuspieler mit
  Codec.
- **Kopplungsmodus** für 1, 3 oder 10 Minuten öffnen, z. B. zum Neukoppeln.
  (Bei `discoverable_timeout: 0` ist das Gerät ohnehin immer sichtbar.)
- **Verzögerung pro Gerät:** z. B. Beamer 1200 ms, Handy 800 ms. „Standard“
  setzt auf `delay_report_ms` zurück. Der Wert wird sofort gemeldet; manche
  Geräte übernehmen ihn erst beim nächsten Start der Wiedergabe.
- **Trennen** und **Entfernen** (entkoppeln). Zum Neukoppeln danach den
  Kopplungsmodus öffnen und am Gerät „Camping Sound“ vergessen und neu koppeln.
- Unter *Andere Geräte an diesem Adapter* stehen z. B. Lautsprecher der
  Bluetooth Bridge, wenn sie denselben Dongle nutzt. Die besser nicht entfernen.

Die Seite ist nur über Home Assistant erreichbar, nicht direkt im Netzwerk.

Alternativ ohne Oberfläche: MAC unter `remove_devices` eintragen und das
Add-on neu starten (danach die Liste wieder leeren).

## Optionen

| Option | Standard | Bedeutung |
|---|---|---|
| `device_name` | Camping Sound | Bluetooth-Name |
| `adapter` | (leer) | `hciX` oder MAC des Dongles |
| `discoverable` | true | Kopplungsmodus an |
| `discoverable_timeout` | 0 | Minuten sichtbar nach Start, 0 = immer |
| `allowed_devices` | [] | nur diese MACs dürfen koppeln/streamen |
| `remove_devices` | [] | diese MACs beim Start entkoppeln (danach wieder leeren) |
| `delay_report_ms` | 1000 | Standard-Verzögerung (pro Gerät in der Geräteverwaltung änderbar) |
| `enable_aac` | true | AAC zusätzlich zu SBC anbieten |
| `apply_remote_volume` | true | Lautstärketasten des Geräts übernehmen |
| `silence_timeout` | 20 | Sekunden Stille bis „kein Signal“ |
| `stream_codec` | pcm | Codec zu MA (pcm/flac/opus) |
| `server_url` | (leer) | `auto` = selbst mit MA auf diesem Rechner verbinden; oder eigene URL; leer = per mDNS warten |
| `listen_port` | 8935 | Port für MA |
| `set_device_class` | true | als Lautsprecher ausgeben |
| `restart_audio_on_start` | true | HA-Audiosystem nach dem Start neu starten (Vorrang vor PulseAudio) |
| `calibration_mic` | (leer) | IP/Name des ESP32-Mess-Mikrofons |
| `calibration_key` | (leer) | gemeinsamer Schlüssel mit dem ESP32 |
| `mic_offset_ms` | 0 | bekannte Mikrofon-Verzögerung, wird abgezogen |

**Camping-Tipp:** Auf dem Campingplatz `discoverable_timeout` auf z. B. 10
setzen oder die MAC des Beamers in `allowed_devices` eintragen. Sonst können
sich Fremde koppeln.

## Fehlersuche

- **Beamer findet das Gerät nicht:** Ist `discoverable` aktiv? Im Log nach
  „Geräteklasse“ schauen (erwartet: „Audio/Video“). Manche TVs zeigen nur Geräte mit Lautsprecher-Klasse
  an.
- **Gekoppelt, aber kein Ton:** Erscheint im Log `Verbunden: … AAC/SBC`? Wenn
  nicht, hat sich vermutlich das HA-Audiosystem den Ton geholt (es bietet
  ebenfalls einen Bluetooth-Empfänger an). Das Add-on erkennt das
  („Ton von … geht an einen anderen Bluetooth-Empfänger“) und übernimmt den
  Stream automatisch; in der Geräteverwaltung geht es auch per „Ton
  übernehmen“. Klappt das nicht: Gerät entfernen und neu koppeln.
- **Beamer/TV verbunden, aber Ton geht nicht ans Add-on:** Geräte, die nur SBC
  können, nehmen den ersten passenden Empfänger, und das HA-Audiosystem
  (PulseAudio, von der Bluetooth Bridge genutzt) bietet auf allen Adaptern
  selbst einen an. Mit `restart_audio_on_start` (Standard) startet das Add-on
  das Audiosystem nach sich selbst neu, damit sein Empfänger vorne steht.
  Welche Codecs ein Gerät kann, zeigt die Geräteverwaltung („Kann senden“).
- **Adapter wechselt nach Neustart:** Die Nummern `hci0`/`hci1` können sich
  vertauschen. Unter `adapter` besser die MAC-Adresse des Dongles eintragen.
- **Ton stockt:** Zweiten Dongle verwenden, `Target latency` in MA erhöhen.
  Mit `log_level: debug` erscheinen verlorene Pakete im Log.
- **MA findet das Add-on nicht** (im Log fehlt „Mit Music Assistant verbunden“):
  `server_url` auf `auto` setzen. Das Add-on verbindet sich dann selbst mit MA
  auf diesem Rechner. Läuft MA woanders: `ws://<IP-von-MA>:8927/sendspin`.
