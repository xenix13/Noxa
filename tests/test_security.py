"""Regression tests for the fixes from the security audit (docs/AUDIT-SECURITE.md).

python3 -m unittest tests.test_security
"""
import hashlib
import http.client
import http.server
import os
import shutil
import socket
import socketserver
import tempfile
import threading
import time
import unittest

from tests.test_api import STATIC, Client  # noqa: F401
from tests.fixtures import ff

from noxa import media, mountd
from noxa.api import Ctx, build_app
from noxa.livetv import LiveError, stream_url_ok
from noxa.web import Server, make_handler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Static(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class SecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp()
        cls.ctx = Ctx(os.path.join(cls.work, "data"), STATIC)
        cls.server = Server(("127.0.0.1", 0), make_handler(build_app(cls.ctx), trust_proxy=True))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.admin = Client(cls.base)
        st, r = cls.admin.call("POST", "/api/setup", {"username": "alex", "password": "motdepasse1"},
                               headers={"X-Setup-Code": cls.ctx.setup_code})
        assert st == 200, r
        cls.lib_dir = os.path.join(cls.work, "series")
        os.makedirs(os.path.join(cls.lib_dir, "Une Série"))
        st, cls.lib = cls.admin.call("POST", "/api/admin/libraries", {"name": "Séries", "kind": "show", "paths": [cls.lib_dir]})
        assert st == 200, cls.lib
        # small local web server: IPTV list "from the provider"
        cls.www = os.path.join(cls.work, "www")
        os.makedirs(cls.www)
        handler = lambda *a, **k: Static(*a, directory=cls.www, **k)  # noqa: E731
        cls.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.www_url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.httpd.shutdown()
        shutil.rmtree(cls.work, ignore_errors=True)

    def raw(self, request_bytes, timeout=5):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        s.sendall(request_bytes)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        finally:
            s.close()
        return data

    # ------------------------------------------------------------ couche HTTP
    def test_http_hardening(self):
        st, body, h = self.admin.call("GET", "/api/status", raw=True)
        for k in ("X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy", "Referrer-Policy",
                  "Cross-Origin-Opener-Policy", "Permissions-Policy"):
            self.assertIn(k, h)
        self.assertIn("object-src 'none'", h["Content-Security-Policy"])
        # negative Content-Length: refused instead of reading the connection to the end
        cookie = "; ".join(f"{c.name}={c.value}" for c in self.admin.jar)
        resp = self.raw(b"PUT /api/me/prefs HTTP/1.1\r\nHost: x\r\nX-Seance: 1\r\nContent-Length: -5\r\nCookie: "
                        + cookie.encode() + b"\r\n\r\n")
        self.assertIn(b" 400 ", resp.split(b"\r\n")[0])
        # JSON nested too deep: 400 and not an internal error
        req = b"[" * 100000 + b"]" * 100000
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("PUT", "/api/me/prefs", body=req, headers={"X-Seance": "1", "Cookie": cookie,
                                                               "Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()
        # non-GET request without the anti-CSRF header
        self.assertEqual(self.admin.call("PUT", "/api/me/prefs", {"theme": "oled"}, csrf=False)[0], 403)
        # apps older than 4.5 send the same header under the former internal name
        self.assertEqual(self.admin.call("PUT", "/api/me/prefs", {"theme": "oled"}, csrf=False,
                                         headers={"X-Seance": "1"})[0], 200)

    def test_idle_connection_closed(self):
        from noxa import web
        self.assertLessEqual(web.IDLE_TIMEOUT, 300)
        handler = make_handler(build_app(self.ctx))
        self.assertEqual(handler.timeout, web.IDLE_TIMEOUT)

    def test_forwarded_for_uses_last_hop(self):
        # behind the proxy, the address made up by the client (first) is ignored: the block applies
        c = Client(self.base)
        for _ in range(8):
            c.call("POST", "/api/login", {"username": "inconnu", "password": "faux"},
                   headers={"X-Forwarded-For": f"10.9.9.{_}, 203.0.113.7"})
        st, r = c.call("POST", "/api/login", {"username": "autre", "password": "faux"},
                       headers={"X-Forwarded-For": "10.1.1.1, 203.0.113.7"})
        self.assertEqual(st, 429, r)
        self.ctx.throttle.fails.clear()

    def test_setup_code_throttled(self):
        c = Client(self.base)
        codes = [c.call("POST", "/api/setup", {"username": "x", "password": "motdepasse1"},
                        headers={"X-Setup-Code": f"BAD{i:05d}", "X-Forwarded-For": "198.51.100.4"})[0] for i in range(10)]
        self.assertEqual(codes[:8], [403] * 8)
        self.assertEqual(codes[-1], 429)
        self.ctx.throttle.fails.clear()

    # ------------------------------------------------------------ parental control
    def test_restricted_profile_cannot_open_unrestricted_profile(self):
        st, kid = self.admin.call("POST", "/api/admin/users", {"username": "petit", "managed": True, "max_age": 10})
        self.assertEqual(st, 200, kid)
        st, open_ = self.admin.call("POST", "/api/admin/users", {"username": "invite", "managed": True})
        st, teen = self.admin.call("POST", "/api/admin/users", {"username": "ado", "managed": True, "max_age": 7})
        c = Client(self.base)
        c.call("POST", "/api/login", {"username": "alex", "password": "motdepasse1"})
        self.assertEqual(c.call("POST", f"/api/profiles/{kid['id']}/switch", {})[0], 200)
        st, r = c.call("POST", f"/api/profiles/{open_['id']}/switch", {})
        self.assertEqual(st, 403, r)  # profile without PIN and without age limit
        self.assertEqual(c.call("POST", f"/api/profiles/{teen['id']}/switch", {})[0], 200)  # plus restreint : permis
        for u in (kid, open_, teen):
            self.admin.call("DELETE", f"/api/admin/users/{u['id']}")

    # ------------------------------------------------------------ webhook
    def test_webhook_cannot_escape_libraries(self):
        st, s = self.admin.call("GET", "/api/admin/settings")
        c = Client(self.base)
        self.ctx.watcher.force_at, self.ctx.watcher.force_scope = None, None
        st, r = c.call("POST", s["webhook_path"], {"eventType": "Download", "series": {"path": "/tv/../../../../etc"}},
                       csrf=False)
        self.assertEqual(st, 200)
        scope = self.ctx.watcher.force_scope or {}
        for dirs in scope.values():
            for d in dirs:
                self.assertTrue(os.path.realpath(d).startswith(os.path.realpath(self.lib_dir)), d)
        st, r = c.call("POST", s["webhook_path"], {"eventType": "Download", "series": {"path": "/tv/Une Série"}}, csrf=False)
        self.assertTrue(r["partial"])
        self.ctx.watcher.force_at, self.ctx.watcher.force_scope = None, None

    # ------------------------------------------------------------ "Watch together"
    def test_events_require_membership_and_are_capped(self):
        lea = Client(self.base)
        self.admin.call("POST", "/api/admin/users", {"username": "lea", "password": "motdepasse2"})
        lea.call("POST", "/api/login", {"username": "lea", "password": "motdepasse2"})
        st, g = self.admin.call("POST", "/api/together", {"name": "Test"})
        self.assertEqual(st, 200, g)
        # a plain GET (which another site can trigger) does not put anyone in the group
        self.assertEqual(lea.call("GET", f"/api/together/{g['id']}/events")[0], 403)
        self.assertFalse(lea.call("GET", f"/api/together/{g['id']}")[1]["member"])
        lea.call("POST", f"/api/together/{g['id']}/join")
        cookie = "; ".join(f"{c.name}={c.value}" for c in lea.jar)
        conns = []
        try:
            for _ in range(6):
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
                conn.request("GET", f"/api/together/{g['id']}/events", headers={"Cookie": cookie})
                self.assertEqual(conn.getresponse().status, 200)
                conns.append(conn)
            time.sleep(0.2)
            self.assertEqual(lea.call("GET", f"/api/together/{g['id']}/events")[0], 429)
        finally:
            for conn in conns:
                conn.close()
        self.ctx.together.groups.clear()

    # ------------------------------------------------------------ live TV
    def test_iptv_list_cannot_read_server_files(self):
        self.assertTrue(stream_url_ok("http://exemple.fr/live.ts"))
        self.assertTrue(stream_url_ok("udp://@239.0.0.1:1234"))
        for bad in ("/etc/passwd", "file:///etc/passwd", "concat:/etc/passwd|/etc/hosts", "subfile:,,start,0,end,0,,:/etc/x",
                    "data:text/plain;base64,QUJD", "http://exemple.fr/a b", "pipe:0"):
            self.assertFalse(stream_url_ok(bad), bad)
        self.assertTrue(stream_url_ok("/srv/tv/chaine.ts", allow_local=True))
        self.assertFalse(stream_url_ok("/srv/../etc/passwd", allow_local=True))
        with open(os.path.join(self.www, "liste.m3u"), "w", encoding="utf-8") as f:
            f.write('#EXTM3U url-tvg="/etc/hostname"\n'
                    '#EXTINF:-1 tvg-logo="/etc/hostname",Fichier local\n/etc/passwd\n'
                    '#EXTINF:-1,Concat\nconcat:/etc/passwd|/etc/hosts\n'
                    '#EXTINF:-1 tvg-logo="file:///etc/hostname",Bonne chaîne\nhttp://127.0.0.1:9/live.ts\n')
        st, r = self.admin.call("POST", "/api/admin/live/sources", {"name": "Fournisseur", "m3u_url": self.www_url + "/liste.m3u"})
        self.assertEqual(st, 200, r)
        src = r["sources"][0]
        self.assertEqual(src["channels"], 1)  # only the network address is kept
        self.assertEqual(src["programmes"], 0)  # guide "/etc/hostname" refused
        self.assertIn("refusée", src["error"] or "")
        ch = self.ctx.db.one("SELECT * FROM live_channels WHERE source_id=?", (int(src["id"]),))
        with self.assertRaises(LiveError):
            self.ctx.live.logo(ch["logo"])
        self.assertEqual(self.admin.call("GET", f"/api/live/logo/{ch['id']}")[0], 404)
        # a local address slipped into the database (older version) is refused when read
        self.ctx.db.run("UPDATE live_channels SET url='/etc/passwd' WHERE id=?", (ch["id"],))
        st, r = self.admin.call("POST", f"/api/live/{ch['id']}/play")
        self.assertEqual(st, 503, r)
        self.assertIn("refusée", r["error"])
        cmd, _, _ = self.ctx.live._command(dict(ch, url="http://x/y.ts"), self.work, [])
        self.assertEqual(cmd[cmd.index("-protocol_whitelist") + 1], "http,https,tcp,tls,crypto")
        self.admin.call("DELETE", f"/api/admin/live/sources/{src['id']}")

    def test_gzip_bomb_rejected(self):
        import gzip
        from noxa import livetv
        bomb = os.path.join(self.work, "bombe.m3u.gz")
        with gzip.open(bomb, "wb") as f:
            f.write(b"#" * (8 * 1024 * 1024))
        with self.assertRaises(LiveError):
            livetv._open(bomb, limit=1024 * 1024 // 4)

    # ------------------------------------------------------------ library files
    def test_disguised_playlist_is_not_indexed(self):
        fake = os.path.join(self.work, "piege.mkv")
        with open(fake, "w") as f:
            f.write("#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nfile:///etc/passwd\n#EXT-X-ENDLIST\n")
        with self.assertRaises(media.MediaError):
            media.probe(fake)
        ok = os.path.join(self.work, "vrai.mkv")
        ff("-f", "lavfi", "-i", "testsrc2=s=64x36:r=5", "-t", "1", "-c:v", "libx264", "-preset", "ultrafast", ok)
        self.assertIsNotNone(media.probe(ok)["video"])

    # ------------------------------------------------------------ installation and shares
    def test_vendored_hls_checksum(self):
        vendor = os.path.join(ROOT, "static", "vendor")
        with open(os.path.join(vendor, "hls.min.js.sha256")) as f:
            want, name = f.read().split()
        with open(os.path.join(vendor, name), "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), want)
        with open(os.path.join(ROOT, "deploy", "install.sh")) as f:
            script = f.read()
        self.assertNotIn("cdn.jsdelivr", script)
        self.assertIn("sha256sum -c", script)

    def test_vendored_libraries_checksums(self):
        """Audit 2026-10: all the shipped JavaScript libraries are checked at install time."""
        vendor = os.path.join(ROOT, "static", "vendor")
        for sums in ("jassub/jassub.sha256", "libpgs/libpgs.sha256", "qrcode/qrcode.mjs.sha256"):
            folder = os.path.dirname(os.path.join(vendor, sums))
            with open(os.path.join(vendor, sums)) as f:
                for line in f:
                    want, name = line.split()
                    with open(os.path.join(folder, name), "rb") as g:
                        self.assertEqual(hashlib.sha256(g.read()).hexdigest(), want, name)
        for script in ("install.sh", "build-deb.sh"):
            with open(os.path.join(ROOT, "deploy", script)) as f:
                text = f.read()
            self.assertIn("jassub.sha256", text)
            self.assertIn("libpgs.sha256", text)

    def test_writable_library_is_flagged(self):
        """Audit 2026-10: a library folder the service can write to is flagged to the administrator."""
        st, data = self.admin.call("GET", "/api/admin/libraries")
        lib = next(l for l in data["libraries"] if l["id"] == self.lib["id"])
        self.assertEqual(lib["paths_writable"], [True])  # temporary folder of the test: writable
        os.chmod(self.lib_dir, 0o555)
        try:
            st, data = self.admin.call("GET", "/api/admin/libraries")
            lib = next(l for l in data["libraries"] if l["id"] == self.lib["id"])
            self.assertEqual(lib["paths_writable"], [os.geteuid() == 0])  # root writes everywhere
        finally:
            os.chmod(self.lib_dir, 0o755)

    def test_existing_mounts_are_hardened(self):
        fstab = os.path.join(self.work, "fstab")
        with open(fstab, "w") as f:
            f.write("UUID=abc / ext4 defaults 0 1\n"
                    f"{mountd.MARK}films\n//nas/Films /mnt/seance/films cifs credentials=/etc/seance/smb/films.cred,ro,uid=1 0 0\n")
        old = mountd.FSTAB
        mountd.FSTAB = fstab
        try:
            self.assertEqual(mountd.harden_fstab(), 1)
            lines = open(fstab).read().splitlines()
            self.assertEqual(lines[0], "UUID=abc / ext4 defaults 0 1")  # system lines untouched
            opts = lines[2].split()[3].split(",")
            self.assertEqual(opts[:4], ["credentials=/etc/seance/smb/films.cred", "nosuid", "nodev", "noexec"])
            self.assertEqual(mountd.harden_fstab(), 0)
        finally:
            mountd.FSTAB = old

    def test_systemd_unit_hardening(self):
        with open(os.path.join(ROOT, "deploy", "seance.service")) as f:
            unit = f.read()
        for opt in ("NoNewPrivileges=true", "CapabilityBoundingSet=", "RestrictAddressFamilies=", "ProtectSystem=strict"):
            self.assertIn(opt, unit)


if __name__ == "__main__":
    unittest.main()
