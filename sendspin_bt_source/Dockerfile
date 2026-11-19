ARG BUILD_FROM
FROM ${BUILD_FROM}

# python3 + venv for the service, bluez for btmgmt (device class), no audio daemon:
# the add-on is its own A2DP sink endpoint on the host's BlueZ.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        bluez \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt

COPY app/btsource /app/btsource
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

CMD ["/run.sh"]
