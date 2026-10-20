FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TMPDIR=/tmp \
    USE_WARP=1 \
    YTDLP_PROXY=socks5h://127.0.0.1:25344

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade yt-dlp \
    && curl -fL --retry 3 -o /usr/local/bin/wgcf \
       https://github.com/ViRb3/wgcf/releases/download/v2.3.1/wgcf_2.3.1_linux_amd64 \
    && chmod 0755 /usr/local/bin/wgcf \
    && curl -fL --retry 3 -o /tmp/wireproxy.tar.gz \
       https://github.com/windtf/wireproxy/releases/download/v1.1.3/wireproxy_linux_amd64.tar.gz \
    && tar -xzf /tmp/wireproxy.tar.gz -C /usr/local/bin wireproxy \
    && chmod 0755 /usr/local/bin/wireproxy \
    && rm -f /tmp/wireproxy.tar.gz

WORKDIR /app
COPY server.py /app/server.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod 0755 /app/entrypoint.sh

RUN useradd --create-home --uid 10001 app \
    && chown -R app:app /app /tmp
USER app

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/' % os.environ.get('PORT', '8080'), timeout=4).read()"

ENTRYPOINT ["/app/entrypoint.sh"]
