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
3. Add-on neu starten. Der Beamer übernimmt den Wert beim nächsten Start der
   Wiedergabe.

Ein guter Startwert ist `Target latency` + 200 ms. Erreicht das Bild nie den
Ton, unterstützt der Beamer oder die App kein Delay Reporting. Dann hilft nur
eine kleinere `Target latency` in MA. Im Log steht dann
„Verzögerung konnte nicht gemeldet werden“.

Hinweis: Manche Apps auf Google TV (z. B. mit eigenem Player) ignorieren die
gemeldete Verzögerung. YouTube, Netflix und der Standard-Player beachten sie
in der Regel.

## Ein Dongle oder zwei?

Läuft über denselben Dongle auch die *Sendspin Bluetooth Bridge* zu den Boxen,
teilen sich Empfang (Beamer → HA) und Senden (HA → Boxen) einen Funkkanal. Das
kann funktionieren, führt aber leicht zu Aussetzern. **Empfehlung:** einen
zweiten günstigen USB-Dongle nur für dieses Add-on verwenden (`adapter`).

Beachte: `device_name` setzt den Namen des gewählten Adapters. Teilst du den
Adapter mit der Bridge, heißt er für alle Geräte so.

## Optionen

| Option | Standard | Bedeutung |
|---|---|---|
| `device_name` | Camping Sound | Bluetooth-Name |
| `adapter` | (leer) | `hciX` oder MAC des Dongles |
| `discoverable` | true | Kopplungsmodus an |
| `discoverable_timeout` | 0 | Minuten sichtbar nach Start, 0 = immer |
| `allowed_devices` | [] | nur diese MACs dürfen koppeln/streamen |
| `delay_report_ms` | 1000 | an den Beamer gemeldete Verzögerung |
| `enable_aac` | true | AAC zusätzlich zu SBC anbieten |
| `apply_remote_volume` | true | Lautstärketasten des Geräts übernehmen |
| `silence_timeout` | 20 | Sekunden Stille bis „kein Signal“ |
| `stream_codec` | pcm | Codec zu MA (pcm/flac/opus) |
| `server_url` | (leer) | MA direkt ansprechen, falls mDNS nicht klappt |
| `listen_port` | 8935 | Port für MA |
| `set_device_class` | true | als Lautsprecher ausgeben |

**Camping-Tipp:** Auf dem Campingplatz `discoverable_timeout` auf z. B. 10
setzen oder die MAC des Beamers in `allowed_devices` eintragen. Sonst können
sich Fremde koppeln.

## Fehlersuche

- **Beamer findet das Gerät nicht:** Ist `discoverable` aktiv? Im Log nach
  „Geräteklasse“ schauen (erwartet: „Audio/Video“). Manche TVs zeigen nur Geräte mit Lautsprecher-Klasse
  an.
- **Gekoppelt, aber kein Ton:** Erscheint im Log `Verbunden: … AAC/SBC`? Wenn
  nicht, hat sich vermutlich das HA-Audiosystem den Ton geholt (es bietet
  ebenfalls einen Bluetooth-Empfänger an). Gerät entkoppeln, `enable_aac`
  aktiviert lassen und neu koppeln.
- **Ton stockt:** Zweiten Dongle verwenden, `Target latency` in MA erhöhen.
  Mit `log_level: debug` erscheinen verlorene Pakete im Log.
- **MA findet das Add-on nicht:** `server_url` setzen, z. B.
  `ws://<IP-von-HA>:8927/sendspin`.
