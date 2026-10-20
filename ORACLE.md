> **Card required.** Oracle asks for a card for identity verification. If you cannot add one, use `RENDER.md` (free, no card) or `HOME_DEVICE.md` (home device, no card).

# Deploy on Oracle Cloud Always Free

This gets the hosted downloader running 24/7 on a free ARM VM. Your PC can be
off after deployment.

## 1. Create the Oracle account

1. Go to <https://signup.oraclecloud.com/> and choose the **Always Free** tier.
2. Use a real home region close to you. It cannot be changed later.
3. Oracle asks for a card for identity verification; Always Free resources are
   not charged if you stay within the free limits.

## 2. Create the VM

In the Oracle console: **Compute → Instances → Create instance**.

- **Name:** `maxrtools-yt`
- **Image:** Canonical Ubuntu 24.04 (make sure the image is **aarch64**).
- **Shape:** Ampere → `VM.Standard.A1.Flex`
  - 1 OCPU and 6 GB RAM is more than enough.
  - If A1 capacity is unavailable, try another availability domain or wait;
    the free AMD micro shape (1 GB) can run audio but is tight for video.
- **Networking:** assign a public IPv4 address and use a public subnet.
- **SSH keys:** upload your public key or let Oracle generate a key pair and
  download the private key.
- **Boot volume:** 50 GB is enough.

Wait for the instance to show **Running**, then copy its public IP.

## 3. Open ports 80 and 443

Oracle blocks these by default at two layers.

### VCN security list

**Networking → Virtual Cloud Networks → your VCN → Security Lists → Default
Security List → Add Ingress Rules**:

| Source | Protocol | Destination port |
|---|---|---|
| `0.0.0.0/0` | TCP | `80` |
| `0.0.0.0/0` | TCP | `443` |

Keep SSH (port 22) limited to your own IP. Do not open port 8080.

The deploy script adds the matching Ubuntu `iptables` rules, but the VCN rules
must be added in the console.

## 4. Upload the files

From this PC:

```bash
cd /home/maxr
scp -i /path/to/your-oracle-key -r hosted-backend ubuntu@YOUR_PUBLIC_IP:/home/ubuntu/
```

Or, if you already copied the files another way, just run the script on the VM.

## 5. Run the installer

```bash
ssh -i /path/to/your-oracle-key ubuntu@YOUR_PUBLIC_IP
cd ~/hosted-backend
chmod +x deploy-oracle.sh
./deploy-oracle.sh
```

The script:

- installs Docker,
- builds the API and Caddy containers,
- uses `api.YOUR_PUBLIC_IP.sslip.io` as a free HTTPS hostname,
- starts the service and waits for a healthy response.

It prints something like:

```text
API URL: https://api.203.0.113.10.sslip.io
Health:  {"status":"ok","name":"MaxTools hosted downloader","account":false,...}
```

No DNS purchase is needed: `sslip.io` resolves the IP embedded in the name, and
Caddy obtains a Let's Encrypt certificate automatically.

## 6. Test the real video

From this PC:

```bash
API=https://api.YOUR_PUBLIC_IP.sslip.io
curl -fsS "$API/"

curl -fsS -X POST "$API/" \
  -H 'Origin: https://maxrtools.netlify.app' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=hjzAcsHbT0w","downloadMode":"audio","audioFormat":"mp3"}'
```

The POST should return a tunnel URL. Fetch it:

```bash
curl -fL -o test.mp3 'THE_TUNNEL_URL_FROM_THE_RESPONSE'
file test.mp3
```

If YouTube blocks the Oracle IP, the POST returns an error or the tunnel is
empty. That means the VM needs a residential proxy:

```bash
nano /opt/maxrtools-hosted/.env
# YTDLP_PROXY=http://user:password@host:port
cd /opt/maxrtools-hosted
sudo docker compose up -d
```

Then repeat the test. Without a residential proxy there is no reliable way to
download login-walled videos from a cloud VM.

## 7. Connect MaxTools

After the API is healthy and the target test succeeds, point the page at it.
For a temporary test, open **YT Save → Server**, enter the API URL, and press
Download. For a permanent change, update `YT_API_DEFAULT` in `tools.html`,
bump `CHANGELOG_V`, test, and deploy.

Do not switch the live default until the hosted API passes the target-video
test. A broken default would be worse than the current public cobalt fallback.

## Updating

Rebuild periodically for the newest yt-dlp:

```bash
cd /opt/maxrtools-hosted
sudo docker compose build --no-cache api
sudo docker compose up -d
```

Check logs with:

```bash
sudo docker compose logs --tail=100 api
```
