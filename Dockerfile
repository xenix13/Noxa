# Noxa Docker image: docker compose up -d (see docker-compose.yml and the README).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 \
    NOXA_HOST=0.0.0.0 NOXA_PORT=8420 NOXA_DATA=/data NOXA_STATIC=/opt/noxa/static NOXA_DOCKER=1 \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

# Ubuntu's ffmpeg (built with NVENC, VAAPI, QSV); AMD VAAPI drivers, and Intel (amd64 only)
RUN apt-get update \
 && INTEL=$([ "$(dpkg --print-architecture)" = amd64 ] && echo intel-media-va-driver || true) \
 && apt-get install -y --no-install-recommends python3 ffmpeg ca-certificates openssl curl mesa-va-drivers $INTEL \
 && rm -rf /var/lib/apt/lists/* \
 && (userdel -r ubuntu 2>/dev/null || true) \
 && useradd --uid 1000 --user-group --home-dir /data --shell /usr/sbin/nologin noxa \
 && mkdir -p /data && chown noxa:noxa /data

WORKDIR /opt/noxa
COPY noxa ./noxa
COPY static ./static
RUN cd static/vendor && sha256sum -c --quiet hls.min.js.sha256 && (cd qrcode && sha256sum -c --quiet qrcode.mjs.sha256) \
 && (cd jassub && sha256sum -c --quiet jassub.sha256) && (cd libpgs && sha256sum -c --quiet libpgs.sha256)

USER noxa
VOLUME /data
EXPOSE 8420 8421 1900/udp
HEALTHCHECK --interval=30s --timeout=6s --start-period=20s \
  CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/status' % (os.environ.get('NOXA_PORT') or os.environ.get('SEANCE_PORT') or '8420'), timeout=5)"
CMD ["python3", "-m", "noxa"]
