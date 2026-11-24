"""Minimal HTTP framework on top of the standard library (no dependencies)."""
import gzip
import hashlib
import http.server
import json
import logging
import mimetypes
import os
import re
import socketserver
import threading
import time
import traceback
import urllib.parse

log = logging.getLogger("noxa.web")

MAX_BODY = 1024 * 1024
IDLE_TIMEOUT = 120   # s without receiving or being able to send anything: connection closed (slow clients, "slowloris")


class HTTPError(Exception):
    def __init__(self, status, message="Erreur", data=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.data = data


class Request:
    def __init__(self, handler, method, path, query, headers, client_ip):
        self.handler = handler
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.client_ip = client_ip
        self.user = None
        self.session_hash = None
        self.fed_peer = None       # linked server sending the request (linked-servers port)
        self.fed_listener = False
        self.params = {}
        self._body = None
        self.set_cookies = []

    def arg(self, name, default=None):
        v = self.query.get(name)
        return v[0] if v else default

    def int_arg(self, name, default=0, lo=None, hi=None):
        try:
            v = int(self.arg(name, default))
        except (TypeError, ValueError):
            raise HTTPError(400, f"Paramètre invalide : {name}")
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    @property
    def body(self):
        if self._body is None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise HTTPError(400, "Content-Length invalide")
            if length < 0:
                raise HTTPError(400, "Content-Length invalide")
            if length > MAX_BODY:
                raise HTTPError(413, "Requête trop volumineuse")
            self._body = self.handler.rfile.read(length) if length else b""
        return self._body

    def json(self):
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise HTTPError(400, "JSON invalide")
        if not isinstance(data, dict):
            raise HTTPError(400, "Objet JSON attendu")
        return data

    @property
    def cookies(self):
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out


# requests longer than this are logged (except video streams, deliberate waits, events)
SLOW_REQUEST = 5.0
SLOW_EXEMPT = re.compile(r"^/(api/(stream/|live/|together/|remote/|share/|photos/\d+/stream|items/\d+/prepare|admin/update)|fed/|dlna/)")


class Response:
    def __init__(self, body=b"", status=200, content_type="application/octet-stream",
                 headers=None, stream=None, length=None, on_close=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}
        if headers:
            self.headers.update(headers)
        self.stream = stream
        self.length = length
        self.on_close = on_close
        self.hijacked = False  # the route took over the connection (tunnel of a linked server): nothing to write


def json_response(data, status=200):
    return Response(json.dumps(data, ensure_ascii=False, separators=(",", ":")), status,
                    "application/json; charset=utf-8", {"Cache-Control": "no-store"})


class App:
    def __init__(self):
        self.routes = []
        self.plain_paths = set()  # routes outside /api/ (fixed addresses, e.g. /tv); everything else is the interface
        self.before = []  # callables(req, route_opts)
        self.static_dir = None
        self.teardown = []
        self.begin = None        # callable(req) at the start of each request (language of the messages)
        self.translate = None    # callable(message) -> message in the request's language
        self.default_lang = lambda: "en"  # interface language before signing in (start page)
        self.lang_auto = lambda: True  # no language chosen for the server: the browser's

    def route(self, method, pattern, **opts):
        regex = re.compile("^" + pattern + "$")
        if not pattern.startswith("/api/"):
            self.plain_paths.add(pattern)

        def deco(fn):
            self.routes.append((method, regex, fn, opts))
            return fn
        return deco

    def dispatch(self, req):
        if req.path.startswith(("/api/", "/fed/", "/dlna/", "/apps/", "/cold/")) or req.path in self.plain_paths:
            allowed = False
            for method, regex, fn, opts in self.routes:
                m = regex.match(req.path)
                if not m:
                    continue
                allowed = True
                if method != req.method:
                    continue
                req.params = m.groupdict()
                for hook in self.before:
                    early = hook(req, opts)
                    if isinstance(early, Response):
                        return early  # answer given in advance (item of a linked server: relayed)
                result = fn(req)
                if isinstance(result, Response):
                    return result
                return json_response(result if result is not None else {"ok": True})
            if allowed:
                raise HTTPError(405, "Méthode non autorisée")
            raise HTTPError(404, "Introuvable")
        if req.method not in ("GET", "HEAD"):
            raise HTTPError(405, "Méthode non autorisée")
        return self.serve_static(req.path, req)

    def serve_static(self, path, req=None):
        root = os.path.realpath(self.static_dir)
        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        # /v/<hash>/js/…: the interface code, at an address that changes with each version (see ui_version)
        stamped = None
        m = STAMPED.match(rel)
        if m:
            stamped, rel = m.group(1), m.group(2)
        full = os.path.realpath(os.path.join(root, rel))
        if rel == "sw.js" and os.path.isfile(full):
            return self.service_worker(root, full, req)
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            if "." in os.path.basename(rel):
                raise HTTPError(404, "Fichier introuvable")
            full = os.path.join(root, "index.html")  # SPA fallback
        if full == os.path.join(root, "index.html"):
            return self.index_page(root, full, req, self.default_lang(), self.lang_auto())
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        st = os.stat(full)
        # file fingerprint: after the cache delay, the browser asks "unchanged?" (304, nothing to send again)
        etag = f'W/"{st.st_size:x}-{st.st_mtime_ns:x}"'
        # interface (HTML, JS, CSS): always checked again (light 304 answer when nothing changed), otherwise the
        # browser keeps the old version after a server update. Images: 5 min without asking again.
        cache = "no-cache" if full.endswith((".html", ".js", ".css", ".webmanifest")) else "public, max-age=300"
        headers = {"Cache-Control": cache, "ETag": etag}
        if "/vendor/" in full:
            headers["Cache-Control"] = "public, max-age=604800"
        if stamped and stamped == ui_version(root):
            # address specific to this version: kept as is by the browser (and by a proxy that would
            # cache the .js files); the next version gets other addresses
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        if "/vendor/jassub/" in full:
            # the subtitle engine (libass compiled to WebAssembly) runs in a worker that needs to
            # compile WebAssembly; this policy only applies to its own files
            headers["Content-Security-Policy"] = JASSUB_CSP
        compressible = ctype.startswith(COMPRESSIBLE)
        if compressible:
            headers["Vary"] = "Accept-Encoding"
        if req and req.headers.get("If-None-Match") == etag:
            return Response(b"", 304, ctype, headers)
        data = _gzipped(full, etag) if compressible and req and "gzip" in (req.headers.get("Accept-Encoding") or "") else None
        if data is not None:
            headers["Content-Encoding"] = "gzip"
        else:
            with open(full, "rb") as f:
                data = f.read()
        return Response(data, 200, ctype, headers)


    @staticmethod
    def index_page(root, full, req, lang="en", auto=False):
        """index.html that loads the interface code and style from addresses specific to this version.
        Without it, a proxy (Nginx Proxy Manager "Cache Assets": expires 30d) or an over-eager cache keeps
        the old code after an update, until a Ctrl+F5."""
        version = ui_version(root)
        st = os.stat(full)
        etag = f'W/"ix-{version}-{lang}{"-auto" if auto else ""}-{st.st_mtime_ns:x}"'
        headers = {"Cache-Control": "no-cache", "ETag": etag, "Vary": "Accept-Encoding"}
        ctype = "text/html; charset=utf-8"
        if req and req.headers.get("If-None-Match") == etag:
            return Response(b"", 304, ctype, headers)
        with open(full, encoding="utf-8") as f:
            html = f.read()
        html = html.replace('href="/css/', f'href="/v/{version}/css/').replace('src="/js/', f'src="/v/{version}/js/')
        # start-up requests sent right away, and all the code loaded in parallel (not one import after the other)
        modules = ",".join(module_graph(root, version))
        html = html.replace('  <link rel="stylesheet"', f'  <meta name="noxa-modules" data-base="/v/{version}/js/" content="{modules}">\n'
                            f'  <script>{BOOT_JS}</script>\n  <link rel="stylesheet"', 1)
        # server's default language: the interface's, as long as nobody is signed in on this device
        auto_attr = ' data-auto-lang="1"' if auto else ""  # no server language yet: the browser's one
        html = html.replace('<html lang="fr"', f'<html lang="{lang}" data-default-lang="{lang}"{auto_attr}', 1)
        if lang == "en":
            html = html.replace("Noxa : tes films, séries et animes, partout.", "Noxa: your movies, shows and anime, everywhere.")
            html = html.replace("Noxa a besoin de JavaScript.", "Noxa needs JavaScript.")
        data = html.encode()
        if req and "gzip" in (req.headers.get("Accept-Encoding") or ""):
            data = gzip.compress(data, 6)
            headers["Content-Encoding"] = "gzip"
        return Response(data, 200, ctype, headers)

    @staticmethod
    def service_worker(root, full, req):
        """sw.js with the list of the interface files and a version that changes as soon as one of them changes
        (the browser then installs the new service worker and reloads its cache)."""
        shell = ["/", "/manifest.webmanifest", "/css/app.css", "/favicon.png", "/img/noxa-logo.webp"]
        for sub, ext in (("js", ".js"), ("fonts", ".woff"), ("icons", ".png")):
            for dirpath, _, files in os.walk(os.path.join(root, sub)):
                shell += ["/" + os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/")
                          for f in sorted(files) if f.endswith(ext)]
        shell = [u for u in shell if os.path.isfile(os.path.join(root, "index.html" if u == "/" else u.lstrip("/")))]
        stamp = []
        for u in [*shell, "/sw.js"]:
            st = os.stat(os.path.join(root, "index.html" if u == "/" else u.lstrip("/")))
            stamp.append(f"{u}:{st.st_size}:{st.st_mtime_ns}")
        version = hashlib.sha1("|".join(stamp).encode()).hexdigest()[:12]
        ui = ui_version(root)  # code and style are loaded from /v/<hash>/ (see index_page)
        shell = [f"/v/{ui}{u}" if u.startswith(("/js/", "/css/")) else u for u in shell]
        etag = f'"sw-{version}"'
        headers = {"Cache-Control": "no-cache", "ETag": etag}
        if req and req.headers.get("If-None-Match") == etag:
            return Response(b"", 304, "application/javascript; charset=utf-8", headers)
        with open(full, encoding="utf-8") as f:
            code = f.read()
        code = re.sub(r"^const VERSION = .*$", f"const VERSION = '{version}';", code, count=1, flags=re.M)
        code = re.sub(r"^const SHELL = .*$", "const SHELL = " + json.dumps(shell) + ";", code, count=1, flags=re.M)
        return Response(code, 200, "application/javascript; charset=utf-8", headers)


STAMPED = re.compile(r"^v/([0-9a-f]{12})/((?:js|css)/.+)$")
_ui = {"at": 0.0, "version": None}


def ui_version(root):
    """Fingerprint of the interface code and style (size and date of each file), checked every 2 s."""
    now = time.monotonic()
    if _ui["version"] and now - _ui["at"] < 2:
        return _ui["version"]
    stamp = []
    for sub in ("js", "css"):
        for dirpath, _, files in os.walk(os.path.join(root, sub)):
            for f in sorted(files):
                st = os.stat(os.path.join(dirpath, f))
                stamp.append(f"{dirpath}/{f}:{st.st_size}:{st.st_mtime_ns}")
    _ui.update(at=now, version=hashlib.sha1("|".join(sorted(stamp)).encode()).hexdigest()[:12])
    return _ui["version"]


_graph = {}
_IMPORT = re.compile(r"""^\s*(?:import|export)\s[^;'"]*?from\s*['"](\./[^'"]+|\.\./[^'"]+)['"]|^\s*import\s*['"](\.{1,2}/[^'"]+)['"]""", re.M)


def module_graph(root, version):
    """Modules imported (directly or not) by app.js, without the pages loaded on demand (import())."""
    if version in _graph:
        return _graph[version]
    base = os.path.join(root, "js")
    seen, todo = [], ["app.js"]
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.append(m)
        try:
            with open(os.path.join(base, m), encoding="utf-8") as f:
                code = f.read()
        except OSError:
            continue
        for a, b in _IMPORT.findall(code):
            dep = os.path.normpath(os.path.join(os.path.dirname(m), a or b)).replace(os.sep, "/")
            if not dep.startswith("..") and dep not in seen:
                todo.append(dep)
    out = sorted(seen[1:])
    _graph.clear()
    _graph[version] = out
    return out


COMPRESSIBLE = ("text/", "application/javascript", "application/json", "application/wasm", "image/svg+xml",
                "application/manifest+json")
_gz_lock = threading.Lock()
_gz_cache = {}  # (path, fingerprint) -> compressed file: hls.js is not compressed again on every visit


def _gzipped(full, etag):
    key = (full, etag)
    with _gz_lock:
        data = _gz_cache.get(key)
    if data is None:
        with open(full, "rb") as f:
            raw = f.read()
        if len(raw) < 1400:
            return None
        data = gzip.compress(raw, 6)
        with _gz_lock:
            if sum(len(v) for v in _gz_cache.values()) > 32 * 1024 * 1024:
                _gz_cache.clear()  # shortcut: emptied all at once; an LRU if the interface grows a lot
            _gz_cache[key] = data
    return data


# Interface start-up: the first requests leave as soon as the page arrives, while the code loads
# (otherwise /api/status, then /api/me, then /api/home each wait for the previous one, and all the code before:
# as many round trips, long when the server is far). api.js reuses these answers instead of asking again.
BOOT_JS = (
    # app bridges: NoxaAndroid / noxaNative since 4.5, NexisAndroid / nexisNative in older apps
    "window.NoxaAndroid=window.NoxaAndroid||window.NexisAndroid;window.noxaNative=window.noxaNative||window.nexisNative;"
    "(function(){var l=null;try{l=localStorage.getItem('nexis.lang')}catch(e){}"
    "var d=document.documentElement;if(l!=='fr'&&l!=='en')l=d.hasAttribute('data-auto-lang')?"
    "((navigator.languages||[navigator.language||'']).join(',').toLowerCase().indexOf('fr')===0?'fr':'en')"
    ":(d.getAttribute('data-default-lang')||'en');"
    "var o={headers:{'X-Noxa':'1','X-Lang':l},credentials:'same-origin'},b={};"
    "function g(p){b[p]=fetch(p,o).then(function(r){return r.text().then(function(t){return{status:r.status,ok:r.ok,text:t}})})"
    "['catch'](function(){return null})}"
    "g('/api/status');g('/api/me');var k=location.hash;if(!k||k==='#'||k==='#/')g('/api/home');"
    "window.__nexisBoot={at:Date.now(),req:b};"
    # all the code requested in parallel, on the first load only (afterwards it comes from the service worker)
    "var m=document.querySelector('meta[name=noxa-modules]');"
    "if(m&&!(navigator.serviceWorker&&navigator.serviceWorker.controller))m.content.split(',').forEach(function(f){"
    "var x=document.createElement('link');x.rel='modulepreload';x.href=m.getAttribute('data-base')+f;document.head.appendChild(x)})"
    "})();"
)
BOOT_HASH = "'sha256-" + __import__("base64").b64encode(hashlib.sha256(BOOT_JS.encode()).digest()).decode() + "'"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: blob: https://tile.openstreetmap.org; media-src 'self' blob:; "
        f"script-src 'self' {BOOT_HASH} https://www.gstatic.com; "
        "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "worker-src 'self' blob:; frame-src https://www.youtube-nocookie.com; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'; object-src 'none'"
    ),
}


