#!/usr/bin/env python3
"""
UVI (Immobilien Hasler Mieterportal) -> MQTT Bruecke fuer Home Assistant.

Laeuft als Home-Assistant-Add-on in einer Dauerschleife: liest einmal pro
Intervall die Zugangsdaten/Einstellungen aus /data/options.json (das ist
der Standardort, an dem der Supervisor die im UI eingetragene
Add-on-Konfiguration ablegt), loggt sich ins Mieterportal ein, liest die
aktuellen Monatswerte fuer Waerme, Warmwasser und Kaltwasser von der
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

import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from playwright.sync_api import sync_playwright

OPTIONS_PATH = "/data/options.json"
LOGIN_URL = "https://uvi.immobilien-hasler.de/tenant_portal_users/sign_in"
CONSUMPTIONS_URL = "https://uvi.immobilien-hasler.de/consumptions"

STATE_PREFIX = "uvi/simbach"

DEVICE = {
    "identifiers": ["uvi_immobilien_hasler_simbach"],
    "manufacturer": "Immobilien Hasler",
    "model": "UVI Mieterportal",
    "name": "UVI Verbrauch Simbach",
}

# key -> (object_id, Anzeigename in HA, Icon, Einheit, state_class)
SENSORS = {
    "waerme": ("uvi_waerme", "Wärme (Monat)", "mdi:fire", "Wh", "total_increasing"),
    "warmwasser_menge": ("uvi_warmwasser_menge", "Warmwasser Menge (Monat)", "mdi:water-thermometer", "m³", "total_increasing"),
    "warmwasser_energie": ("uvi_warmwasser_energie", "Warmwasser Energie (Monat)", "mdi:water-thermometer-outline", "kWh", "total_increasing"),
    "kaltwasser": ("uvi_kaltwasser", "Kaltwasser (Monat)", "mdi:water", "m³", "total_increasing"),
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
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            page.goto(url, wait_until="load", timeout=30000)
            return
        except Exception as e:
            last_err = e
            print(f"goto({url}) fehlgeschlagen (Versuch {attempt}/{retries}): {e}", flush=True)
            if attempt < retries:
                time.sleep(delay)
    raise last_err


def login(page, email: str, password: str) -> None:
    page.goto(LOGIN_URL)
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
    page.goto(CONSUMPTIONS_URL)
    page.wait_for_selector(".bg-white.shadow.rounded-lg")

    data: dict = {}
    cards = page.locator(".bg-white.shadow.rounded-lg")
    for i in range(cards.count()):
        card = cards.nth(i)
        heading_loc = card.locator("h1").first
        if heading_loc.count() == 0:
            continue
        heading = heading_loc.inner_text().strip()

        value_loc = card.locator(".text-4xl.font-bold").first
        if value_loc.count() == 0:
            continue
        value = parse_number(value_loc.inner_text())

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


def run_once(opts: dict) -> dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            login(page, opts["uvi_email"], opts["uvi_password"])
            data = scrape(page)
        except Exception:
            if os.environ.get("DEBUG_SCREENSHOT"):
                page.screenshot(path="/app/debug_screenshot.png", full_page=True)
            raise
        finally:
            browser.close()
    return data


def publish(opts: dict, data: dict) -> None:
    client = mqtt.Client()
    mqtt_user = opts.get("mqtt_username") or None
    mqtt_pass = opts.get("mqtt_password") or None
    if mqtt_user:
        client.username_pw_set(mqtt_user, mqtt_pass)
    client.connect(opts.get("mqtt_host", "core-mosquitto"), int(opts.get("mqtt_port", 1883)), 60)
    client.loop_start()

    for key, (object_id, name, icon, unit, state_class) in SENSORS.items():
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
            "device": DEVICE,
        }
        client.publish(config_topic, json.dumps(config), retain=True)
        if key in data:
            client.publish(state_topic, data[key], retain=True)

    client.publish(
        f"{STATE_PREFIX}/last_update",
        datetime.now(timezone.utc).isoformat(),
        retain=True,
    )
    time.sleep(1)  # kurz warten, damit publish() vor dem Trennen noch rausgeht
    client.loop_stop()
    client.disconnect()


def main() -> None:
    while True:
        opts = load_options()
        interval_hours = int(opts.get("run_interval_hours", 24) or 24)
        try:
            print(f"[{datetime.now().isoformat()}] Starte Abfrage...", flush=True)
            data = run_once(opts)
            print(f"[{datetime.now().isoformat()}] Ausgelesen: {data}", flush=True)
            if not data:
                print("WARNUNG: keine Werte gefunden - Login/Selektoren pruefen.", flush=True)
            else:
                publish(opts, data)
                print(f"[{datetime.now().isoformat()}] An MQTT veroeffentlicht.", flush=True)
        except Exception:
            print("FEHLER bei Abfrage/Veroeffentlichung:", flush=True)
            traceback.print_exc(file=sys.stdout)

        print(f"Warte {interval_hours}h bis zum naechsten Lauf...", flush=True)
        time.sleep(interval_hours * 3600)


if __name__ == "__main__":
    main()
