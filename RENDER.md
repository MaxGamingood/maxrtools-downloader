# Try Render's free tier (no card)

Render's free web-service tier does not require a credit card. It is a **test**
option, not a guaranteed fix: Render runs on datacenter IPs, so YouTube may
still block the target video. It also spins down after 15 minutes of inactivity
and has 0.1 CPU / 512 MB RAM.

If it works for your video, great. If the target still fails, you need a
residential proxy or a machine on a home connection.

## 1. Put the folder in a public Git repository

Render deploys from GitHub/GitLab/Bitbucket (or a public Git URL). The easiest
path is a free GitHub account:

1. Create a new **public** repository, e.g. `maxrtools-downloader`.
2. Upload the contents of `/home/maxr/hosted-backend/` (not the folder itself):
   - `Dockerfile`
   - `server.py`
   - `render.yaml`
   - `README.md`
   - `ORACLE.md`
   - `Caddyfile`
   - `docker-compose.yml`
   - `.env.example`
   - `.dockerignore`
   - `deploy-oracle.sh`
3. Commit.

The `Dockerfile` is enough; `render.yaml` is optional.

## 2. Create the Render service

1. Sign up at <https://dashboard.render.com/register> with GitHub (no card).
2. **New → Web Service**.
3. Connect the repository.
4. Runtime: **Docker**.
5. Plan: **Free**.
6. Health check path: `/`.
7. Add environment variables:

   ```text
   ALLOWED_ORIGINS=https://maxrtools.netlify.app
   TRUST_PROXY=1
   MAX_CONCURRENT=1
   RATE_LIMIT=6
   RATE_WINDOW=60
   MAX_HEIGHT=720
   MAX_FILESIZE=100M
   MAX_DURATION=1800
   ```

   `PORT` is provided by Render. Do not set `PUBLIC_BASE_URL` manually; the
   server derives it from Render's forwarded host/proto.
8. Deploy.

Render gives you an HTTPS URL such as:

```text
https://maxrtools-downloader.onrender.com
```

## 3. Test

The first request after a sleep may take about a minute.

```bash
API=https://maxrtools-downloader.onrender.com
curl -fsS "$API/"

curl -fsS -X POST "$API/" \
  -H 'Origin: https://maxrtools.netlify.app' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=hjzAcsHbT0w","downloadMode":"audio","audioFormat":"mp3"}'
```

If the POST returns a tunnel URL, fetch it and check the size. If it returns
`error.api.youtube.login` or an empty tunnel, Render's IP is blocked and no
amount of code will fix that without a residential proxy.

## 4. If it works

Point the page's server field at the Render URL for a test, or update
`YT_API_DEFAULT` in `tools.html` and bump `CHANGELOG_V` for production.

## Limits to expect

- 15-minute idle spin-down: the first request after that is slow.
- 512 MB RAM / 0.1 CPU: audio is realistic; 720p video may be slow or fail.
- No persistent disk: temp files disappear on restart; that is fine.
- Free bandwidth is limited; public use can exhaust it.
- Render is not a residential IP; the target may still fail.
