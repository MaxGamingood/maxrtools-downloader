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
import select
import shutil
import signal
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
STREAM_START_TIMEOUT = max(10, int(os.environ.get('STREAM_START_TIMEOUT', '55')))
STREAM_FRAGMENTS = max(1, int(os.environ.get('STREAM_FRAGMENTS', '4')))
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', '').strip().rstrip('/')
TRUST_PROXY = os.environ.get('TRUST_PROXY', '0').lower() in ('1', 'true', 'yes')
YTDLP_PROXY = os.environ.get('YTDLP_PROXY', '').strip()
YT_RE = re.compile(r'^https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/', re.I)
QUAL = ('max', '2160', '1440', '1080', '720', '480', '360', '240')
AUDIO = ('mp3', 'm4a', 'opus', 'wav')
YT_CLIENTS = ('android', 'ios', 'tv', 'mweb', 'web', 'web_safari',
              'web_embedded', 'tv_embedded', 'android_vr')
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


def build_cmd(binary, req, outdir, proxy=None, client=None):
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
           '--fragment-retries', '3',
           '--concurrent-fragments', str(STREAM_FRAGMENTS), '-o', out]
    if proxy:
        cmd += ['--proxy', proxy]
    if client in YT_CLIENTS:
        cmd += ['--extractor-args', 'youtube:player_client=' + client]
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


def stream_quality(req):
    quality = str(req.get('videoQuality') or '720').strip()
    if quality not in QUAL:
        quality = '720'
    if quality != 'max':
        try:
            quality = str(min(int(quality), MAX_HEIGHT))
        except ValueError:
            quality = '720'
    return quality


def stream_format(req, kind):
    """Return a yt-dlp format selector for one side of a live stream.

    Video and audio are fetched by separate yt-dlp processes so ffmpeg can
    start muxing as soon as both begin producing data.  The audio side is
    forced to m4a where possible so the MP4 muxer can copy AAC without a
    second encode.
    """
    if kind == 'audio':
        return 'ba[ext=m4a]/ba/b'
    quality = stream_quality(req)
    cap = '' if quality == 'max' else '[height<=' + quality + ']'
    return 'bv*' + cap + '[vcodec^=avc1]/bv*' + cap + '/b' + cap + '/b'


def build_stream_cmd(binary, req, kind, proxy=None, client=None):
    cmd = [binary, '--no-playlist', '--no-progress',
           '--socket-timeout', '30', '--retries', '3',
           '--fragment-retries', '3',
           '--concurrent-fragments', str(STREAM_FRAGMENTS),
           '-f', stream_format(req, kind), '-o', '-']
    if proxy:
        cmd += ['--proxy', proxy]
    if client in YT_CLIENTS:
        cmd += ['--extractor-args', 'youtube:player_client=' + client]
    if MAX_FILESIZE:
        cmd += ['--max-filesize', MAX_FILESIZE]
    if MAX_DURATION > 0:
        cmd += ['--match-filter', 'duration <= %d' % MAX_DURATION]
    cmd.append(str(req.get('url') or ''))
    return cmd


