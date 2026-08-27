#!/usr/bin/env python3
"""
UVI (Immobilien Hasler Mieterportal) -> MQTT Bruecke fuer Home Assistant.

Laeuft als Home-Assistant-Add-on dauerhaft im Hintergrund und fragt das
Portal gezielt einmal pro Monat ab (Standard: am 1., 06:00 Uhr - konfigurierbar
ueber run_day_of_month/run_hour/timezone), da das Portal die fertigen Werte
immer erst fuer den VORMONAT anzeigt. Liest dazu die Zugangsdaten/Einstellungen
aus /data/options.json (das ist der Standardort, an dem der Supervisor die im
UI eingetragene Add-on-Konfiguration ablegt), loggt sich ins Mieterportal ein,
liest die aktuellen Monatswerte fuer Waerme, Warmwasser und Kaltwasser von der
"Verbrauch"-Uebersichtsseite und veroeffentlicht sie per MQTT Discovery -
genau wie deine bestehenden eedc/AstraMeter/HAME-Sensoren.

Was ich WEISS (aus der gespeicherten /consumptions-Seite):
- Jede der drei Kacheln (Waerme, Warmwasser, Kaltwasser) liegt in einem
  Container mit den Klassen ".bg-white.shadow.rounded-lg".
- Darin: eine <h1> mit dem Namen (z.B. "Warmwasser (Erwärmung)"),
  ein grosser Wert in ".text-4xl.font-bold" (Format "3, 18" -> 3.18,
  wegen <sup> fuer die Nachkommastellen im HTML aufgesplittet),
  und bei Warmwasser zusaetzlich ein Sekundaerwert in kWh.

Was ich NICHT verifizieren konnte (bitte im Add-on-Log pruefen):
- Die genauen Feld-Selektoren auf der Login-Seite selbst
  (tenant_portal_users/sign_in) - mir lag nur die Seite NACH dem Login
  vor. Falls der Login fehlschlaegt, meldet das Skript das im Log; dann
  bitte einmal DEBUG_SCREENSHOT=1 setzen (siehe unten) und den
  Screenshot aus /app pruefen bzw. mir schicken.
"""

import calendar
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
from playwright.sync_api import sync_playwright

OPTIONS_PATH = "/data/options.json"
CUMULATIVE_PATH = "/data/uvi_cumulative.json"
HA_API_BASE = "http://supervisor/core/api"
LOGIN_URL = "https://uvi.immobilien-hasler.de/tenant_portal_users/sign_in"
CONSUMPTIONS_URL = "https://uvi.immobilien-hasler.de/consumptions"

STATE_PREFIX = "uvi/simbach"

DEVICE = {
    "identifiers": ["uvi_immobilien_hasler_simbach"],
    "manufacturer": "Immobilien Hasler",
    "model": "UVI Mieterportal",
    "name": "UVI Verbrauch Simbach",
}

# key -> (object_id, Anzeigename in HA, Icon, Einheit, state_class, device_class)
# device_class "gas"/"water" macht die Sensoren im Energie-Dashboard als
# Gas- bzw. Wasserquelle auswaehlbar (Waerme/Warmwasser-Energie stammen aus
# der Gastherme, daher "gas" in kWh; Warmwasser-Menge/Kaltwasser sind
# "water" in m³).
SENSORS = {
    "waerme": ("uvi_waerme", "Wärme (Monat)", "mdi:fire", "kWh", "total_increasing", "gas"),
    "warmwasser_menge": ("uvi_warmwasser_menge", "Warmwasser Menge (Monat)", "mdi:water-thermometer", "m³", "total_increasing", "water"),
    "warmwasser_energie": ("uvi_warmwasser_energie", "Warmwasser Energie (Monat)", "mdi:water-thermometer-outline", "kWh", "total_increasing", "gas"),
    "kaltwasser": ("uvi_kaltwasser", "Kaltwasser (Monat)", "mdi:water", "m³", "total_increasing", "water"),
}


def load_options() -> dict:
    with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_number(text: str) -> float:
    """Wandelt '3, 18' bzw. '0,40' (durch <sup> im HTML aufgesplittete
    deutsche Dezimalschreibweise) in 3.18 / 0.40 um."""
    cleaned = text.replace("\xa0", " ")
    cleaned = re.sub(r"\s+", "", cleaned)
    cleaned = cleaned.replace(".", "").replace(",", ".")  # falls Tausenderpunkt vorkommt
    return float(cleaned)


