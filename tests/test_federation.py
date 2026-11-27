"""Linked servers: encrypted pairing, tunnel, replicated accounts, shared catalogue, merge, relayed playback.

Two complete servers in the same process. B joins A without giving an address: so A can only talk to it
through the tunnel B opens to it.
- A: Films (Brume in French 180p, Le Grand Voyage, Nuit Polaire), Séries (Les Veilleurs S01E01 only)
- B: Films (Brume in English 360p, Ciel Bas), Séries (Les Veilleurs complete), Animes

python3 -m unittest tests.test_federation
"""
import http.client
import json
import re
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest

from tests.test_api import hls_segment, STATIC, Client, media_root, wait_scan

from noxa.api import Ctx, build_app
from noxa.web import Server, make_handler


def start_server(work, name):
    ctx = Ctx(os.path.join(work, name), STATIC)
    ctx.fed.port = 0
    server = Server(("127.0.0.1", 0), make_handler(build_app(ctx)))
    ctx.http_port = server.server_address[1]  # like __main__: local address of the cold copies
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ctx.fed.start(loop=False)
    return ctx, server, f"http://127.0.0.1:{server.server_address[1]}"


def fed_raw(port, method, path, headers=None, body=None):
    """Direct request on the linked-servers port (TLS, without checking the certificate)."""
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection("127.0.0.1", port, context=c, timeout=20)
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data


class FederationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp()
        root = media_root()
        cls.media = m = os.path.join(cls.work, "media")
        brume = os.path.join(root, "films", "Brume (2022)", "Brume.2022.HEVC.mkv")
        shutil.copytree(os.path.join(root, "films"), os.path.join(m, "films_a"))
        os.remove(os.path.join(m, "films_a", "Brume (2022)", "Brume.2022.HEVC.mkv"))
        ff = ["ffmpeg", "-v", "error", "-y", "-i", brume]
        subprocess.run([*ff, "-map", "0", "-c", "copy", "-metadata:s:a:0", "language=fre",
                        os.path.join(m, "films_a", "Brume (2022)", "Brume.2022.HEVC.mkv")], check=True)
        os.makedirs(os.path.join(m, "films_b", "Brume (2022)"))
        subprocess.run([*ff, "-map", "0:v", "-map", "0:a", "-vf", "scale=640:360", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "copy", "-metadata:s:a:0", "language=eng",
                        os.path.join(m, "films_b", "Brume (2022)", "Brume.2022.mkv")], check=True)
        os.makedirs(os.path.join(m, "films_b", "Ciel Bas (2020)"))
        shutil.copy(os.path.join(root, "films", "Nuit.Polaire.2021.1080p.BluRay.x264.mkv"),
                    os.path.join(m, "films_b", "Ciel Bas (2020)", "Ciel.Bas.2020.mkv"))
        shutil.copytree(os.path.join(root, "series"), os.path.join(m, "series_b"))
        ep1 = os.path.join("Les Veilleurs", "Season 01", "Les.Veilleurs.S01E01.720p.WEB.mkv")
        os.makedirs(os.path.dirname(os.path.join(m, "series_a", ep1)))
        shutil.copy(os.path.join(root, "series", ep1), os.path.join(m, "series_a", ep1))
        shutil.copytree(os.path.join(root, "animes"), os.path.join(m, "animes_b"))
        old = time.time() - 3600  # otherwise the scan thinks they are being copied and postpones them
        for d, _, files in os.walk(m):
            for f in files:
                os.utime(os.path.join(d, f), (old, old))
        cls.a, cls.sa, cls.base_a = start_server(cls.work, "a")
        cls.b, cls.sb, cls.base_b = start_server(cls.work, "b")
        cls.admin_a, cls.admin_b = Client(cls.base_a), Client(cls.base_b)
        for ctx, client, pw in ((cls.a, cls.admin_a, "motdepasseA"), (cls.b, cls.admin_b, "motdepasseB")):
            st, r = client.call("POST", "/api/setup", {"username": "alex", "password": pw},
                                headers={"X-Setup-Code": ctx.setup_code})
            assert st == 200, r
        for client, name, kind, folder in ((cls.admin_a, "Films", "movie", "films_a"), (cls.admin_a, "Séries", "show", "series_a"),
                                           (cls.admin_b, "Films", "movie", "films_b"), (cls.admin_b, "Séries", "show", "series_b"),
                                           (cls.admin_b, "Animes", "show", "animes_b")):
            st, r = client.call("POST", "/api/admin/libraries", {"name": name, "kind": kind, "paths": [os.path.join(m, folder)]})
            assert st == 200, r
            wait_scan(client)
        st, r = cls.admin_b.call("POST", "/api/admin/users", {"username": "lea", "password": "motdepasseL"})
        assert st == 200, r
        # "papa" exists on both sides but it is not the same person
        for client, pw in ((cls.admin_a, "motdepassePA"), (cls.admin_b, "motdepassePB")):
            st, r = client.call("POST", "/api/admin/users", {"username": "papa", "password": pw})
            assert st == 200, r
        # invitation created on A (reachable), pasted on B (without an address: behind a home router, for example)
        cls.a.fed.listen()
        st, inv = cls.admin_a.call("POST", "/api/admin/federation/invite", {"address": f"127.0.0.1:{cls.a.fed.port}"})
        assert st == 200, inv
        cls.invite = inv["invite"]
        st, cls.preview = cls.admin_b.call("POST", "/api/admin/federation/join/preview", {"invite": inv["invite"]})
        assert st == 200, cls.preview
        # alex: the same person; papa: by default, two people
        st, cls.joined = cls.admin_b.call("POST", "/api/admin/federation/join",
                                          {"invite": inv["invite"], "accounts": {"alex": {"merge": True}}})
        assert st == 200, cls.joined
        cls.peer_on_a = cls.a.db.scalar("SELECT id FROM peers")
        cls.peer_on_b = cls.b.db.scalar("SELECT id FROM peers")
        cls.sync()

    @classmethod
    def tearDownClass(cls):
        for ctx, server in ((cls.a, cls.sa), (cls.b, cls.sb)):
            ctx.fed.stop()
            server.shutdown()
        shutil.rmtree(cls.work, ignore_errors=True)

    @classmethod
    def sync(cls):
        for _ in range(2):  # both ways, twice: what comes back from the other is applied too
            cls.a.fed.sync(cls.peer_on_a)
            cls.b.fed.sync(cls.peer_on_b)


    def test_40_peer_as_cold_storage(self):
        """A linked server keeps the other's archives (space reserved by its administrator)."""
        st, r = self.admin_b.call("GET", "/api/admin/cold")
        self.assertIsNone(r["available"]["peer"])
        # A hosts nothing until its administrator reserves space
        st, r = self.admin_b.call("POST", "/api/admin/cold/stores", {"kind": "peer", "name": "Chez A", "conf": {"peer": self.peer_on_b}})
        self.assertEqual(st, 400, r)
        self.assertIn("accueille pas", r["error"])
        self.admin_a.call("PUT", "/api/admin/cold/extra", {"host_gb": 5})
        st, r = self.admin_b.call("POST", "/api/admin/cold/stores", {"kind": "peer", "name": "Chez A", "conf": {"peer": self.peer_on_b}})
        self.assertEqual(st, 200, r)
        store = r["id"]
        movie = self.b.db.one("SELECT * FROM items WHERE type='movie' AND peer_id IS NULL AND key LIKE '%Ciel.Bas%'")
        st, r = self.admin_b.call("POST", "/api/admin/cold/archive", {"ids": [movie["id"]], "store": store})
        self.assertEqual(r["queued"], 1, r)
        for _ in range(300):
            st, r = self.admin_b.call("GET", "/api/admin/cold")
            if not [j for j in r["jobs"] if j["state"] in ("queued", "running")]:
                break
            time.sleep(0.2)
        self.assertEqual(r["jobs"], [], r["jobs"])
        hosted = os.path.join(self.a.data_dir, "cold-hosted", str(self.peer_on_a))
        files = [os.path.join(d, f) for d, _, fs in os.walk(hosted) for f in fs]
        self.assertEqual([os.path.getsize(f) for f in files], [movie["file_size"]], files)
        st, x = self.admin_a.call("GET", "/api/admin/cold/extra")
        self.assertEqual([h["bytes"] for h in x["hosted"]], [movie["file_size"]])
        # playback from the copy kept by A
        st, p = self.admin_b.call("POST", "/api/play", {"id": str(movie["id"]), "cold": True})
        self.assertEqual(st, 200, p)
        self.assertTrue(self.b.transcoder.get(p["stream"]).path.startswith("cold:"))
        n, data, _ = hls_segment(self.admin_b, p["url"], at=5.0)
        self.assertGreater(len(data), 1000)
        self.admin_b.call("POST", "/api/stop", {"stream": p["stream"]})
        # copy removed: erased on A too
        st, r = self.admin_b.call("DELETE", f"/api/admin/cold/items/{movie['id']}/{store}")
        self.assertEqual(st, 200, r)
        self.assertEqual([f for d, _, fs in os.walk(hosted) for f in fs], [])
        self.admin_b.call("DELETE", f"/api/admin/cold/stores/{store}")
        self.admin_a.call("PUT", "/api/admin/cold/extra", {"host_gb": 0})
    def login(self, base, username, password):
        c = Client(base)
        st, r = c.call("POST", "/api/login", {"username": username, "password": password})
        self.assertEqual(st, 200, r)
        return c

    # ---------------------------------------------------------------- jumelage
    def test_01_paired(self):
        st, va = self.admin_a.call("GET", "/api/admin/federation")
        self.assertEqual(st, 200)
        self.assertEqual(len(va["peers"]), 1)
        pb = va["peers"][0]
        self.assertTrue(pb["online"], pb)
        self.assertIsNone(pb["address"])          # B is not reachable: A goes through the tunnel
        self.assertEqual(pb["via"], "tunnel")
        st, vb = self.admin_b.call("GET", "/api/admin/federation")
        self.assertEqual(vb["peers"][0]["via"], "direct")
        self.assertEqual(vb["peers"][0]["fingerprint"], va["fingerprint"])
        # the invitation only works once
        st, r = self.admin_b.call("POST", "/api/admin/federation/join", {"invite": self.invite})
        self.assertEqual(st, 409, r)

    def test_02a_same_username_different_people(self):
        self.assertEqual([c["username"] for c in self.preview["conflicts"]], ["alex", "papa"])
        suggested = self.preview["conflicts"][1]["suggested"]
        self.assertEqual(suggested, "papa-" + re.sub(r"[^\w.-]+", "", self.b.fed.name().lower())[:12])
        # A's papa keeps his username, B's is renamed, each with his password, on both servers
        for base in (self.base_a, self.base_b):
            self.login(base, "papa", "motdepassePA")
            self.login(base, suggested, "motdepassePB")
            st, _ = Client(base).call("POST", "/api/login", {"username": "papa", "password": "motdepassePB"})
            self.assertEqual(st, 401)

    def test_02_accounts_replicated(self):
        # same username on both sides: A's account wins (A's password everywhere)
        self.login(self.base_b, "alex", "motdepasseA")
        st, _ = Client(self.base_b).call("POST", "/api/login", {"username": "alex", "password": "motdepasseB"})
        self.assertEqual(st, 401)
        # account that only existed on B: present on A
        self.login(self.base_a, "lea", "motdepasseL")
        # created on B after pairing -> on A; renamed on A -> on B; deleted on A -> no longer on B
        st, tom = self.admin_b.call("POST", "/api/admin/users", {"username": "tom", "password": "motdepasseT"})
        self.assertEqual(st, 200, tom)
        self.sync()
        tom_a = self.login(self.base_a, "tom", "motdepasseT")
        st, me = tom_a.call("GET", "/api/me")
        uid_a = me["user"]["id"]
        st, r = self.admin_a.call("PUT", f"/api/admin/users/{uid_a}", {"display_name": "Thomas"})
        self.assertEqual(st, 200, r)
        self.sync()
        st, users = self.admin_b.call("GET", "/api/admin/users")
        self.assertEqual(next(u for u in users if u["username"] == "tom")["display_name"], "Thomas")
        st, r = self.admin_a.call("DELETE", f"/api/admin/users/{uid_a}")
        self.assertEqual(st, 200, r)
        self.sync()
        st, users = self.admin_b.call("GET", "/api/admin/users")
        self.assertNotIn("tom", [u["username"] for u in users])

    def lib(self, client, name, server=False):
        st, libs = client.call("GET", "/api/libraries")
        return next((l for l in libs if l["title"] == name and bool(l["server"]) == server), None)

    def titles(self, client, lib):
        st, items = client.call("GET", f"/api/libraries/{lib['id']}/items")
        return sorted(i["title"] for i in items["items"])

    def test_03_catalog_shared(self):
        # no setting: B's libraries are added, under their name, with the server name for the badge
        st, libs = self.admin_a.call("GET", "/api/libraries")
        name_b = self.b.fed.name()
        self.assertEqual(sorted((l["title"], l["server"] or "") for l in libs),
                         sorted([("Films", ""), ("Séries", ""), ("Films", name_b), ("Séries", name_b), ("Animes", name_b)]))
        remote = self.lib(self.admin_a, "Séries", server=True)
        st, items = self.admin_a.call("GET", f"/api/libraries/{remote['id']}/items")
        self.assertEqual([i["title"] for i in items["items"]], ["Les Veilleurs"])
        show = items["items"][0]
        st, d = self.admin_a.call("GET", f"/api/items/{show['id']}")
        self.assertEqual(st, 200, d)
        self.assertEqual(len(d["seasons"]), 2)
        # the library administration page only shows this server's
        st, admin_libs = self.admin_a.call("GET", "/api/admin/libraries")
        self.assertEqual(sorted(l["name"] for l in admin_libs["libraries"]), ["Films", "Séries"])
        st, r = self.admin_a.call("DELETE", f"/api/admin/libraries/{remote['id']}")
        self.assertEqual(st, 404)
        # poster relayed (picture taken from the video by B)
        st, img, h = self.admin_a.call("GET", f"/api/img?p=/library/metadata/{show['id']}/thumb/1&w=200&h=300", raw=True)
        self.assertEqual(st, 200, img[:200])
        self.assertEqual(h.get("Content-Type"), "image/jpeg")
        self.assertTrue(img.startswith(b"\xff\xd8"), img[:40])  # a real JPEG picture, not a page

    def test_04_merge(self):
        st, v = self.admin_a.call("GET", "/api/admin/federation")
        received = {l["name"]: l for l in v["peers"][0]["libraries"]}
        local = {l["name"]: l for l in v["local_libraries"]}
        # movies and shows do not merge with each other
        st, r = self.admin_a.call("PUT", f"/api/admin/federation/libraries/{received['Films']['id']}",
                                  {"merge_into": local["Séries"]["id"]})
        self.assertEqual(st, 400, r)
        for name in ("Films", "Séries"):
            st, v = self.admin_a.call("PUT", f"/api/admin/federation/libraries/{received[name]['id']}",
                                      {"merge_into": local[name]["id"]})
            self.assertEqual(st, 200, v)
        merged = {l["name"]: l for l in v["peers"][0]["libraries"]}
        self.assertEqual((merged["Films"]["merge_into"], merged["Films"]["merged"]), (local["Films"]["id"], 1))
        # only one Films library and one Séries left; Animes stays apart
        st, libs = self.admin_a.call("GET", "/api/libraries")
        self.assertEqual(sorted(l["title"] for l in libs), ["Animes", "Films", "Séries"])
        films = self.lib(self.admin_a, "Films")
        self.assertEqual(self.titles(self.admin_a, films), ["Brume", "Ciel Bas", "Le Grand Voyage", "Nuit Polaire"])
        series = self.lib(self.admin_a, "Séries")
        self.assertEqual(self.titles(self.admin_a, series), ["Les Veilleurs"])
        show = self.a.db.scalar("SELECT id FROM items WHERE type='show' AND peer_id IS NULL")
        st, d = self.admin_a.call("GET", f"/api/items/{show}")
        self.assertEqual([s["title"] for s in d["seasons"]], ["Saison 1", "Saison 2"])
        st, eps = self.admin_a.call("GET", f"/api/items/{d['seasons'][0]['id']}/children")
        self.assertEqual([e["episode"] for e in eps], [1, 2, 3])
        # search: only once
        st, res = self.admin_a.call("GET", "/api/search?q=brume")
        self.assertEqual(sum(1 for v in res.values() if isinstance(v, list) for i in v if i["title"] == "Brume"), 1)

    def remote_episode(self):
        rk = self.a.db.scalar("SELECT id FROM items WHERE peer_id IS NOT NULL AND alias_of IS NULL AND type='episode' "
                              "ORDER BY idx, id")
        remote = self.a.db.scalar("SELECT remote_id FROM items WHERE id=?", (rk,))
        return str(rk), remote

    def test_05_play_from_other_server(self):
        rk, remote = self.remote_episode()
        path = self.b.db.scalar("SELECT key FROM items WHERE id=?", (remote,))
        caps = {"video": ["h264", "hevc"], "audio": ["aac", "ac3", "mp3"], "containers": ["mp4", "matroska", "mov"],
                "native": True}
        st, p = self.admin_a.call("POST", "/api/play", {"id": rk, "caps": caps})
        self.assertEqual(st, 200, p)
        self.assertEqual(p["item"]["id"], rk)
        self.assertEqual(p["mode"], "direct")
        st, data, h = self.admin_a.call("GET", p["url"], headers={"Range": "bytes=0-99"}, raw=True)
        self.assertEqual(st, 206)
        with open(path, "rb") as f:
            self.assertEqual(data, f.read(100))
        st, streams = self.admin_a.call("GET", "/api/admin/streams")
        self.assertIn(p["stream"], [s["id"] for s in streams])
        # progress: saved here and sent back to the server that has the file
        st, r = self.admin_a.call("POST", "/api/progress", {"id": rk, "position": 42000, "duration": 600000,
                                                            "stream": p["stream"]})
        self.assertEqual(st, 200, r)
        alex_b = self.login(self.base_b, "alex", "motdepasseA")
        for _ in range(50):
            st, d = alex_b.call("GET", f"/api/items/{remote}")
            if d.get("position") == 42000:
                break
            time.sleep(0.1)
        self.assertEqual(d.get("position"), 42000, d)
        self.admin_a.call("POST", "/api/stop", {"stream": p["stream"]})
        # stream prepared by B (remux), relayed by A
        st, p = self.admin_a.call("POST", "/api/play", {"id": rk, "no_direct": True})
        self.assertEqual(st, 200, p)
        self.assertTrue(p["url"].endswith("main.m3u8"))
        st, pl, _ = self.admin_a.call("GET", p["url"], raw=True)
        self.assertEqual(st, 200)
        self.assertIn(b"#EXTM3U", pl)
        st, init, _ = self.admin_a.call("GET", p["url"].replace("main.m3u8", "init.mp4"), raw=True)
        self.assertEqual(st, 200)
        self.admin_a.call("POST", "/api/stop", {"stream": p["stream"]})
        # offline download: prepared by B, relayed by A
        caps = {"video": ["h264", "hevc"], "audio": ["aac"], "containers": ["mp4"], "native": True}
        st, r = self.admin_a.call("POST", "/api/downloads/options", {"id": rk, "caps": caps})
        self.assertEqual(st, 200, r)
        self.assertEqual(len(r["options"]), 4)
        st, job = self.admin_a.call("POST", "/api/downloads", {"id": rk, "quality": "auto", "caps": caps})
        self.assertEqual(st, 200, job)
        self.assertEqual(job["item_id"], rk)
        self.assertIn(job["id"], self.a.fed.dl_jobs)
        for _ in range(300):
            st, job = self.admin_a.call("GET", f"/api/downloads/{job['id']}")
            self.assertEqual(st, 200, job)
            if job["state"] in ("ready", "error"):
                break
            time.sleep(0.2)
        self.assertEqual((job["state"], job["item_id"]), ("ready", rk), job)
        st, data, h = self.admin_a.call("GET", f"/api/downloads/{job['id']}/file", headers={"Range": "bytes=0-99"}, raw=True)
        self.assertEqual((st, len(data)), (206, 100))
        self.assertIn(b"ftyp", data[:16])
        # another account does not see this download
        lea = self.login(self.base_a, "lea", "motdepasseL")
        self.assertEqual(lea.call("GET", f"/api/downloads/{job['id']}")[0], 404)
        st, _ = self.admin_a.call("DELETE", f"/api/downloads/{job['id']}")
        self.assertEqual(st, 200)
        self.assertNotIn(job["id"], self.a.fed.dl_jobs)
        for _ in range(50):
            if not self.b.downloads.jobs:
                break
            time.sleep(0.1)
        self.assertEqual(self.b.downloads.jobs, {})  # B erased its preparation

    def test_06_best_copy(self):
        brume = self.a.db.scalar("SELECT id FROM items WHERE title='Brume' AND peer_id IS NULL")
        # the wanted language only exists on A's copy (French): A, despite the lower resolution
        st, p = self.admin_a.call("POST", "/api/play", {"id": str(brume), "no_direct": True})
        self.assertEqual(st, 200, p)
        self.assertNotIn(p["stream"], self.a.fed.proxied)
        self.admin_a.call("POST", "/api/stop", {"stream": p["stream"]})
        # language present nowhere: the best quality, B's 360p copy
        self.admin_a.call("PUT", "/api/me/prefs", {"audio_lang": "deu"})
        st, p = self.admin_a.call("POST", "/api/play", {"id": str(brume), "no_direct": True})
        self.assertEqual(st, 200, p)
        self.assertIn(p["stream"], self.a.fed.proxied)
        self.assertEqual(p["item"]["id"], str(brume))
        # same title started again (seek): same copy
        st, p2 = self.admin_a.call("POST", "/api/play", {"id": str(brume), "no_direct": True, "offset": 2000,
                                                         "replace": p["stream"]})
        self.assertEqual(st, 200, p2)
        self.assertIn(p2["stream"], self.a.fed.proxied)
        self.assertNotIn(p["stream"], self.a.fed.proxied)
        # during playback, the title's sub-pages come from the copy being played
        st, tp = self.admin_a.call("GET", f"/api/items/{brume}/trickplay")
        self.assertEqual(st, 200, tp)
        self.admin_a.call("POST", "/api/stop", {"stream": p2["stream"]})
        # track chosen on the title page (English, only present on B): B
        self.admin_a.call("PUT", "/api/me/prefs", {"audio_lang": "fra"})
        st, p = self.admin_a.call("POST", "/api/play", {"id": str(brume), "no_direct": True})
        self.assertNotIn(p["stream"], self.a.fed.proxied)
        self.admin_a.call("POST", "/api/stop", {"stream": p["stream"]})
        self.admin_a.call("PUT", f"/api/items/{brume}/tracks", {"audio_lang": "eng"})
        st, p = self.admin_a.call("POST", "/api/play", {"id": str(brume), "no_direct": True})
        self.assertIn(p["stream"], self.a.fed.proxied)
        self.assertEqual(p["audio"][0]["lang"], "eng")
        self.admin_a.call("POST", "/api/stop", {"stream": p["stream"]})
        self.admin_a.call("PUT", f"/api/items/{brume}/tracks", {"reset": True})

    def test_07_progress_comes_back(self):
        rk, remote = self.remote_episode()
        alex_b = self.login(self.base_b, "alex", "motdepasseA")
        st, r = alex_b.call("POST", f"/api/items/{remote}/watched", {"watched": True})
        self.assertEqual(st, 200, r)
        # title present on both: watched on B -> watched on our copy
        brume_b = self.b.db.scalar("SELECT id FROM items WHERE title='Brume' AND peer_id IS NULL")
        st, r = alex_b.call("POST", f"/api/items/{brume_b}/watched", {"watched": True})
        self.sync()
        st, d = self.admin_a.call("GET", f"/api/items/{rk}")
        self.assertTrue(d["watched"], d)
        brume = self.a.db.scalar("SELECT id FROM items WHERE title='Brume' AND peer_id IS NULL")
        st, d = self.admin_a.call("GET", f"/api/items/{brume}")
        self.assertTrue(d["watched"], d)

    def test_08_offline_server(self):
        fed = self.a.fed
        fed.set_online(self.peer_on_a, False)
        try:
            st, libs = self.admin_a.call("GET", "/api/libraries")
            self.assertEqual(sorted(l["title"] for l in libs), ["Films", "Séries"])  # B's Animes hidden
            films = self.lib(self.admin_a, "Films")
            self.assertEqual(self.titles(self.admin_a, films), ["Brume", "Le Grand Voyage", "Nuit Polaire"])
            show = self.a.db.scalar("SELECT id FROM items WHERE type='show' AND peer_id IS NULL")
            st, d = self.admin_a.call("GET", f"/api/items/{show}")
            self.assertEqual([s["title"] for s in d["seasons"]], ["Saison 1"])
            # the title present on both sides plays from our copy, even if B would have been chosen
            self.admin_a.call("PUT", "/api/me/prefs", {"audio_lang": "deu"})
            brume = self.a.db.scalar("SELECT id FROM items WHERE title='Brume' AND peer_id IS NULL")
            st, p = self.admin_a.call("POST", "/api/play", {"id": str(brume), "no_direct": True})
            self.assertEqual(st, 200, p)
            self.assertNotIn(p["stream"], fed.proxied)
            self.admin_a.call("POST", "/api/stop", {"stream": p["stream"]})
        finally:
            self.admin_a.call("PUT", "/api/me/prefs", {"audio_lang": "fra"})
            fed.set_online(self.peer_on_a, True)
        films = self.lib(self.admin_a, "Films")
        self.assertEqual(len(self.titles(self.admin_a, films)), 4)

    def test_09_local_item_removed(self):
        """Our episode deleted: B's takes its place back instead of disappearing with it."""
        ep = self.a.db.scalar("SELECT id FROM items WHERE type='episode' AND peer_id IS NULL")
        self.a.db.run("DELETE FROM items WHERE id=?", (ep,))
        self.a.fed.relink(self.peer_on_a)
        self.a.db.release()
        show = self.a.db.scalar("SELECT id FROM items WHERE type='show' AND peer_id IS NULL")
        st, d = self.admin_a.call("GET", f"/api/items/{show}")
        st, eps = self.admin_a.call("GET", f"/api/items/{d['seasons'][0]['id']}/children")
        self.assertEqual([e["episode"] for e in eps], [1, 2, 3])

    def test_10_link_is_protected(self):
        port = self.a.fed.port
        st, _ = fed_raw(port, "GET", "/fed/hello")
        self.assertEqual(st, 401)
        uid_b = self.b.fed.uid()
        st, _ = fed_raw(port, "GET", "/fed/hello", {"X-Fed-Peer": uid_b, "X-Fed-Key": "x" * 43})
        self.assertEqual(st, 401)
        secret = self.a.db.scalar("SELECT secret FROM peers")
        ok = {"X-Fed-Peer": uid_b, "X-Fed-Key": secret}
        st, body = fed_raw(port, "GET", "/fed/hello", ok)
        self.assertEqual(st, 200, body)
        # administration on behalf of an account: only if it is an administrator here, and if this server accepts it
        alex = self.a.db.scalar("SELECT uid FROM users WHERE username='alex'")
        lea = self.a.db.scalar("SELECT uid FROM users WHERE username='lea'")
        st, _ = fed_raw(port, "GET", "/api/admin/users", dict(ok, **{"X-Fed-User": alex}))
        self.assertEqual(st, 200)
        st, _ = fed_raw(port, "GET", "/api/admin/users", dict(ok, **{"X-Fed-User": lea}))
        self.assertEqual(st, 403)
        self.a.db.set_setting("fed_remote_admin", "0")
        try:
            st, _ = fed_raw(port, "GET", "/api/admin/users", dict(ok, **{"X-Fed-User": alex}))
            self.assertEqual(st, 403)
        finally:
            self.a.db.set_setting("fed_remote_admin", None)
        st, _ = fed_raw(port, "GET", "/api/me", dict(ok, **{"X-Fed-User": alex}))
        self.assertEqual(st, 403)  # the rest of the API stays closed
        # an account's avatar: route with a parameter, served (and not the interface page)
        st, body = fed_raw(port, "GET", f"/fed/avatar/{alex}", ok)
        self.assertEqual(st, 404, body[:80])
        # the interface is not served on this port, and the server-to-server routes not on the other
        st, _ = fed_raw(port, "GET", "/", ok)
        self.assertEqual(st, 404)
        st, _ = self.admin_a.call("GET", "/fed/hello")
        self.assertEqual(st, 404)
        # jeton d'invitation inconnu
        st, _ = fed_raw(port, "POST", "/fed/join", {"Content-Type": "application/json"},
                        json.dumps({"token": "z" * 32, "uid": "0" * 32, "fingerprint": "0" * 64}))
        self.assertEqual(st, 403)
        # invitation whose fingerprint does not match the server contacted
        from noxa.federation import decode_invite, encode_invite
        st, inv = self.admin_a.call("POST", "/api/admin/federation/invite", {"address": f"127.0.0.1:{port}"})
        data = decode_invite(inv["invite"])
        data["f"] = "ab" * 32
        data["id"] = "cd" * 16
        st, r = self.admin_b.call("POST", "/api/admin/federation/join", {"invite": encode_invite(data)})
        self.assertEqual(st, 502, r)
        self.assertIn("Certificat inattendu", r["error"])

    def test_11_same_username_created_on_both(self):
        """The same username created on both sides before the synchronisation: two accounts, one renamed everywhere."""
        st, r = self.admin_a.call("POST", "/api/admin/users", {"username": "nina", "password": "motdepasseNA"})
        self.assertEqual(st, 200, r)
        st, r = self.admin_b.call("POST", "/api/admin/users", {"username": "Nina", "password": "motdepasseNB"})
        self.assertEqual(st, 200, r)
        self.sync()
        self.sync()
        q = "SELECT username, uid, password_hash FROM users WHERE username LIKE 'nina%' ORDER BY uid"
        rows_a, rows_b = self.a.db.all(q), self.b.db.all(q)
        self.assertEqual(rows_a, rows_b)
        self.assertEqual(sorted(r["username"].lower() for r in rows_a), ["nina", "nina-2"])
        renamed = next(r["username"] for r in rows_a if r["username"].lower() == "nina-2")
        for base in (self.base_a, self.base_b):  # each keeps their password
            logins = {(name, pw): Client(base).call("POST", "/api/login", {"username": name, "password": pw})[0]
                      for name, pw in (("nina", "motdepasseNA"), (renamed, "motdepasseNB"),
                                       ("nina", "motdepasseNB"), (renamed, "motdepasseNA"))}
            self.assertEqual(sorted(logins.values()).count(200), 2, logins)
        st, v = self.admin_a.call("GET", "/api/admin/federation")
        self.assertIn(renamed.lower(), [n["renamed"].lower() for n in v["notices"]])

    def test_12_load_balancing(self):
        """Server already loaded: the copy of a free server is played, even in lower quality."""
        st, v = self.admin_a.call("PUT", "/api/admin/federation", {"busy": 2})
        self.assertEqual((st, v["busy"]), (200, 2), v)
        self.assertEqual(self.admin_a.call("PUT", "/api/admin/federation", {"busy": 99})[0], 400)
        brume = str(self.a.db.scalar("SELECT id FROM items WHERE title='Brume' AND peer_id IS NULL"))
        st_b = self.a.fed.status.setdefault(self.peer_on_a, {})
        self.admin_a.call("PUT", "/api/me/prefs", {"audio_lang": "deu"})  # no copy has the language: quality decides
        try:
            st_b["load"] = 5  # B is busy: our copy (180p) rather than its own (360p)
            st, p = self.admin_a.call("POST", "/api/play", {"id": brume, "no_direct": True})
            self.assertEqual(st, 200, p)
            self.assertNotIn(p["stream"], self.a.fed.proxied)
            self.admin_a.call("POST", "/api/stop", {"stream": p["stream"]})
            st_b["load"] = 0  # B libre : sa copie, meilleure
            st, p = self.admin_a.call("POST", "/api/play", {"id": brume, "no_direct": True})
            self.assertIn(p["stream"], self.a.fed.proxied)
            self.assertEqual(st_b["load"], 1)  # counted on its side until its next announcement
            self.admin_a.call("POST", "/api/stop", {"stream": p["stream"]})
        finally:
            st_b["load"] = 0
            self.admin_a.call("PUT", "/api/me/prefs", {"audio_lang": "fra"})
            self.admin_a.call("PUT", "/api/admin/federation", {"busy": 3})

    def test_13_watch_together_across_servers(self):
        """Group created on A, joined from B: titles, accounts and commands translated from one server to the other."""
        brume_a = self.a.db.scalar("SELECT id FROM items WHERE title='Brume' AND peer_id IS NULL")
        brume_on_b = str(self.b.db.scalar("SELECT id FROM items WHERE peer_id IS NOT NULL AND remote_id=?", (brume_a,)))
        st, g = self.admin_a.call("POST", "/api/together", {"item_id": str(brume_a), "position": 5000, "name": "Soirée"})
        self.assertEqual(st, 200, g)
        gid = g["id"]
        lea = self.login(self.base_b, "lea", "motdepasseL")
        st, lst = lea.call("GET", "/api/together")
        self.assertEqual(st, 200, lst)
        mine = [x for x in lst if x["id"] == gid]
        self.assertEqual(len(mine), 1, lst)
        self.assertEqual((mine[0]["name"], mine[0]["item"]["id"], mine[0]["member"]), ("Soirée", brume_on_b, False))
        st, j = lea.call("POST", f"/api/together/{gid}/join")
        self.assertEqual(st, 200, j)
        lea_b = self.b.db.scalar("SELECT id FROM users WHERE username='lea'")
        self.assertEqual((j["item_id"], j["item"]["id"], j["position"], j["member"]), (brume_on_b, brume_on_b, 5000, True))
        self.assertIn(lea_b, [m["id"] for m in j["members"]])
        # events relayed by B
        cookie = "; ".join(f"{c.name}={c.value}" for c in lea.jar)
        conn = http.client.HTTPConnection("127.0.0.1", self.sb.server_address[1], timeout=15)
        conn.request("GET", f"/api/together/{gid}/events", headers={"Cookie": cookie})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)

        def next_event(kind):
            deadline = time.time() + 15
            while time.time() < deadline:
                line = resp.fp.readline()
                if line.startswith(b"data: "):
                    ev = json.loads(line[6:])
                    if ev.get("type") == kind:
                        return ev
            self.fail(f"pas d'événement {kind}")
        members = next_event("members")
        self.assertIn(lea_b, [m["id"] for m in members["members"]])
        state = next_event("state")
        self.assertEqual(state["item_id"], brume_on_b)
        # host (A) ready: start; pause from B, seen by A
        self.admin_a.call("POST", f"/api/together/{gid}/cmd", {"action": "ready"})
        st, r = lea.call("POST", f"/api/together/{gid}/cmd", {"action": "pause", "position": 7000})
        self.assertEqual((st, r["playing"], r["position"]), (200, False, 7000), r)
        st, ga = self.admin_a.call("GET", f"/api/together/{gid}")
        self.assertEqual((ga["playing"], ga["position"]), (False, 7000))
        # title change from B (a title of B): A gets its own
        ciel_b = self.b.db.scalar("SELECT id FROM items WHERE title='Ciel Bas' AND peer_id IS NULL")
        ciel_on_a = self.a.db.scalar("SELECT COALESCE(alias_of, id) FROM items WHERE peer_id IS NOT NULL AND remote_id=?",
                                     (ciel_b,))
        st, r = lea.call("POST", f"/api/together/{gid}/cmd", {"action": "item", "item_id": str(ciel_b)})
        self.assertEqual((st, r["item_id"]), (200, str(ciel_b)), r)
        st, ga = self.admin_a.call("GET", f"/api/together/{gid}")
        self.assertEqual(ga["item_id"], str(ciel_on_a))
        ev = next_event("state")
        while ev.get("action") != "item":
            ev = next_event("state")
        self.assertEqual(ev["item_id"], str(ciel_b))
        conn.close()
        st, _ = lea.call("POST", f"/api/together/{gid}/leave")
        self.assertEqual(st, 200)
        st, ga = self.admin_a.call("GET", f"/api/together/{gid}")
        self.assertNotIn("Léa", [m["name"] for m in ga["members"]])
        self.admin_a.call("POST", f"/api/together/{gid}/leave")

    def test_14_shared_tmdb_and_seerr(self):
        """TMDB key and Seerr set up on A only: B uses them (metadata, Discover, requests)."""
        from tests.fixtures import FakeSeerr
        from tests.test_api import TMDB
        seerr = FakeSeerr()
        try:
            self.assertFalse(self.b.metadata.tmdb().enabled)
            st, r = self.admin_a.call("PUT", "/api/admin/settings", {"tmdb_key": TMDB.key})
            self.assertEqual(st, 200, r)
            st, r = self.admin_a.call("PUT", "/api/admin/requests", {"seerr_url": seerr.url, "seerr_key": seerr.key})
            self.assertEqual(st, 200, r)
            self.sync()
            self.assertTrue(self.b.metadata.tmdb().enabled)
            # B's titles left without metadata for lack of a key: looked up with A's
            for _ in range(100):
                left = self.b.db.scalar("SELECT COUNT(*) FROM items WHERE peer_id IS NULL AND type IN ('movie','show') "
                                        "AND meta_state IN ('local','pending')")
                if not left:
                    break
                time.sleep(0.1)
            self.assertEqual(left, 0)
            lea = self.login(self.base_b, "lea", "motdepasseL")
            st, me = lea.call("GET", "/api/me")
            self.assertEqual((me["features"]["discover"], me["features"]["requests"]), (True, True))
            st, home = lea.call("GET", "/api/discover/home")
            self.assertEqual((st, home["seerr"]), (200, True), home)
            st, cfg = self.admin_b.call("GET", "/api/admin/requests")
            self.assertEqual((cfg["enabled"], cfg["shared_by"]), (True, self.a.fed.name()))
            st, r = lea.call("POST", "/api/discover/request", {"type": "movie", "tmdb_id": 9101})
            self.assertEqual((st, r["as_user"]), (200, True), r)
            lea_seerr = next(u for u in seerr.users if u.get("username") == "lea")
            self.assertEqual(seerr.requests[-1]["user"], lea_seerr["id"])
            # only the useful Seerr routes are lent
            peer = self.b.fed.peer(self.peer_on_b)
            st, r = self.b.fed.call(peer, "POST", "/fed/seerr", {"method": "GET", "path": "/settings/main"})
            self.assertEqual(st, 403, r)
            st, r = self.b.fed.call(peer, "POST", "/fed/tmdb", {"path": "/../account", "params": {}})
            self.assertEqual(st, 400, r)
        finally:
            self.admin_a.call("PUT", "/api/admin/settings", {"tmdb_key": ""})
            self.admin_a.call("PUT", "/api/admin/requests", {"seerr_url": "", "seerr_key": ""})
            seerr.httpd.shutdown()
            self.sync()

    def test_15_remote_admin(self):
        """An administrator signed in to A sets up B: administration requests relayed on behalf of the same account."""
        st, sv = self.admin_a.call("GET", "/api/admin/servers")
        self.assertEqual(st, 200, sv)
        self.assertEqual([p["id"] for p in sv["peers"]], [str(self.peer_on_a)])
        on_b = {"X-Noxa-Server": str(self.peer_on_a)}
        st, libs = self.admin_a.call("GET", "/api/admin/libraries", headers=on_b)
        self.assertEqual(st, 200, libs)
        self.assertIn("Animes", [l["name"] for l in libs["libraries"]])  # the library only exists on B
        st, libs_a = self.admin_a.call("GET", "/api/admin/libraries")
        self.assertNotIn("Animes", [l["name"] for l in libs_a["libraries"] if not l.get("peer")])
        # a setting changed from A only applies to B
        st, r = self.admin_a.call("PUT", "/api/admin/settings", {"watch_interval": 123}, headers=on_b)
        self.assertEqual(st, 200, r)
        self.assertEqual(self.b.db.setting("watch_interval"), "123")
        self.assertNotEqual(self.a.db.setting("watch_interval"), "123")
        st, r = self.admin_a.call("GET", f"/api/admin/settings?_server={self.peer_on_a}")  # same thing through the address
        self.assertEqual((st, r["watch_interval"]), (200, 123))
        # the server list stays this one's
        st, sv2 = self.admin_a.call("GET", "/api/admin/servers", headers=on_b)
        self.assertEqual(sv2["local"]["name"], sv["local"]["name"])
        # an ordinary account cannot use it
        lea = self.login(self.base_a, "lea", "motdepasseL")
        self.assertEqual(lea.call("GET", "/api/admin/settings", headers=on_b)[0], 403)
        self.assertEqual(self.admin_a.call("GET", "/api/admin/settings", headers={"X-Nexis-Server": "999"})[0], 404)
        # B can refuse to be administered from a linked server (setting made on site)
        st, r = self.admin_b.call("PUT", "/api/admin/servers", {"remote_admin": False})
        self.assertEqual((st, r["remote_admin"]), (200, False))
        st, r = self.admin_a.call("GET", "/api/admin/settings", headers=on_b)
        self.assertEqual(st, 403)
        self.assertIn("n'accepte pas", r["error"])
        self.admin_b.call("PUT", "/api/admin/servers", {"remote_admin": True})
        # the server-to-server routes stay closed to the rest of the API (no "/api/me" on behalf of an account)
        st, r = self.a.fed.call(self.a.fed.peer(self.peer_on_a), "GET", "/api/me",
                                user=self.a.db.one("SELECT * FROM users WHERE username='alex'"))
        self.assertEqual(st, 403)

    def test_16_backups_copied_between_servers(self):
        """A backup of A is copied to B, stored under A's name; B keeps each server's backups apart."""
        from noxa import backup
        self.a.db.set_setting("server_name", "Serveur Salon")
        self.b.db.set_setting("server_name", "Serveur Bureau")
        b_a = backup.create(self.a, "auto", distribute=False)
        self.assertTrue(b_a["name"].startswith("noxa-serveur-salon_"), b_a["name"])
        res = backup.distribute(self.a, os.path.join(backup.directory(self.a), b_a["name"]))
        self.assertTrue(res and all(ok for _, ok in res), res)
        on_b = {b["name"]: b for b in backup.listing(self.b)}
        from noxa import __version__
        sent = backup.name_for_peer({"version": __version__}, b_a["name"])  # nexis- for a server older than 4.5
        self.assertIn(sent, on_b)
        copy = on_b[sent]
        self.assertEqual((copy["mine"], copy["server"]), (False, "Serveur Salon"))
        # from A, B's administration shows the copy, with its origin server
        st, v = self.admin_a.call("GET", "/api/admin/backups", headers={"X-Noxa-Server": str(self.peer_on_a)})
        self.assertEqual(st, 200, v)
        self.assertTrue(any(b["name"] == sent and not b["mine"] for b in v["backups"]))
        # B keeps at most N automatic copies of A, without touching its own
        mine_b = backup.create(self.b, "auto", distribute=False)
        self.b.tasks.update("backup", options={"keep": 1})
        try:
            import time as _t
            _t.sleep(1.1)
            b_a2 = backup.create(self.a, "auto", distribute=False)
            backup.distribute(self.a, os.path.join(backup.directory(self.a), b_a2["name"]))
            names = {b["name"] for b in backup.listing(self.b)}
            self.assertIn(backup.name_for_peer({"version": __version__}, b_a2["name"]), names)
            self.assertNotIn(sent, names)
            self.assertIn(mine_b["name"], names)
        finally:
            self.b.tasks.update("backup", options={"keep": 7})
        # B can refuse to keep the others' backups
        self.b.db.set_setting("backup_from_peers", "0")
        try:
            res = backup.distribute(self.a, os.path.join(backup.directory(self.a), b_a2["name"]))
            self.assertFalse(res[0][1])
            self.assertIn("ne garde pas", backup.copies_status(self.a)[f"peer:{self.peer_on_a}"]["error"])
        finally:
            self.b.db.set_setting("backup_from_peers", None)
        # a linked server cannot drop a backup on behalf of another server
        with open(os.path.join(backup.directory(self.a), b_a2["name"]), "rb") as f:
            raw = f.read()
        st, _ = fed_raw(self.a.fed.port, "POST", "/fed/backup", {"X-Fed-Peer": self.b.fed.uid(),
                        "X-Fed-Key": self.a.db.scalar("SELECT secret FROM peers"), "X-Backup-Name": "nexis-pirate_20260101-000000-auto.tar.gz",
                        "Content-Length": str(len(raw))}, raw)
        self.assertEqual(st, 400)  # it is a backup of A, not of B
        self.a.db.set_setting("server_name", None)
        self.b.db.set_setting("server_name", None)

    def test_17_prefs_follow_account(self):
        """An account's preferences follow it from one linked server to the other (libraries and wallpaper translated)."""
        alex_b = self.login(self.base_b, "alex", "motdepasseA")
        films_a = self.lib(self.admin_a, "Films")                    # A's library, seen from A
        films_a_on_b = self.lib(alex_b, "Films", server=True)        # the same, received by B
        series_b = self.lib(alex_b, "Séries")                        # B's library (apart or merged here)
        self.assertNotEqual(films_a["id"], films_a_on_b["id"])
        movie = self.admin_a.call("GET", f"/api/libraries/{films_a['id']}/items")[1]["items"][0]
        with open(os.path.join(STATIC, "themes", "ville-neon.json"), encoding="utf-8") as f:
            neon = json.load(f)
        st, r = self.admin_a.call("PUT", "/api/me/prefs", {
            "theme": "light", "accent": "#4c9aff", "sub_size": 125, "hidden_libraries": [films_a["id"]],
            "start_page": f"lib:{films_a['id']}", "nav_hidden": [f"lib:{films_a['id']}", "live"],
            "wallpaper": f"/library/metadata/{movie['id']}/art/1",
            "home_pins": [{"id": 3, "type": "genre", "ref": "Drame", "lib": films_a["id"], "title": "Drames"}],
            "home_order": ["pin:3", "continue"], "lib_langs": {films_a["id"]: {"audio_lang": "jpn"}},
            "hidden_genres": ["Horreur"], "private_mode": True, "quiet_hours": "23:00-07:30", "muted_shows": [str(movie["id"])],
            "my_themes": [neon]})
        self.assertEqual(st, 200, r)
        self.sync()
        p = alex_b.call("GET", "/api/me")[1]["prefs"]
        self.assertEqual((p["theme"], p["accent"], p["sub_size"]), ("light", "#4c9aff", 125))
        self.assertEqual(p["hidden_libraries"], [films_a_on_b["id"]])
        self.assertEqual(p["start_page"], f"lib:{films_a_on_b['id']}")
        self.assertEqual(p["nav_hidden"], [f"lib:{films_a_on_b['id']}", "live"])
        self.assertEqual(p["home_pins"][0]["lib"], films_a_on_b["id"])
        self.assertEqual(p["home_order"], ["pin:3", "continue"])
        self.assertEqual(p["lib_langs"], {films_a_on_b["id"]: {"audio_lang": "jpn"}})
        self.assertEqual((p["hidden_genres"], p["private_mode"], p["quiet_hours"]), (["Horreur"], True, "23:00-07:30"))
        self.assertEqual([t["id"] for t in p["my_themes"]], ["ville-neon"])  # installed theme: on the other server too
        st, d = alex_b.call("GET", f"/api/items/{p['muted_shows'][0]}")
        self.assertEqual((st, d["title"]), (200, movie["title"]))  # muted: same title, B's number
        wid = int(p["wallpaper"].split("/")[3])
        st, d = alex_b.call("GET", f"/api/items/{wid}")
        self.assertEqual((st, d["title"]), (200, movie["title"]))  # the same movie, with its number on B
        # the other way: a change made on B comes back to A
        st, r = alex_b.call("PUT", "/api/me/prefs", {"theme": "oled", "hidden_libraries": [series_b["id"]]})
        self.assertEqual(st, 200, r)
        self.sync()
        p = self.admin_a.call("GET", "/api/me")[1]["prefs"]
        # B's Séries seen from A: apart, or merged with A's (merge test): it is the one shown
        series_b_on_a = self.lib(self.admin_a, "Séries", server=True) or self.lib(self.admin_a, "Séries")
        self.assertEqual((p["theme"], p["hidden_libraries"]), ("oled", [series_b_on_a["id"]]))
        self.assertEqual(p["accent"], "#4c9aff")
        # back to the default settings: everywhere too
        st, r = self.admin_a.call("POST", "/api/me/prefs/reset")
        self.assertEqual((st, r["theme"]), (200, "dark"))
        self.sync()
        p = alex_b.call("GET", "/api/me")[1]["prefs"]
        self.assertEqual((p["theme"], p["hidden_libraries"], p["home_pins"]), ("dark", [], []))
        # copy an account's settings to another one (administrator)
        self.admin_a.call("PUT", "/api/me/prefs", {"theme": "light", "sub_size": 150})
        lea_id = next(u["id"] for u in self.admin_a.call("GET", "/api/admin/users")[1] if u["username"] == "lea")
        me_id = self.admin_a.call("GET", "/api/me")[1]["user"]["id"]
        st, r = self.admin_a.call("POST", f"/api/admin/users/{lea_id}/prefs", {"from": me_id})
        self.assertEqual(st, 200, r)
        lea = self.login(self.base_a, "lea", "motdepasseL")
        self.assertEqual(lea.call("GET", "/api/me")[1]["prefs"]["sub_size"], 150)
        self.assertEqual(self.admin_a.call("POST", f"/api/admin/users/{lea_id}/prefs", {"from": lea_id})[0], 400)
        self.admin_a.call("POST", "/api/me/prefs/reset")
        self.sync()

    def test_99_unlink(self):
        st, r = self.admin_b.call("DELETE", f"/api/admin/federation/peers/{self.peer_on_b}")
        self.assertEqual(st, 200, r)
        self.assertEqual(r["peers"], [])
        st, va = self.admin_a.call("GET", "/api/admin/federation")
        self.assertEqual(va["peers"], [])
        st, libs = self.admin_a.call("GET", "/api/libraries")
        self.assertEqual(sorted(l["title"] for l in libs), ["Films", "Séries"])
        films = self.lib(self.admin_a, "Films")
        self.assertEqual(self.titles(self.admin_a, films), ["Brume", "Le Grand Voyage", "Nuit Polaire"])
        # the accounts stay, on both sides, and no longer move
        self.login(self.base_a, "lea", "motdepasseL")
        self.login(self.base_b, "alex", "motdepasseA")
        st, libs_b = self.admin_b.call("GET", "/api/libraries")
        self.assertEqual(sorted(l["title"] for l in libs_b), ["Animes", "Films", "Séries"])
        self.assertFalse(self.b.db.scalar("SELECT COUNT(*) FROM items WHERE peer_id IS NOT NULL"))
        self.assertFalse(self.a.db.scalar("SELECT COUNT(*) FROM items WHERE peer_id IS NOT NULL"))
        # our episode and our show are intact (B's merged episodes are gone)
        show = self.a.db.scalar("SELECT id FROM items WHERE type='show'")
        st, d = self.admin_a.call("GET", f"/api/items/{show}")
        self.assertEqual([s["title"] for s in d["seasons"]], ["Saison 1"])

    def pair(self):
        st, inv = self.admin_a.call("POST", "/api/admin/federation/invite", {"address": f"127.0.0.1:{self.a.fed.port}"})
        self.assertEqual(st, 200, inv)
        st, r = self.admin_b.call("POST", "/api/admin/federation/join", {"invite": inv["invite"]})
        self.assertEqual(st, 200, r)
        type(self).peer_on_a = self.a.db.scalar("SELECT id FROM peers")
        type(self).peer_on_b = self.b.db.scalar("SELECT id FROM peers")
        self.sync()

    def test_99b_relink_after_one_sided_unlink(self):
        """Unlinked on one side only (the other was off): a new pairing replaces the old link."""
        self.pair()
        self.b.fed.remove(self.peer_on_b, notify=False)  # A does not learn it
        self.b.db.release()
        self.assertEqual(self.a.db.scalar("SELECT COUNT(*) FROM peers"), 1)
        self.pair()
        self.assertEqual([ctx.db.scalar("SELECT COUNT(*) FROM peers") for ctx in (self.a, self.b)], [1, 1])
        st, v = self.admin_a.call("GET", "/api/admin/federation")
        self.assertTrue(v["peers"][0]["online"], v)
        st, libs = self.admin_a.call("GET", "/api/libraries")
        self.assertEqual(sorted(l["title"] for l in libs), ["Animes", "Films", "Films", "Séries", "Séries"])


if __name__ == "__main__":
    unittest.main()
