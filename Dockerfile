# MakeMKV is not in Debian: build it from makemkv.com source at a pinned version.
# To bump: change the three values below (see README "Updating MakeMKV").
FROM python:3.12-slim-trixie AS makemkv-build
ARG MAKEMKV_VERSION=2.0.0
ARG MAKEMKV_OSS_SHA256=435316b2d219eb48c880526557addd076b5f5e6de5171424c7651f9cac95b161
ARG MAKEMKV_BIN_SHA256=f1265e74875a186efdfbbbec7459a64e969033515e53cbc4d805f0a374f0a124
RUN apt-get update     && apt-get install -y --no-install-recommends build-essential pkg-config libc6-dev         libssl-dev libexpat1-dev zlib1g-dev libavcodec-dev curl ca-certificates     && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN for part in oss bin; do         curl -fsSL -O "https://www.makemkv.com/download/makemkv-${part}-${MAKEMKV_VERSION}.tar.gz";     done     && echo "${MAKEMKV_OSS_SHA256}  makemkv-oss-${MAKEMKV_VERSION}.tar.gz" | sha256sum -c -     && echo "${MAKEMKV_BIN_SHA256}  makemkv-bin-${MAKEMKV_VERSION}.tar.gz" | sha256sum -c -     && tar xzf makemkv-oss-${MAKEMKV_VERSION}.tar.gz     && tar xzf makemkv-bin-${MAKEMKV_VERSION}.tar.gz
RUN cd makemkv-oss-${MAKEMKV_VERSION}     && ./configure --prefix=/usr --disable-gui     && make -j"$(nproc)"     && make install DESTDIR=/staging
RUN cd makemkv-bin-${MAKEMKV_VERSION}     && mkdir -p tmp && echo accepted > tmp/eula_accepted     && make     && make install DESTDIR=/staging PREFIX=/usr

FROM python:3.12-slim-trixie

# tzdata: schedule times are Pi local time (set TZ in the template).
# util-linux provides setpriv, used to drop from root to PUID:PGID.
# tini runs as PID 1 so child processes get signals and are reaped.
# libssl3t64..libavcodec61: MakeMKV runtime libs; ffmpeg provides ffprobe.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata util-linux tini cyanrip \
        libssl3t64 libexpat1 zlib1g libavcodec61 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=makemkv-build /staging/usr/ /usr/

ENV MAKEMKV_HOME=/config/makemkv \
    PYTHONUNBUFFERED=1 \
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