def goto_with_retry(page, url: str, retries: int = 5, delay: int = 5) -> None:
    """Navigiert zu url, mit Wiederholungen bei transienten Netzwerkfehlern.

    Direkt nach dem Start des Add-ons (bzw. des Containers) ist das
    interne Docker-Netzwerk manchmal noch nicht ganz bereit, was Chromium
    mit net::ERR_NETWORK_CHANGED quittiert. Ein kurzer Retry behebt das
    zuverlaessig, ohne dass man das Timing von Hand raten muss.
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            page.goto(url, wait_until="load", timeout=30000)
            return
        except Exception as e:
            last_err = e
            print(
                f"goto({url}) fehlgeschlagen (Versuch {attempt}/{retries}): {e}",
                flush=True,
            )
            if attempt < retries:
                time.sleep(delay)
    raise last_err


def login(page, email: str, password: str) -> None:
    goto_with_retry(page, LOGIN_URL)
    page.wait_for_load_state("networkidle")

    email_field = page.locator(
        "input[type=email], input[name*=email i], input[id*=email i]"
    ).first
    password_field = page.locator("input[type=password]").first
    email_field.fill(email)
    password_field.fill(password)

    submit_button = page.locator(
        "input[type=submit], button[type=submit], button:has-text('Anmelden')"
    ).first
    submit_button.click()

    page.wait_for_url("**/consumptions**", timeout=20000)


def scrape(page) -> dict:
    goto_with_retry(page, CONSUMPTIONS_URL)
    # Vue-SPA: der Kartencontainer steht sofort im DOM, die eigentlichen
    # Werte werden aber erst per AJAX nachgeladen. Auf "networkidle"
    # warten, statt nur auf den (leeren) Container zu pruefen.
    page.wait_for_load_state("networkidle")
    page.wait_for_selector(".bg-white.shadow.rounded-lg")

    data: dict = {}
    cards = page.locator(".bg-white.shadow.rounded-lg")
    count = cards.count()
    print(f"scrape(): {count} Karten mit '.bg-white.shadow.rounded-lg' gefunden", flush=True)
    for i in range(count):
        card = cards.nth(i)
        heading_loc = card.locator("h1").first
        if heading_loc.count() == 0:
            print(f"  Karte {i}: keine <h1> gefunden. HTML: {card.inner_html()[:500]}", flush=True)
            continue
        heading = heading_loc.inner_text().strip()

        value_loc = card.locator(".text-4xl.font-bold").first
        if value_loc.count() == 0:
            print(
                f"  Karte {i} ('{heading}'): kein Wert-Element (.text-4xl.font-bold) gefunden. "
                f"HTML: {card.inner_html()[:1000]}",
                flush=True,
            )
            continue
        value = parse_number(value_loc.inner_text())
        print(f"  Karte {i} ('{heading}'): Wert = {value}", flush=True)

        if "Wärme" in heading:
            data["waerme"] = value
        elif "Warmwasser" in heading:
            data["warmwasser_menge"] = value
            secondary_loc = card.locator(".text-gray-600").first
            if secondary_loc.count():
                secondary_text = secondary_loc.inner_text()
                try:
                    data["warmwasser_energie"] = parse_number(secondary_text)
                except ValueError:
                    pass
        elif "Kaltwasser" in heading:
            data["kaltwasser"] = value

    return data


def run_once(opts: dict, retries: int = 3, delay: int = 20) -> dict:
    """Fuehrt Login+Scrape aus, mit kompletten Neuversuchen (frischer Browser)
    bei Fehlern. Manche net::ERR_*-Fehler treten nicht beim ersten goto()
    auf, sondern erst bei der durch den Login-Klick ausgeloesten
    Weiterleitung - dagegen hilft nur ein Neustart des ganzen Ablaufs statt
    eines einzelnen Retries innerhalb von login()/scrape()."""
    last_err = None
    for attempt in range(1, retries + 1):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                login(page, opts["uvi_email"], opts["uvi_password"])
                data = scrape(page)
                return data
            except Exception as e:
                last_err = e
                print(
                    f"run_once Versuch {attempt}/{retries} fehlgeschlagen: {e}",
                    flush=True,
                )
                if os.environ.get("DEBUG_SCREENSHOT"):
                    try:
                        page.screenshot(path="/app/debug_screenshot.png", full_page=True)
                    except Exception:
                        pass
            finally:
                browser.close()
        if attempt < retries:
            time.sleep(delay)
    raise last_err


def publish(opts: dict, data: dict) -> None:
    client = mqtt.Client()
    mqtt_user = opts.get("mqtt_username") or None
    mqtt_pass = opts.get("mqtt_password") or None
    if mqtt_user:
        client.username_pw_set(mqtt_user, mqtt_pass)
    client.connect(opts.get("mqtt_host", "core-mosquitto"), int(opts.get("mqtt_port", 1883)), 60)
    client.loop_start()

    for key, (object_id, name, icon, unit, state_class, device_class) in SENSORS.items():
        config_topic = f"homeassistant/sensor/{object_id}/config"
        state_topic = f"{STATE_PREFIX}/{key}"
        config = {
            "name": name,
            "unique_id": object_id,
            "object_id": object_id,
            "state_topic": state_topic,
            "icon": icon,
            "unit_of_measurement": unit,
            "state_class": state_class,
            "device_class": device_class,
            "device": DEVICE,
        }
        client.publish(config_topic, json.dumps(config), retain=True)
        if key in data:
            client.publish(state_topic, data[key], retain=True)

    last_update_config = {
        "name": "Zuletzt aktualisiert",
        "unique_id": "uvi_last_update",
        "object_id": "uvi_last_update",
        "state_topic": f"{STATE_PREFIX}/last_update",
        "device_class": "timestamp",
        "icon": "mdi:clock-check-outline",
        "device": DEVICE,
    }
    client.publish(
        "homeassistant/sensor/uvi_last_update/config",
        json.dumps(last_update_config),
        retain=True,
    )
    client.publish(
        f"{STATE_PREFIX}/last_update",
        datetime.now(timezone.utc).isoformat(),
        retain=True,
    )
    time.sleep(1)  # kurz warten, damit publish() vor dem Trennen noch rausgeht
    client.loop_stop()
    client.disconnect()


def load_cumulative() -> dict:
    try:
        with open(CUMULATIVE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(
            f"WARNUNG: {CUMULATIVE_PATH} konnte nicht gelesen werden ({e}), starte bei 0.",
            flush=True,
        )
        return {}


def save_cumulative(cumulative: dict) -> None:
    try:
        with open(CUMULATIVE_PATH, "w", encoding="utf-8") as f:
            json.dump(cumulative, f)
    except Exception as e:
        print(f"WARNUNG: {CUMULATIVE_PATH} konnte nicht geschrieben werden: {e}", flush=True)


def previous_month_start(tz: ZoneInfo) -> datetime:
    """Erster Tag des VORMONATS um Mitternacht - das Portal zeigt beim
    Abruf am 1. immer die fertigen Werte fuer den bereits abgelaufenen
    Monat, nicht fuer den gerade begonnenen."""
    now = datetime.now(tz)
    year = now.year - (1 if now.month == 1 else 0)
    month = 12 if now.month == 1 else now.month - 1
    return datetime(year, month, 1, 0, 0, 0, tzinfo=tz)


def call_ha_service(token: str, domain: str, service: str, payload: dict) -> None:
    url = f"{HA_API_BASE}/services/{domain}/{service}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def import_external_statistics(data: dict, tz: ZoneInfo) -> None:
    """Schreibt zusaetzlich zu den normalen MQTT-Sensoren eigene, komplett
    unabhaengige "externe" Statistiken (source="uvi", statistic_id
    "uvi:<key>" statt "sensor.uvi_<key>"), rueckdatiert auf den
    tatsaechlichen Verbrauchsmonat.

    Bewusst getrennt von den sensor.uvi_*-Sensoren: die haben ihre eigene,
    von Home Assistant automatisch am echten Abrufzeitpunkt (1. des
    Monats) erzeugte Statistik. Wuerde man dieselbe Entity zusaetzlich
    rueckwirkend ueberschreiben, gaebe es zwei Zeitreihen fuer denselben
    Sensor und der Verbrauch wuerde doppelt gezaehlt. Die externe
    Statistik hier wird dagegen NIE live aktualisiert - einzige Quelle
    dafuer ist dieser Import, also keine Kollision moeglich.

    Falls das fehlschlaegt (z.B. weil homeassistant_api in config.yaml
    fehlt oder die Home-Assistant-Version keine externen Statistiken im
    Energie-Dashboard anbietet), wird nur eine Warnung geloggt - der
    normale MQTT-Weg (Kacheln, ggf. sensor.uvi_* direkt im
    Energie-Dashboard) laeuft davon komplett unberuehrt weiter.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        print(
            "WARNUNG: SUPERVISOR_TOKEN fehlt - externe Statistik wird nicht "
            "geschrieben (homeassistant_api: true in config.yaml gesetzt und "
            "Add-on danach neu installiert?).",
            flush=True,
        )
        return

    start_iso = previous_month_start(tz).isoformat()
    cumulative = load_cumulative()

    for key, (object_id, name, icon, unit, state_class, device_class) in SENSORS.items():
        if key not in data:
            continue
        prev_sum = cumulative.get(key, 0.0)
        new_sum = prev_sum + data[key]
        statistic_id = f"uvi:{key}"
        payload = {
            "metadata": {
                "has_mean": False,
                "has_sum": True,
                "name": f"{name} (UVI, rückdatiert)",
                "source": "uvi",
                "statistic_id": statistic_id,
                "unit_of_measurement": unit,
            },
            "stats": [
                {"start": start_iso, "sum": round(new_sum, 3), "state": round(data[key], 3)}
            ],
        }
        try:
            call_ha_service(token, "recorder", "import_statistics", payload)
            cumulative[key] = new_sum
            print(
                f"Externe Statistik geschrieben: {statistic_id} @ {start_iso} "
                f"-> sum={new_sum:.2f}",
                flush=True,
            )
        except Exception as e:
            print(f"FEHLER beim Schreiben der externen Statistik {statistic_id}: {e}", flush=True)

    save_cumulative(cumulative)


