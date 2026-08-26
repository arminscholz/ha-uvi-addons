# Basis-Image bringt Chromium + alle System-Abhängigkeiten für Playwright
# schon mit - daher kein separates "playwright install-deps" nötig.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

RUN pip install --no-cache-dir paho-mqtt==1.6.1 playwright==1.47.0

WORKDIR /app
COPY uvi_to_mqtt.py /app/uvi_to_mqtt.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
