FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends bird2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY crowdsec-bird-bouncer.py /app/crowdsec-bird-bouncer.py
COPY crowdsec-bird-bouncer.env.example /app/crowdsec-bird-bouncer.env.example

ENTRYPOINT ["python3", "/app/crowdsec-bird-bouncer.py"]
