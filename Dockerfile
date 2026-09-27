FROM golang:1.26.8-bookworm AS build
ARG SONOSCLI_COMMIT=b7850a276c8c549532f3fcd88c29dad68facf2b4
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/steipete/sonoscli.git /src && cd /src && git checkout "$SONOSCLI_COMMIT"
WORKDIR /src
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/sonos ./cmd/sonos

FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg tzdata && rm -rf /var/lib/apt/lists/* && python -m pip install --no-cache-dir yt-dlp && useradd --create-home --home-dir /data --uid 10001 sonos
ENV PYTHONUNBUFFERED=1 HOME=/data XDG_CONFIG_HOME=/data/config XDG_CACHE_HOME=/data/cache XDG_STATE_HOME=/data/state TMPDIR=/data/tmp
WORKDIR /data
COPY --from=build /out/sonos /usr/local/bin/sonos
COPY --chown=10001:10001 app/ /app/
USER sonos
ENTRYPOINT ["python3", "-u", "/app/server.py"]
