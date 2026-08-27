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

   Das Portal zeigt die fertigen Werte immer erst für den **Vormonat** an,
   daher fragt das Add-on gezielt einmal im Monat ab, statt stur alle X
   Stunden zu pollen:
   - `run_day_of_month` (Default: 1) – an welchem Kalendertag im Monat.
   - `run_hour` (Default: 6) – zu welcher Uhrzeit (0–23).
   - `timezone` (Default: `Europe/Berlin`) – Zeitzone für die obigen Angaben.
   - `run_on_start` (Default: an) – zusätzlich einmal sofort beim
     (Neu-)Start des Add-ons abfragen, praktisch zum Testen der
     Zugangsdaten, ohne bis zum nächsten Monatsersten warten zu müssen.
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

## Ins Energie-Dashboard eintragen

Alle vier Sensoren haben jetzt das passende `device_class`-Attribut und
lassen sich unter Settings → Dashboards → Energie eintragen:

- **Gas hinzufügen** (kWh) → `sensor.uvi_waerme` und `sensor.uvi_warmwasser_energie`
  (beides Wärme aus der Gastherme).
- **Wasser hinzufügen** (m³) → `sensor.uvi_warmwasser_menge` und `sensor.uvi_kaltwasser`.

Ein Punkt zum Wissen: Das Portal zeigt die Werte immer erst für den
**Vormonat**, aktualisiert wird bei uns am 1. des Monats. Wenn du die vier
`sensor.uvi_*`-Entities direkt einträgst, verbucht Home Assistant die
Aktualisierung technisch im Moment des Abrufs – die Monatsbalken im
Energie-Dashboard erscheinen dadurch einen Monat "verspätet" (der im
September ankommende Wert steht dort als September-Balken, obwohl er
eigentlich Augusts Verbrauch ist). Die Jahressumme bleibt davon unberührt,
nur die Monatszuordnung ist verschoben.

### Alternative: rückdatierte externe Statistik (exakte Monatszuordnung)

Seit Version 3.0.0 schreibt das Add-on zusätzlich vier **externe
Statistiken** (`uvi:waerme`, `uvi:warmwasser_menge`, `uvi:warmwasser_energie`,
`uvi:kaltwasser`) über `recorder.import_statistics`, die exakt auf den
echten Verbrauchsmonat datiert sind (Sept.-Abruf → Eintrag im August).
Diese sind komplett getrennt von den normalen `sensor.uvi_*`-Sensoren, es
gibt also keine Doppelzählung. Dafür braucht das Add-on Zugriff auf die
Home-Assistant-API (`homeassistant_api: true` in `config.yaml`, dafür
einmal neu installieren, damit der `SUPERVISOR_TOKEN` gesetzt wird).

Nach dem ersten erfolgreichen Lauf: Settings → Dashboards → Energie →
"Gas hinzufügen" bzw. "Wasser hinzufügen" → in der Auswahlliste nach
"UVI" suchen. Falls dort **nichts** auftaucht: Home Assistant bietet
externe (nicht-Entity-gebundene) Statistiken in eurer Version
möglicherweise nicht im Auswahldialog an – dann einfach stattdessen die
normalen `sensor.uvi_*`-Entities eintragen (siehe oben, mit der
1-Monats-Verschiebung). Schick mir in dem Fall kurz Bescheid, dann schauen
wir nach Alternativen. Im Add-on-Log siehst du bei jedem Lauf, ob der
Schreibvorgang geklappt hat ("Externe Statistik geschrieben: ...").

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