JASSUB_CSP = ("default-src 'self'; script-src 'self' 'unsafe-eval' 'wasm-unsafe-eval'; connect-src 'self'; "
              "img-src 'self' data: blob:; font-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")
mimetypes.add_type("application/manifest+json", ".webmanifest")


def make_handler(app, trust_proxy=False, hsts=False, fed=None):
    """fed: the federation, for the port reserved to linked servers (TLS, each request signed by the server)."""
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Seance"
        sys_version = ""
        timeout = IDLE_TIMEOUT

        def setup(self):
            if fed is not None and hasattr(self.request, "do_handshake"):
                self.request.settimeout(30)
                self.request.do_handshake()  # here rather than when accepting: a slow client does not block the others
            super().setup()

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _client_ip(self):
            ip = self.client_address[0]
            if trust_proxy:
                # the LAST address is the one added by our reverse proxy; the earlier ones come from the
                # client and can be made up (to get around the block after wrong passwords)
                fwd = self.headers.get("X-Forwarded-For")
                if fwd:
                    ip = fwd.split(",")[-1].strip() or ip
            return ip

        def _handle(self, method):
            parsed = urllib.parse.urlsplit(self.path)
            req = Request(self, method if method != "HEAD" else "GET", parsed.path,
                          urllib.parse.parse_qs(parsed.query), self.headers, self._client_ip())
            started = time.monotonic()
            try:
                if app.begin:
                    app.begin(req)
                if fed is not None:
                    # linked-servers port: the API only, and only for a paired server
                    # (except the join request, which presents the invitation token)
                    req.fed_listener = True
                    if not parsed.path.startswith(("/api/", "/fed/")):
                        raise HTTPError(404, "Introuvable")
                    req.fed_peer = fed.authenticate(self.headers)
                    if not req.fed_peer and parsed.path not in ("/fed/join", "/fed/join/preview"):
                        raise HTTPError(401, "Serveur non reconnu")
                resp = app.dispatch(req)
            except HTTPError as e:
                payload = {"error": app.translate(e.message) if app.translate else e.message}
                if e.data:
                    payload.update(e.data)
                resp = json_response(payload, e.status)
            except Exception:
                log.error("Erreur sur %s %s\n%s", method, parsed.path, traceback.format_exc())
                msg = "Erreur interne du serveur"
                resp = json_response({"error": app.translate(msg) if app.translate else msg}, 500)
            finally:
                spent = time.monotonic() - started
                if spent > SLOW_REQUEST and not SLOW_EXEMPT.match(parsed.path):
                    log.warning("Requête lente : %s %s (%.1f s)", method, parsed.path, spent)
                for fn in app.teardown:
                    try:
                        fn()
                    except Exception:
                        pass
            try:
                self._write(resp, req, head=(method == "HEAD"))
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                self.close_connection = True
            except Exception as e:  # upstream failed mid-stream: drop the connection
                log.debug("Flux interrompu sur %s : %s", parsed.path, e)
                self.close_connection = True
            finally:
                if resp.on_close:
                    try:
                        resp.on_close()
                    except Exception:
                        pass

        def _write(self, resp, req, head=False):
            if resp.hijacked:
                self.close_connection = True
                return
            # Drain an unread request body so keep-alive stays in sync.
            if req._body is None and req.method in ("POST", "PUT", "DELETE"):
                try:
                    req.body
                except HTTPError:
                    self.close_connection = True
            self.send_response(resp.status)
            for k, v in SECURITY_HEADERS.items():
                if k not in resp.headers:
                    self.send_header(k, v)
            if hsts:  # behind an HTTPS reverse proxy (NOXA_COOKIE_SECURE=1)
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k, v in resp.headers.items():
                self.send_header(k, v)
            for c in req.set_cookies:
                self.send_header("Set-Cookie", c)
            if resp.stream is None:
                self.send_header("Content-Length", str(len(resp.body)))
                self.end_headers()
                if not head:
                    self.wfile.write(resp.body)
                return
            if resp.length is not None:
                self.send_header("Content-Length", str(resp.length))
                self.end_headers()
                if not head:
                    for chunk in resp.stream:
                        self.wfile.write(chunk)
                return
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if head:
                return
            for chunk in resp.stream:
                if chunk:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.write(b"0\r\n\r\n")

        def do_GET(self):
            self._handle("GET")

        def do_HEAD(self):
            self._handle("HEAD")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def do_DELETE(self):
            self._handle("DELETE")

        def do_SUBSCRIBE(self):  # UPnP events (DLNA server)
            self._handle("SUBSCRIBE")

        def do_UNSUBSCRIBE(self):
            self._handle("UNSUBSCRIBE")

    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64
    max_connections = 256   # beyond this, new connections are refused instead of exhausting the memory

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(self.max_connections)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return  # the browser closed the connection (seek, page change): normal
        super().handle_error(request, client_address)


class TLSServer(Server):
    """Linked-servers port: TLS with this server's certificate (pinned by the paired servers)."""
    max_connections = 128

    def __init__(self, address, handler, ssl_context):
        self.ssl_context = ssl_context
        super().__init__(address, handler)

    def get_request(self):
        sock, addr = self.socket.accept()
        return self.ssl_context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def handle_error(self, request, client_address):
        import ssl
        import sys
        if isinstance(sys.exc_info()[1], (ssl.SSLError, OSError)):
            log.debug("Connexion d'un serveur lié refusée (%s) : %s", client_address[0], sys.exc_info()[1])
            return
        super().handle_error(request, client_address)
