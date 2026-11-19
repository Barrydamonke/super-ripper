FROM python:3.12-slim-trixie

# tzdata: schedule times are Pi local time (set TZ in the template).
# util-linux provides setpriv, used to drop from root to PUID:PGID.
# tini runs as PID 1 so child processes get signals and are reaped.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata util-linux tini cyanrip \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/config \
    PUID=99 \
    PGID=100 \
    UMASK=0002 \
    PORT=8580

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --root-user-action=ignore . && rm -rf /app/src

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

EXPOSE 8580
VOLUME ["/config"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8580'), timeout=3)"

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
