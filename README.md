# MaxTools hosted downloader

A small public download backend for the MaxTools **YT Save** tab. It runs
`yt-dlp` + `ffmpeg` on a cloud server, so visitors do **not** install the local
helper and the owner's PC can be off.

It is intentionally **anonymous-only**:

- It does not accept browser cookies or Google account credentials.
- It does not log or retain cookies.
- Account-gated videos remain a separate security decision. Do not send a
  Google password to this service; a cookies.txt upload to a public server is
  also unsafe because cookies can grant full account access.

The service is cobalt-compatible for the subset MaxTools uses:

```text
GET  /                 health
POST /                 start a download, return a tunnel URL
GET  /file/<job-id>    stream the finished file (deleted after serving)
OPTIONS /              CORS preflight
```

## No card?

Oracle requires a card. If you cannot add one, see:

- `RENDER.md` — Render free tier, no card, but a datacenter IP may still be blocked.
- `HOME_DEVICE.md` — Raspberry Pi/NAS/always-on Linux device with Tailscale Funnel; uses your home IP and needs no card, but the device must stay on.

## Requirements

- A small Linux VPS (1 vCPU / 1–2 GB RAM is enough for audio and 720p; use
  2 vCPU / 2 GB if several people download at once).
- Docker Engine and Docker Compose on the VPS.
- A public hostname with TLS. You can use a free `sslip.io` name such as
  `api.203.0.113.10.sslip.io` if you do not own a domain.
- **A residential/ISP proxy is normally required.** YouTube shows its
  login/bot wall to datacenter IPs. Without a residential proxy, normal public
  videos may work, but login-walled videos such as `hjzAcsHbT0w` will still
  return an empty tunnel or `error.api.youtube.login`.
- Ports 80 and 443 reachable from the internet.

## Deploy

On your PC (this is only for copying files; the server runs independently):

```bash
scp -r hosted-backend user@YOUR_VPS:/opt/maxrtools-hosted
```

On the VPS:

```bash
cd /opt/maxrtools-hosted
cp .env.example .env
nano .env          # set DOMAIN, PUBLIC_BASE_URL and YTDLP_PROXY
docker compose up -d --build
docker compose ps
curl -fsS https://YOUR_DOMAIN/
```

`DOMAIN` and `PUBLIC_BASE_URL` must match. Example:

```dotenv
DOMAIN=api.203.0.113.10.sslip.io
PUBLIC_BASE_URL=https://api.203.0.113.10.sslip.io
```

Caddy obtains and renews the HTTPS certificate automatically. Do not expose
port 8080 publicly; only Caddy needs 80/443.

### Residential proxy

Set the proxy in `.env`:

```dotenv
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
curl -fsS "$API/"

# audio
curl -fsS -X POST "$API/" \
  -H 'Origin: https://maxrtools.netlify.app' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=hjzAcsHbT0w","downloadMode":"audio","audioFormat":"mp3"}'
```

The response contains `{"status":"tunnel","url":"...","filename":"..."}`. Fetch
that URL; the file should be a valid MP3/MP4 and non-zero. The tunnel file is
removed after it is served or after `JOB_TTL` seconds.

## Limits and abuse

Defaults are deliberately modest:

- 2 concurrent downloads
- 8 requests per IP per 60 seconds
- 10-minute download timeout
- 100 MB maximum file size
- 30-minute maximum video duration
- 1080p maximum output height

Edit `.env` and restart if needed. This service has **no login**; if you open it
to a large audience, add Cloudflare Turnstile or an access token, monitor
bandwidth, and keep the proxy bill in mind. Do not run it as an open download
proxy for arbitrary URLs — the server only accepts YouTube links.

## Updating yt-dlp

YouTube changes frequently. Rebuild periodically:

```bash
cd /opt/maxrtools-hosted
docker compose build --no-cache api
docker compose up -d
curl -fsS https://YOUR_DOMAIN/
```

## Connecting MaxTools

After the API is healthy, change the page's default server from the public
cobalt instance to the hosted URL, or enter the URL in **YT Save → Server** for
testing. The existing page sends account fields only to loopback, so the
hosted service never receives them.

The public downloader is anonymous-only. Account login remains available only
through the local helper, where cookies stay on the user's device.
