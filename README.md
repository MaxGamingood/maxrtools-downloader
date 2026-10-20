# MaxTools hosted downloader

A small public download backend for the MaxTools **YT Save** tab. It runs
`yt-dlp` + `ffmpeg` on a cloud server, so visitors do **not** install the local
helper and the owner's PC can be off.

The hosted image uses a free Cloudflare WARP egress by default. That avoids
YouTube's usual datacenter login/bot wall without a card or a residential
proxy; normal videos and the login-gated test video both work through it.
WARP is a free network path, not a guarantee: if Cloudflare changes or blocks
it, use a residential proxy or the home-device option below.

It is intentionally **anonymous-only**:

- It does not accept browser cookies or Google account credentials.
- It does not log or retain cookies.
- Account-gated videos remain a separate security decision. Do not send a
  Google password to this service; a cookies.txt upload to a public server is
  also unsafe because cookies can grant full account access.

The service is cobalt-compatible for the subset MaxTools uses:

```text
GET  /                 status
GET  /health           lightweight health check
POST /                 start a download, return a tunnel URL
GET  /file/<job-id>    stream the finished file (deleted after serving)
HEAD /                 health-check probe
OPTIONS /              CORS preflight
```

## No card?

Oracle requires a card. The current public deployment is on Render's free
tier (no card) with WARP egress; it passed the target-video test.

- `RENDER.md` — Render free tier, no card, WARP egress (the current setup).
- `HOME_DEVICE.md` — Raspberry Pi/NAS/always-on Linux device with Tailscale
  Funnel; uses your home IP and needs no card, but the device must stay on.

## Requirements

- For Render: a free Render account and a public Git repository. No card.
- For a self-hosted VPS: a small Linux server (1 vCPU / 1–2 GB RAM is enough
  for audio and 720p), Docker Engine + Docker Compose, and a public hostname
  with TLS.
- Ports 80 and 443 reachable from the internet for the self-hosted Caddy setup.
- **Egress:** the Render image starts a userspace Cloudflare WARP client and
  routes yt-dlp through it. For a self-hosted VPS you can enable WARP, use a
  residential/ISP proxy, or run on a home connection. A residential proxy is
  no longer required for the hosted setup.

## Deploy

On your PC (this is only for copying files; the server runs independently):

```bash
scp -r hosted-backend user@YOUR_VPS:/opt/maxrtools-hosted
```

On the VPS:

```bash
cd /opt/maxrtools-hosted
cp .env.example .env
nano .env          # set DOMAIN and PUBLIC_BASE_URL; keep USE_WARP=0 for home IP
docker compose up -d --build
docker compose ps
curl -fsS https://YOUR_DOMAIN/healthhealth
```

`DOMAIN` and `PUBLIC_BASE_URL` must match. Example:

```dotenv
DOMAIN=api.203.0.113.10.sslip.io
PUBLIC_BASE_URL=https://api.203.0.113.10.sslip.io
```

Caddy obtains and renews the HTTPS certificate automatically. Do not expose
port 8080 publicly; only Caddy needs 80/443.

For Render, use the Docker image directly. The image defaults to
`USE_WARP=1`; you do not need an `.env` file or a card. See `RENDER.md`.

### WARP (Render default)

The image starts a fresh free WARP identity at boot and exposes it as a local
SOCKS5 proxy. The downloader then uses
`YTDLP_PROXY=socks5h://127.0.0.1:25344`. No user Google account or cookies are
involved. The account is ephemeral and is recreated when the container
restarts.

Keep `USE_WARP=1` only when the container's own network is a datacenter (Render
or a VPS). For a home device, use `USE_WARP=0` so downloads use your home IP.
If WARP is blocked, switch to a residential/ISP proxy or the home-device setup.

### Residential proxy (optional)

For a self-hosted VPS, disable WARP and set a proxy in `.env`:

```dotenv
USE_WARP=0
YTDLP_PROXY=http://USER:PASSWORD@HOST:PORT
```

The service tries the VPS direct first, then retries with the proxy only if
the direct attempt fails. This keeps normal traffic cheap. Use a reputable
residential or ISP proxy; free public proxies are unreliable and unsafe.
Proxy credentials stay in `.env` and are not returned by the API.

> If YouTube still says “Sign in to confirm you’re not a bot” even through the
> proxy, update yt-dlp and try another proxy location. Account cookies are not
> accepted by this service.

## Test it

Replace the host with your deployed HTTPS hostname:

```bash
API=https://api.203.0.113.10.sslip.io

# health
curl -fsS "$API/health"

# audio (also works for the login-gated target through WARP)
curl -fsS -X POST "$API/" \
  -H 'Origin: https://maxrtools.netlify.app' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=hjzAcsHbT0w","downloadMode":"audio","audioFormat":"mp3"}'
```

The response contains `{"status":"tunnel","url":"...","filename":"..."}`. Fetch
that URL; the file should be a valid MP3/MP4 and non-zero. The tunnel file is
removed after it is served or after `JOB_TTL` seconds.

## Limits and abuse

The Render service is configured with deliberately modest limits:

- 1 concurrent download
- 6 requests per IP per 60 seconds
- 10-minute download timeout
- 100 MB maximum file size
- 30-minute maximum video duration
- 720p maximum output height

The `.env.example` defaults for a private self-host are 2 concurrent and 8
requests per minute. Edit `.env` and restart if needed. This service has **no
login**; if you open it to a large audience, add Cloudflare Turnstile or an
access token, monitor bandwidth, and keep the proxy bill in mind. Do not run it
as an open download proxy for arbitrary URLs — the server only accepts YouTube
links.

## Updating yt-dlp

YouTube changes frequently. Rebuild periodically:

```bash
cd /opt/maxrtools-hosted
docker compose build --no-cache api
docker compose up -d
curl -fsS https://YOUR_DOMAIN/health
```

## Connecting MaxTools

The prepared v90 page uses
`https://maxrtools-downloader.onrender.com` as its default server. Until that
page is published, enter the URL in **YT Save → Server** or use the existing
default. The page sends account fields only to loopback, so the hosted service
never receives them.

The public downloader is anonymous-only. Account login remains available only
through the local helper, where cookies stay on the user's device. The page
tries the hosted server anonymously first and falls back to the local helper
only when needed.
