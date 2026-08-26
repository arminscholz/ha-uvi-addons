# UVI Verbrauch -> MQTT (Home Assistant Add-on)

Liest Heizkosten/Warmwasser/Kaltwasser aus dem Immobilien-Hasler-Mieterportal
(`uvi.immobilien-hasler.de`) und veröffentlicht sie per MQTT Discovery, damit
sie automatisch als Sensoren in Home Assistant erscheinen.

## Installation als lokales Add-on

1. Diesen ganzen Ordner (`uvi_to_mqtt/` mit `config.yaml`, `Dockerfile`,
   `run.sh`, `uvi_to_mqtt.py`) so auf deinen Home-Assistant-Host kopieren,
   dass er unter `/addons/local/uvi_to_mqtt/` liegt. Am einfachsten geht das
   mit dem offiziellen **Samba share**-Add-on (Settings → Add-ons →
   Add-on Store → offizielle Add-ons) – danach ist `\\<HA-IP>\addons` als
   Netzlaufwerk erreichbar, dort den Ordner reinkopieren. Alternativ über
   das **Terminal & SSH**-Add-on mit `scp`/`cp`.
2. In Home Assistant: Settings → Add-ons → Add-on Store → oben rechts ⋮ →
   "Check for updates" (oder Seite neu laden). Unter "Local add-ons"
   taucht jetzt "UVI Verbrauch -> MQTT" auf.
3. Add-on öffnen → Install (der erste Build dauert ein paar Minuten, weil
   das Playwright/Chromium-Basisimage geladen wird).
4. Tab "Configuration": `uvi_email`, `uvi_password` eintragen. Bei
   `mqtt_host` reicht meist der Standardwert `core-mosquitto` (Name des
   offiziellen Mosquitto-Broker-Add-ons im internen Netz), dazu
   `mqtt_username`/`mqtt_password` von deinem MQTT-Broker eintragen.
   `run_interval_hours` steuert, wie oft neu abgefragt wird (Default: 24 –
   das Portal aktualisiert offenbar nur monatlich, häufiger bringt nichts).
5. Speichern, dann Tab "Info" → Start. Im Tab "Log" siehst du, ob Login und
   MQTT-Veröffentlichung geklappt haben.

## Danach in Home Assistant

Nach dem ersten erfolgreichen Lauf tauchen vier neue Sensoren auf:

- `sensor.uvi_waerme` – Wärme (Monat)
- `sensor.uvi_warmwasser_menge` – Warmwasser Menge (Monat, m³)
- `sensor.uvi_warmwasser_energie` – Warmwasser Energie (Monat, kWh)
- `sensor.uvi_kaltwasser` – Kaltwasser (Monat, m³)

Zu finden auch unter Settings → Devices & Services → MQTT → Gerät
"UVI Verbrauch Simbach".

## Falls der Login fehlschlägt

Mir lag nur die Seite NACH dem Login vor, nicht die Login-Seite selbst –
die Feld-Selektoren in `login()` sind daher plausible Annahmen
(E-Mail-Feld, Passwort-Feld, Submit-Button), aber ungetestet. Falls es
nicht auf Anhieb klappt: im Add-on eine Umgebungsvariable
`DEBUG_SCREENSHOT=1` setzen (Configuration-Tab → "Show unused optional
configuration options" bzw. notfalls direkt im Dockerfile/run.sh
ergänzen) – dann legt das Skript bei einem Fehler einen Screenshot der
Login-Seite ab, den du mir schicken kannst, damit ich die Selektoren
korrigiere.
