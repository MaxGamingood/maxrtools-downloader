# Render free tier (no card) — current deployment

The public service runs on Render's free web-service tier:

```text
https://maxrtools-downloader.onrender.com
```

It uses the Docker image in this folder. No card is required. The image starts
a fresh free Cloudflare WARP identity at boot and routes `yt-dlp` through a
local SOCKS5 proxy, so YouTube does not see Render's datacenter IP directly.
The login-gated test video `hjzAcsHbT0w` has been tested end-to-end (MP3 and
720p MP4), along with ordinary videos.

WARP is a free third-party network path, not a permanent guarantee. If
Cloudflare blocks it, use a residential/ISP proxy or the home-device setup in
`HOME_DEVICE.md`.

## Service settings

- Runtime: Docker
- Plan: Free
- Region: Oregon
- Branch: `main`
- Dockerfile: `./Dockerfile`
- Health check path: `/health`
- Public repo: <https://github.com/MaxGamingood/maxrtools-downloader>

Environment:

```text
ALLOWED_ORIGINS=https://maxrtools.netlify.app
TRUST_PROXY=1
MAX_CONCURRENT=1
RATE_LIMIT=6
RATE_WINDOW=60
DOWNLOAD_TIMEOUT=600
JOB_TTL=300
MAX_FILESIZE=100M
MAX_DURATION=1800
MAX_HEIGHT=720
STREAM_FRAGMENTS=4
STREAM_START_TIMEOUT=55
```

The image defaults to `USE_WARP=1` and
`YTDLP_PROXY=socks5h://127.0.0.1:25344`. Do not set `YTDLP_PROXY` in the
Render dashboard unless you are deliberately replacing WARP. Render supplies
`PORT`; do not set `PUBLIC_BASE_URL` manually because the server derives it
from the forwarded host/proto headers.

## Deploy from scratch

1. Push this folder to a public GitHub repository. The required files are
   `Dockerfile`, `entrypoint.sh`, `server.py`, `render.yaml`, and the docs.
2. In Render: **New → Web Service**, connect the repo, choose **Docker** and
   **Free**.
3. Set the health check path to `/health` and add the environment variables
   above.
4. Deploy. The first build downloads `ffmpeg`, `yt-dlp`, `wgcf`, and
   `wireproxy`, then starts WARP before the HTTP server.
5. The free service is live at a URL like
   `https://maxrtools-downloader.onrender.com`.

## Test

The first request after a 15-minute sleep can take about a minute while Render
starts the container and WARP connects.

```bash
API=https://maxrtools-downloader.onrender.com
curl -fsS "$API/health"

curl -fsS -X POST "$API/" \
  -H 'Origin: https://maxrtools.netlify.app' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=hjzAcsHbT0w","downloadMode":"audio","audioFormat":"mp3"}'
```

Expected: a JSON response with `"status":"tunnel"` and a `/file/...` URL.
Fetch that URL and verify a non-zero MP3. A 720p MP4 request uses
`"downloadMode":"video","videoQuality":"720"`.

Fast stream (v94 page):

```bash
curl -fsS -X POST "$API/stream" \
  -H 'Origin: https://maxrtools.netlify.app' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=hjzAcsHbT0w","downloadMode":"audio","audioFormat":"mp3"}' \
  -o test.mp3
```

The response is chunked and starts before the download finishes. Verify the
MP3 is non-zero and playable. Use `"downloadMode":"auto","videoQuality":"720"`
for MP4. The server tries WARP first and retries WARP once before falling back
to direct; it only sends JSON after the attempts fail, never a broken media
file.

## Limits to expect

- 15-minute idle spin-down; cold starts are slow.
- 512 MB RAM / 0.1 CPU: audio is comfortable; 720p can take tens of seconds.
- No persistent disk: temp files disappear on restart.
- Free bandwidth is limited; public use can exhaust it.
- WARP egress can be rate-limited or blocked; keep the home-device option as
  a fallback.
- The service is anonymous-only and does not accept account cookies.
