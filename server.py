#!/usr/bin/env python3
"""MaxTools hosted download service.

A small, self-hosted yt-dlp/ffmpeg API for the MaxTools YouTube Downloader.
It is intentionally anonymous-only: it does not accept browser cookies or
account credentials. Run it on a public VPS behind HTTPS (see docker-compose
and Caddyfile); the MaxTools page can then use it with no local helper.

The service is cobalt-compatible for the subset MaxTools needs:
    GET  /                 -> health
    POST /                 -> {"status":"tunnel","url":...,"filename":...}
    GET  /file/<job-id>    -> finished file (removed after it is served)
    OPTIONS /              -> CORS preflight

Configuration is via environment variables; see .env.example and README.md.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = threading.Lock()
JOBS = {}
RATE = {}
JOB_TTL = float(os.environ.get('JOB_TTL', '300'))
TIMEOUT = int(os.environ.get('DOWNLOAD_TIMEOUT', '600'))
MAX_BODY = 64 * 1024
MAX_COOKIES = 0
SITE_ORIGINS = [o.strip().rstrip('/') for o in os.environ.get(
    'ALLOWED_ORIGINS', 'https://maxrtools.netlify.app').split(',') if o.strip()]
MAX_CONCURRENT = max(1, int(os.environ.get('MAX_CONCURRENT', '2')))
RATE_LIMIT = max(1, int(os.environ.get('RATE_LIMIT', '8')))
RATE_WINDOW = max(1, int(os.environ.get('RATE_WINDOW', '60')))
MAX_FILESIZE = os.environ.get('MAX_FILESIZE', '100M').strip()
MAX_DURATION = int(os.environ.get('MAX_DURATION', '1800'))
MAX_HEIGHT = int(os.environ.get('MAX_HEIGHT', '1080'))
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', '').strip().rstrip('/')
TRUST_PROXY = os.environ.get('TRUST_PROXY', '0').lower() in ('1', 'true', 'yes')
YTDLP_PROXY = os.environ.get('YTDLP_PROXY', '').strip()
YT_RE = re.compile(r'^https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/', re.I)
QUAL = ('max', '2160', '1440', '1080', '720', '480', '360', '240')
AUDIO = ('mp3', 'm4a', 'opus', 'wav')
SEM = threading.BoundedSemaphore(MAX_CONCURRENT)


def find_ytdlp():
    for candidate in (os.environ.get('YTDLP_BIN'), shutil.which('yt-dlp'),
                      os.path.join(HERE, 'yt-dlp')):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def yt_version(binary):
    try:
        out = subprocess.run([binary, '--version'], capture_output=True, timeout=20)
        if out.returncode == 0:
            return out.stdout.decode('utf-8', 'replace').strip()
    except Exception:
        pass
    return None


def error(code, detail=None):
    body = {'code': code}
    if detail:
        body['detail'] = str(detail)[-300:]
    return {'status': 'error', 'error': body}


def build_cmd(binary, req, outdir, proxy=None):
    quality = str(req.get('videoQuality') or '720').strip()
    if quality not in QUAL:
        quality = '720'
    if quality != 'max':
        try:
            quality = str(min(int(quality), MAX_HEIGHT))
        except ValueError:
            quality = '720'
    out = os.path.join(outdir, '%(title).80s [%(id)s].%(ext)s')
    cmd = [binary, '--no-playlist', '--no-progress', '--newline',
           '--socket-timeout', '30', '--retries', '3',
           '--fragment-retries', '3', '-o', out]
    if proxy:
        cmd += ['--proxy', proxy]
    if MAX_FILESIZE:
        cmd += ['--max-filesize', MAX_FILESIZE]
    if MAX_DURATION > 0:
        cmd += ['--match-filter', 'duration <= %d' % MAX_DURATION]
    if str(req.get('downloadMode') or '') == 'audio':
        fmt = str(req.get('audioFormat') or 'mp3').lower()
        if fmt not in AUDIO:
            fmt = 'mp3'
        cmd += ['-x', '--audio-format', fmt, '--audio-quality', '0',
                '-f', 'bestaudio/best']
    else:
        cap = '' if quality == 'max' else '[height<=' + quality + ']'
        cmd += ['-f',
                'bv*' + cap + '[vcodec^=avc1]+ba[ext=m4a]/b' + cap +
                '[ext=mp4]/bv*' + cap + '+ba/b' + cap + '/b',
                '--merge-output-format', 'mp4']
    cmd.append(str(req.get('url') or ''))
    return cmd


def safe_detail(text):
    text = str(text or '')
    if YTDLP_PROXY:
        # Never echo proxy credentials if yt-dlp includes the URL in an error.
        text = text.replace(YTDLP_PROXY, '[proxy]')
        try:
            parsed = urlparse(YTDLP_PROXY)
            if parsed.password:
                text = text.replace(parsed.password, '[proxy-password]')
        except Exception:
            pass
    return text[-300:]


def classify_failure(stderr):
    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', 'replace')
    low = str(stderr or '').lower()
    if 'sign in to confirm' in low or 'login' in low or 'bot' in low:
        return 'error.api.youtube.login'
    if 'video unavailable' in low or 'private video' in low:
        return 'error.api.fetch.fail'
    if 'timed out' in low or 'timeout' in low:
        return 'error.api.timeout'
    return 'error.api.fetch.fail'


def client_ip(handler):
    if TRUST_PROXY:
        xff = handler.headers.get('X-Forwarded-For', '')
        if xff:
            return xff.split(',', 1)[0].strip()
    return handler.client_address[0]


def rate_ok(ip):
    now = time.time()
    with LOCK:
        hits = [t for t in RATE.get(ip, []) if now - t < RATE_WINDOW]
        if len(hits) >= RATE_LIMIT:
            RATE[ip] = hits
            return False
        hits.append(now)
        RATE[ip] = hits
        # Keep the in-memory map bounded.
        if len(RATE) > 5000:
            for key in list(RATE.keys())[:1000]:
                RATE.pop(key, None)
    return True


def base_url(handler):
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    host = handler.headers.get('X-Forwarded-Host', '').split(',', 1)[0].strip()
    host = host or handler.headers.get('Host') or ('127.0.0.1:%d' % handler.server.server_address[1])
    proto = handler.headers.get('X-Forwarded-Proto', 'http').split(',', 1)[0].strip()
    if proto not in ('http', 'https'):
        proto = 'http'
    return '%s://%s' % (proto, host)


def cleanup_jobs():
    while True:
        time.sleep(30)
        now = time.time()
        with LOCK:
            expired = [(jid, job) for jid, job in JOBS.items()
                       if now - job['created'] > JOB_TTL]
            for jid, job in expired:
                JOBS.pop(jid, None)
                shutil.rmtree(os.path.dirname(job['path']), ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'MaxTools-hosted-downloader'

    def log_message(self, fmt, *args):
        print('[hosted] ' + (fmt % args), flush=True)

    def _origin(self):
        return self.headers.get('Origin', '').rstrip('/')

    def _origin_allowed(self):
        origin = self._origin()
        if not origin:
            return True          # curl / native clients
        return origin in SITE_ORIGINS

    def _cors(self):
        origin = self._origin()
        if origin and self._origin_allowed():
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Access-Control-Max-Age', '86400')
        self.send_header('X-Content-Type-Options', 'nosniff')

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for key, val in (extra or {}).items():
            self.send_header(key, val)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, code, obj):
        self._send(code, 'application/json', json.dumps(obj).encode('utf-8'))

    def do_OPTIONS(self):
        if not self._origin_allowed():
            self.send_response(403)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self.send_response(204)
        self._cors()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_HEAD(self):
        # Render's health checker probes with HEAD; answer it without running
        # yt-dlp or writing a response body.
        if not self._origin_allowed():
            self.send_response(403)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        path = urlparse(self.path).path
        if path in ('/', '/health'):
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self.send_response(404)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        if not self._origin_allowed():
            return self._json(403, error('error.api.origin.denied'))
        path = urlparse(self.path).path
        if path.startswith('/file/'):
            return self._serve(path.split('/', 2)[2])
        if path == '/health':
            return self._json(200, {
                'status': 'ok',
                'name': 'MaxTools hosted downloader',
                'account': False,
            })
        if path == '/':
            binary = find_ytdlp()
            return self._json(200, {
                'status': 'ok',
                'name': 'MaxTools hosted downloader',
                'account': False,
                'proxy': bool(YTDLP_PROXY),
                'ytdlp': yt_version(binary) if binary else None,
            })
        self._json(404, error('error.file.gone'))

    def _serve(self, jid):
        with LOCK:
            job = JOBS.pop(jid, None)
        if not job or not os.path.isfile(job['path']):
            return self._json(404, error('error.api.fetch.empty'))
        size = os.path.getsize(job['path'])
        ext = os.path.splitext(job['name'])[1].lower() or '.bin'
        if not re.match(r'^\.[a-z0-9]{1,8}$', ext):
            ext = '.bin'
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(size))
        self.send_header('Content-Disposition', 'attachment; filename="download%s"' % ext)
        self.end_headers()
        try:
            with open(job['path'], 'rb') as fh:
                shutil.copyfileobj(fh, self.wfile, 1024 * 256)
        except Exception:
            pass
        finally:
            shutil.rmtree(os.path.dirname(job['path']), ignore_errors=True)

    def do_POST(self):
        if urlparse(self.path).path != '/':
            return self._json(404, error('error.api.link.invalid'))
        if not self._origin_allowed():
            self.close_connection = True
            return self._json(403, error('error.api.origin.denied'))
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self.close_connection = True
            return self._json(400, error('error.api.link.invalid'))
        if length > MAX_BODY:
            self.close_connection = True
            return self._json(413, error('error.api.body.too_large'))
        try:
            req = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
        except Exception:
            return self._json(400, error('error.api.link.invalid'))
        if not isinstance(req, dict):
            return self._json(400, error('error.api.link.invalid'))
        url = str(req.get('url') or '').strip()
        if not YT_RE.match(url):
            return self._json(200, error('error.api.link.invalid'))
        if req.get('cookiesTxt') or req.get('cookiesFromBrowser'):
            # Deliberately unsupported: this is a public anonymous service.
            return self._json(200, error('error.api.cookies.unsupported',
                                         'Account cookies are not accepted by the hosted downloader'))

        ip = client_ip(self)
        if not rate_ok(ip):
            return self._json(429, error('error.api.rate_exceeded'))
        if not SEM.acquire(blocking=False):
            return self._json(429, error('error.api.rate_exceeded'))
        try:
            binary = find_ytdlp()
            if not binary:
                return self._json(200, error('error.api.ytdlp.missing'))
            outdir = tempfile.mkdtemp(prefix='maxrtools-hosted-')
            keep = False
            try:
                def attempt(proxy):
                    cmd = build_cmd(binary, req, outdir, proxy)
                    started = time.time()
                    try:
                        proc = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT)
                    except FileNotFoundError:
                        return None, None, 'error.api.ytdlp.missing'
                    except subprocess.TimeoutExpired:
                        return None, None, 'error.api.timeout'
                    files = [os.path.join(outdir, f) for f in os.listdir(outdir)
                             if not f.endswith('.part')]
                    files = [f for f in files if os.path.isfile(f) and os.path.getsize(f) > 0]
                    elapsed = time.time() - started
                    if proc.returncode == 0 and files:
                        path = max(files, key=os.path.getsize)
                        print('[hosted] ok %.1fs mode=%s bytes=%d proxy=%s' % (
                            elapsed, req.get('downloadMode') or 'video',
                            os.path.getsize(path), 'yes' if proxy else 'no'), flush=True)
                        return path, None, None
                    tail = ' '.join((proc.stderr or b'').decode('utf-8', 'replace')
                                    .strip().splitlines()[-2:])
                    tail = safe_detail(tail)
                    # Clear partial output before another attempt.
                    for name in os.listdir(outdir):
                        p = os.path.join(outdir, name)
                        if os.path.isfile(p):
                            try:
                                os.unlink(p)
                            except OSError:
                                pass
                        elif os.path.isdir(p):
                            shutil.rmtree(p, ignore_errors=True)
                    print('[hosted] fail %.1fs proxy=%s code=%s' % (
                        elapsed, 'yes' if proxy else 'no',
                        classify_failure(proc.stderr or b'')), flush=True)
                    return None, tail, None

                path, direct_detail, fatal = attempt(None)
                if fatal:
                    return self._json(200, error(fatal))
                proxy_detail = None
                if not path and YTDLP_PROXY:
                    path, proxy_detail, fatal = attempt(YTDLP_PROXY)
                    if fatal:
                        return self._json(200, error(fatal))
                if not path:
                    detail = direct_detail or ''
                    if proxy_detail:
                        detail = ('direct: ' + detail + ' | proxy: ' + proxy_detail)[-300:]
                    return self._json(200, error(classify_failure(detail), detail))
                jid = uuid.uuid4().hex
                now = time.time()
                with LOCK:
                    JOBS[jid] = {'path': path, 'name': os.path.basename(path),
                                 'created': now}
                keep = True
                return self._json(200, {
                    'status': 'tunnel',
                    'url': base_url(self) + '/file/' + jid,
                    'filename': os.path.basename(path),
                })
            except Exception as exc:
                print('[hosted] exception: ' + traceback.format_exc()[-4000:], flush=True)
                return self._json(200, error('error.api.fetch.fail', safe_detail(exc)))
            finally:
                if not keep:
                    shutil.rmtree(outdir, ignore_errors=True)
        finally:
            SEM.release()


def main():
    ap = argparse.ArgumentParser(description='MaxTools hosted yt-dlp downloader')
    ap.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8080')))
    ap.add_argument('--bind', default=os.environ.get('BIND', '0.0.0.0'))
    args = ap.parse_args()
    binary = find_ytdlp()
    threading.Thread(target=cleanup_jobs, daemon=True).start()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.daemon_threads = True
    print('MaxTools hosted downloader on %s:%d' % (args.bind, server.server_address[1]), flush=True)
    print('yt-dlp: %s' % ((binary + ' (' + str(yt_version(binary)) + ')') if binary else 'NOT FOUND'), flush=True)
    print('origins: %s' % ', '.join(SITE_ORIGINS or ['(none)']), flush=True)
    print('anonymous only; proxy=%s; concurrency=%d; rate=%d/%ds' % (
        'configured' if YTDLP_PROXY else 'none', MAX_CONCURRENT, RATE_LIMIT, RATE_WINDOW), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
