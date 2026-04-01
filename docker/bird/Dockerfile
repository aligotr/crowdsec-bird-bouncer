FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends bird2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY docker/bird/bird.conf /etc/bird/bird.conf
COPY docker/bird/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