def do_run(opts: dict) -> None:
    """Fuehrt einen Abfrage+Veroeffentlichungs-Zyklus aus und faengt dabei
    alle Fehler ab, damit ein einzelner fehlgeschlagener Lauf nicht den
    ganzen Scheduler (siehe main()) zum Absturz bringt."""
    try:
        print(f"[{datetime.now().isoformat()}] Starte Abfrage...", flush=True)
        data = run_once(opts)
        print(f"[{datetime.now().isoformat()}] Ausgelesen: {data}", flush=True)
        if not data:
            print("WARNUNG: keine Werte gefunden - Login/Selektoren pruefen.", flush=True)
        else:
            publish(opts, data)
            print(f"[{datetime.now().isoformat()}] An MQTT veroeffentlicht.", flush=True)
            tz_name = opts.get("timezone") or "Europe/Berlin"
            import_external_statistics(data, ZoneInfo(tz_name))
    except Exception:
        print("FEHLER bei Abfrage/Veroeffentlichung:", flush=True)
        traceback.print_exc(file=sys.stdout)


def next_run_time(day_of_month: int, hour: int, tz: ZoneInfo) -> datetime:
    """Naechster Zeitpunkt (>jetzt) am gewuenschten Tag im Monat/Uhrzeit.

    Das Portal zeigt die Werte fuer den VORMONAT - "Tag 1" ist also genau
    richtig, um moeglichst frueh im neuen Monat die fertigen Vormonatswerte
    abzuholen. day_of_month > 28 wird bei kuerzeren Monaten automatisch auf
    den letzten Tag des jeweiligen Monats begrenzt.
    """
    now = datetime.now(tz)
    last_day_this_month = calendar.monthrange(now.year, now.month)[1]
    day = min(day_of_month, last_day_this_month)
    candidate = now.replace(day=day, hour=hour, minute=0, second=0, microsecond=0)

    if candidate <= now:
        year = now.year + (1 if now.month == 12 else 0)
        month = 1 if now.month == 12 else now.month + 1
        last_day_next_month = calendar.monthrange(year, month)[1]
        day = min(day_of_month, last_day_next_month)
        candidate = datetime(year, month, day, hour, 0, 0, tzinfo=tz)

    return candidate


def main() -> None:
    # Beim (Neu-)Start des Add-ons einmal sofort abfragen - praktisch zum
    # Testen der Zugangsdaten, ohne bis zum naechsten Monatsersten warten
    # zu muessen. Ueber die Option "run_on_start" abschaltbar.
    startup_opts = load_options()
    if startup_opts.get("run_on_start", True):
        do_run(startup_opts)

    while True:
        opts = load_options()
        day_of_month = int(opts.get("run_day_of_month", 1) or 1)
        hour = int(opts.get("run_hour", 6) if opts.get("run_hour") is not None else 6)
        tz_name = opts.get("timezone") or "Europe/Berlin"
        tz = ZoneInfo(tz_name)

        target = next_run_time(day_of_month, hour, tz)
        sleep_seconds = max((target - datetime.now(tz)).total_seconds(), 1)
        print(
            f"[{datetime.now().isoformat()}] Naechster Lauf geplant fuer "
            f"{target.isoformat()} ({tz_name}) - warte {sleep_seconds / 3600:.1f}h...",
            flush=True,
        )
        time.sleep(sleep_seconds)

        opts = load_options()  # falls sich die Konfiguration zwischenzeitlich geaendert hat
        do_run(opts)


if __name__ == "__main__":
    main()