def build_stream_mux(req, video_fd=None, audio_fd=None):
    if video_fd is None:
        return ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-i', 'pipe:%d' % audio_fd, '-vn',
                '-c:a', 'libmp3lame', '-b:a', '192k',
                '-f', 'mp3', 'pipe:1']
    return ['ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-i', 'pipe:%d' % video_fd, '-i', 'pipe:%d' % audio_fd,
            '-map', '0:v:0', '-map', '1:a:0', '-c', 'copy',
            '-bsf:a', 'aac_adtstoasc',
            '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
            '-f', 'mp4', 'pipe:1']


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
        self.send_header('Access-Control-Allow-Methods', 'GET, HEAD, POST, OPTIONS')
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
                'stream': True,
            })
        if path == '/':
            binary = find_ytdlp()
            return self._json(200, {
                'status': 'ok',
                'name': 'MaxTools hosted downloader',
                'account': False,
                'proxy': bool(YTDLP_PROXY),
                'stream': True,
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

    def _read_req(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self.close_connection = True
            self._json(400, error('error.api.link.invalid'))
            return None
        if length > MAX_BODY:
            self.close_connection = True
            self._json(413, error('error.api.body.too_large'))
            return None
        try:
            req = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
        except Exception:
            self._json(400, error('error.api.link.invalid'))
            return None
        if not isinstance(req, dict):
            self._json(400, error('error.api.link.invalid'))
            return None
        return req

    def _stream_stderr(self, stream):
        parts = []
        for fh in stream.get('errs', []):
            try:
                fh.seek(0)
                text = fh.read().decode('utf-8', 'replace')
                if text.strip():
                    parts.append(text.strip()[-1200:])
            except Exception:
                pass
        return safe_detail(' | '.join(parts)[-1200:])

    def _kill_stream(self, stream):
        for proc in list(stream.get('procs', [])) + [stream.get('ff')]:
            if not proc or proc.poll() is not None:
                continue
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _start_stream(self, binary, req, proxy, client):
        """Start yt-dlp and ffmpeg, returning pipes ready for the first byte."""
        mode = str(req.get('downloadMode') or '')
        procs = []
        errs = []
        ff = None

        def errfile():
            fh = tempfile.TemporaryFile()
            errs.append(fh)
            return fh

        try:
            if mode == 'audio':
                a = subprocess.Popen(
                    build_stream_cmd(binary, req, 'audio', proxy, client),
                    stdout=subprocess.PIPE, stderr=errfile(),
                    start_new_session=True)
                procs.append(a)
                afd = a.stdout.fileno()
                ff = subprocess.Popen(
                    build_stream_mux(req, audio_fd=afd),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=errfile(), pass_fds=(afd,),
                    start_new_session=True)
                a.stdout.close()
            else:
                v = subprocess.Popen(
                    build_stream_cmd(binary, req, 'video', proxy, client),
                    stdout=subprocess.PIPE, stderr=errfile(),
                    start_new_session=True)
                a = subprocess.Popen(
                    build_stream_cmd(binary, req, 'audio', proxy, client),
                    stdout=subprocess.PIPE, stderr=errfile(),
                    start_new_session=True)
                procs.extend([v, a])
                vfd = v.stdout.fileno()
                afd = a.stdout.fileno()
                ff = subprocess.Popen(
                    build_stream_mux(req, video_fd=vfd, audio_fd=afd),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=errfile(), pass_fds=(vfd, afd),
                    start_new_session=True)
                v.stdout.close()
                a.stdout.close()
            return {'procs': procs, 'ff': ff, 'errs': errs}
        except Exception:
            for proc in procs:
                try:
                    if proc.stdout:
                        proc.stdout.close()
                except Exception:
                    pass
                try:
                    proc.kill()
                except Exception:
                    pass
            if ff:
                try:
                    ff.kill()
                except Exception:
                    pass
            for fh in errs:
                try:
                    fh.close()
                except Exception:
                    pass
            raise

    def _stop_stream(self, stream):
        self._kill_stream(stream)
        for fh in stream.get('errs', []):
            try:
                fh.close()
            except Exception:
                pass

    def _write_chunk(self, data):
        self.wfile.write(('%X\r\n' % len(data)).encode('ascii'))
        self.wfile.write(data)
        self.wfile.write(b'\r\n')

    def _stream(self, req):
        client = self.headers.get('X-MaxTools-Client', '').strip()
        if client not in YT_CLIENTS:
            client = None
        ip = client_ip(self)
        if not rate_ok(ip):
            return self._json(429, error('error.api.rate_exceeded'))
        if not SEM.acquire(blocking=False):
            return self._json(429, error('error.api.rate_exceeded'))
        st = None
        timer = None
        started = time.time()
        try:
            binary = find_ytdlp()
            if not binary:
                return self._json(200, error('error.api.ytdlp.missing'))
            if str(req.get('downloadMode') or '') == 'audio':
                fmt = str(req.get('audioFormat') or 'mp3').lower()
                if fmt != 'mp3':
                    return self._json(200, error(
                        'error.api.stream.unsupported',
                        'Live streaming is only available for MP3 audio'))
            # Hosted deployments need WARP: YouTube treats Render's datacenter
            # IPs as bots. Try the proxy first, then fall back to direct for
            # self-hosted deployments where the host IP is already clean.
            proxies = [YTDLP_PROXY] if YTDLP_PROXY else []
            proxies.append(None)
            first = b''
            detail = ''
            used_proxy = None
            for idx, proxy in enumerate(proxies):
                try:
                    st = self._start_stream(binary, req, proxy, client)
                except Exception as exc:
                    detail = safe_detail(exc)
                    if idx == len(proxies) - 1:
                        return self._json(200, error('error.api.fetch.fail', detail))
                    continue
                timer = threading.Timer(TIMEOUT, self._kill_stream, args=(st,))
                timer.daemon = True
                timer.start()
                fd = st['ff'].stdout.fileno()
                deadline = time.time() + STREAM_START_TIMEOUT
                while time.time() < deadline:
                    ready, _, _ = select.select([fd], [], [], 0.5)
                    if ready:
                        try:
                            first = os.read(fd, 65536)
                        except OSError:
                            first = b''
                        break
                    if st['ff'].poll() is not None:
                        try:
                            first = os.read(fd, 65536)
                        except OSError:
                            first = b''
                        break
                if first:
                    used_proxy = proxy
                    break
                detail = self._stream_stderr(st) or 'Live stream produced no data'
                self._stop_stream(st)
                st = None
                if timer:
                    timer.cancel()
                    timer = None
                if not YTDLP_PROXY:
                    break
            if not first or not st:
                code = classify_failure(detail)
                return self._json(200, error(code, detail))

            mode = str(req.get('downloadMode') or '')
            ext = 'mp3' if mode == 'audio' else 'mp4'
            ctype = 'audio/mpeg' if mode == 'audio' else 'video/mp4'
            vid = ''
            m = re.search(r'(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{11})',
                          str(req.get('url') or ''))
            if m:
                vid = '-' + m.group(1)
            name = 'maxrtools' + vid + '.' + ext
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Disposition',
                             'attachment; filename="%s"' % name)
            self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('Access-Control-Expose-Headers',
                             'Content-Disposition, Content-Type')
            self.end_headers()
            self.close_connection = True
            self._write_chunk(first)
            total = len(first)
            last_data = time.time()
            completed = True
            fd = st['ff'].stdout.fileno()
            while True:
                if st['ff'].poll() is not None:
                    while True:
                        try:
                            data = os.read(fd, 65536)
                        except OSError:
                            data = b''
                        if not data:
                            break
                        self._write_chunk(data)
                        total += len(data)
                    completed = st['ff'].returncode == 0
                    break
                if time.time() - last_data > 180:
                    completed = False
                    break
                ready, _, _ = select.select([fd], [], [], 1.0)
                if not ready:
                    continue
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b''
                if not data:
                    try:
                        completed = st['ff'].wait(timeout=5) == 0
                    except Exception:
                        completed = False
                    break
                self._write_chunk(data)
                total += len(data)
                last_data = time.time()
            if completed:
                for proc in st['procs']:
                    if proc.poll() is None:
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            completed = False
                    if proc.returncode not in (0, None):
                        completed = False
            if completed:
                self.wfile.write(b'0\r\n\r\n')
            else:
                print('[hosted] stream incomplete mode=%s bytes=%d' % (
                    mode or 'video', total), flush=True)
            elapsed = time.time() - started
            print('[hosted] stream %.1fs mode=%s bytes=%d proxy=%s client=%s' % (
                elapsed, mode or 'video', total,
                'yes' if used_proxy else 'no', client or 'default'), flush=True)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            print('[hosted] stream exception: ' + traceback.format_exc()[-4000:],
                  flush=True)
        finally:
            if timer:
                timer.cancel()
            if st:
                self._stop_stream(st)
            SEM.release()

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ('/', '/stream'):
            return self._json(404, error('error.api.link.invalid'))
        if not self._origin_allowed():
            self.close_connection = True
            return self._json(403, error('error.api.origin.denied'))
        req = self._read_req()
        if req is None:
            return
        url = str(req.get('url') or '').strip()
        if not YT_RE.match(url):
            return self._json(200, error('error.api.link.invalid'))
        if req.get('cookiesTxt') or req.get('cookiesFromBrowser'):
            # Deliberately unsupported: this is a public anonymous service.
            return self._json(200, error('error.api.cookies.unsupported',
                                         'Account cookies are not accepted by the hosted downloader'))

        client = self.headers.get('X-MaxTools-Client', '').strip()
        if client not in YT_CLIENTS:
            client = None
        if path == '/stream':
            return self._stream(req)
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
                def attempt(proxy, yt_client=None):
                    cmd = build_cmd(binary, req, outdir, proxy, yt_client)
                    started = time.time()
                    try:
                        proc = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT)
                    except FileNotFoundError:
                        return None, None, 'error.api.ytdlp.missing', None
                    except subprocess.TimeoutExpired:
                        return None, None, 'error.api.timeout', None
                    files = [os.path.join(outdir, f) for f in os.listdir(outdir)
                             if not f.endswith('.part')]
                    files = [f for f in files if os.path.isfile(f) and os.path.getsize(f) > 0]
                    elapsed = time.time() - started
                    if proc.returncode == 0 and files:
                        path = max(files, key=os.path.getsize)
                        print('[hosted] ok %.1fs mode=%s bytes=%d proxy=%s client=%s' % (
                            elapsed, req.get('downloadMode') or 'video',
                            os.path.getsize(path), 'yes' if proxy else 'no',
                            yt_client or 'default'), flush=True)
                        return path, None, None, None
                    tail = ' '.join((proc.stderr or b'').decode('utf-8', 'replace')
                                    .strip().splitlines()[-2:])
                    tail = safe_detail(tail)
                    fail_code = classify_failure(proc.stderr or b'')
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
                    print('[hosted] fail %.1fs proxy=%s client=%s code=%s' % (
                        elapsed, 'yes' if proxy else 'no', yt_client or 'default',
                        fail_code), flush=True)
                    return None, tail, None, fail_code

                path, direct_detail, fatal, direct_code = attempt(None, client)
                if fatal:
                    return self._json(200, error(fatal))
                proxy_detail = None
                proxy_code = None
                if not path and YTDLP_PROXY:
                    path, proxy_detail, fatal, proxy_code = attempt(YTDLP_PROXY, client)
                    if fatal:
                        return self._json(200, error(fatal))
                if not path:
                    detail = direct_detail or ''
                    if proxy_detail:
                        detail = ('direct: ' + detail + ' | proxy: ' + proxy_detail)[-300:]
                    code = direct_code or proxy_code or classify_failure(detail)
                    return self._json(200, error(code, detail))
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
