# No-card option: an always-on home device

If you have a Raspberry Pi, a NAS with Docker, an old mini PC, or another Linux
device that can stay on, you can run the same backend at home. That gives you a
residential IP (so login-walled videos are much more likely to work) and needs
no credit card or cloud account. Your main PC can be off.

**This is still a device running at home.** It is not a cloud service, but
visitors only use the MaxTools page; they do not install anything.

## Requirements

- Always-on Linux device with Docker (Raspberry Pi 4/5 with 2 GB+ RAM works;
  a NAS with Docker is ideal).
- Home internet that can stay connected.
- Tailscale account (free Personal plan, no card) for a public HTTPS URL.

## 1. Copy and start the backend

On the device:

```bash
# from /path/to/hosted-backend
docker build -t maxrtools-downloader .
cp .env.example .env
nano .env
```

Set at least:

```dotenv
BIND=0.0.0.0
PORT=8080
ALLOWED_ORIGINS=https://maxrtools.netlify.app
TRUST_PROXY=1
PUBLIC_BASE_URL=https://YOUR_DEVICE.YOUR_TAILNET.ts.net
MAX_CONCURRENT=1
RATE_LIMIT=6
RATE_WINDOW=60
MAX_HEIGHT=720
```

Leave `DOMAIN`, `YTDLP_PROXY` and the Caddy settings unused for this setup.

Run only the API container, bound to loopback so it is not exposed directly on
the LAN:

```bash
docker run -d --name maxrtools-downloader \
  --restart unless-stopped \
  --env-file .env \
  -p 127.0.0.1:8080:8080 \
  maxrtools-downloader

curl -fsS http://127.0.0.1:8080/
```

## 2. Publish it with Tailscale Funnel

Install Tailscale on the device:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale funnel 8080
```

Tailscale prints a public HTTPS URL like:

```text
https://your-device.your-tailnet.ts.net
```

Set `PUBLIC_BASE_URL` to exactly that URL, then restart the container:

```bash
nano .env
docker rm -f maxrtools-downloader
docker run -d --name maxrtools-downloader \
  --restart unless-stopped \
  --env-file .env \
  -p 127.0.0.1:8080:8080 \
  maxrtools-downloader

curl -fsS https://YOUR_DEVICE.YOUR_TAILNET.ts.net/
```

Funnel provides the public TLS certificate. No port forwarding, domain purchase
or router changes are needed.

## 3. Test the target video

```bash
API=https://YOUR_DEVICE.YOUR_TAILNET.ts.net
curl -fsS -X POST "$API/" \
  -H 'Origin: https://maxrtools.netlify.app' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=hjzAcsHbT0w","downloadMode":"audio","audioFormat":"mp3"}'
```

Fetch the returned tunnel URL and check that the file is non-zero. Because the
request leaves from a normal home connection, the target should work without a
residential proxy.

## Warnings

- Funnel makes the API publicly reachable. The service has rate limits and
  only accepts YouTube links, but it is still an open download endpoint. Do not
  run it on a device holding sensitive data.
- Keep the container bound to `127.0.0.1:8080`; do not expose port 8080 on your
  router.
- The home device must stay powered and online. If it sleeps, downloads stop.
- Account cookies are still not accepted. Account-gated videos remain
  local-helper-only.
