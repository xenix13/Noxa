"""Linked servers: synchronisation of accounts, preferences, progress and catalogues."""
import http.client
import json
import os
import re
import threading
import time

from .. import i18n
from ..auth import USERNAME, find_user
from ..catalog import CARD_SQL
from ..names import fold
from ..web import HTTPError
from .common import BEDTIME, CATALOG_PAGE, FedError, HEX32, ITEM_LOCAL, LIMIT_FIELDS, PROGRESS_PAGE, USER_FIELDS, free_username, log


class SyncMixin:
    # ------------------------------------------------------------------ synchronisation
    def sync(self, pid):
        with self.lock:
            lk = self.sync_locks.setdefault(pid, threading.Lock())
        if not lk.acquire(blocking=False):
            return
        db = self.ctx.db
        st = {"checked": int(time.time())}
        try:
            peer = self.peer(pid)
            db.release()
            t0, w0 = time.monotonic(), time.time() * 1000
            hello = self.call_ok(peer, "GET", "/fed/hello", timeout=15)
            w1 = time.time() * 1000
            caps = hello.get("caps") if isinstance(hello.get("caps"), dict) else {}
            st.update(online=True, latency=int((time.monotonic() - t0) * 1000), fails=0,
                      tmdb=bool(caps.get("tmdb")), seerr=bool(caps.get("seerr")), load=int(hello.get("load") or 0),
                      # gap between its clock and ours (watch together: positions dated by its clock)
                      clock=int(hello["now_ms"] - (w0 + w1) / 2) if isinstance(hello.get("now_ms"), (int, float)) else 0)
            self.set_online(pid, True)
            self.pull_users(peer)
            self.pull_catalog(peer)
            self.pull_progress(peer)
            for lid, uid, ver in self.avatar_todo.pop(pid, []):
                self.fetch_avatar(peer, lid, uid, ver)
            with self.lock:
                self.status.setdefault(pid, {}).update(st)
            self.kick_metadata()
            try:
                self.fetch_groups(peer)
            except FedError as e:
                log.debug("Groupes du serveur lié %s : %s", pid, e)
            db.run("UPDATE peers SET last_seen=?, version=?, name=?, error=NULL WHERE id=?",
                   (int(time.time()), str(hello.get("version") or "")[:20], str(hello.get("name") or peer["name"])[:40], pid))
        except (FedError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as e:
            fails = self.status.get(pid, {}).get("fails", 0) + 1
            st.update(online=False, fails=fails)
            if fails >= 2:  # two failures in a row (about thirty seconds): its titles are hidden
                self.set_online(pid, False)
            log.info("Serveur lié %s : %s", pid, e)
            try:
                db.run("UPDATE peers SET error=? WHERE id=?", (str(e)[:300], pid))
            except Exception:
                pass
        finally:
            with self.lock:
                self.status.setdefault(pid, {}).update(st)
            db.release()
            lk.release()

    def _snapshot(self):
        """Consistent read (one transaction): the change number returned covers exactly what is sent."""
        c = self.ctx.db.conn()
        c.execute("BEGIN")
        return c

    # comptes ---------------------------------------------------------------
    # ------------------------------------------------------------------ account preferences
    # They follow the account on the linked servers. What points to a library (lib:N, hidden libraries,
    # pinned rows of a genre or of filters) goes through the libraries' shared id, the wallpaper
    # through the item's shared reference. Pinned rows of a collection or a playlist stay
    # specific to each server (they only exist on theirs).
    LIB_KEYS = ("home_order", "home_hidden", "nav_order", "nav_hidden")

    def prefs_out(self, user_id):
        raw = self.ctx.db.scalar("SELECT data FROM prefs WHERE user_id=?", (user_id,))
        if raw is None:
            return {}
        try:
            p = json.loads(raw)
        except ValueError:
            return {}
        uids = {str(r["id"]): r["uid"] for r in self.ctx.db.all("SELECT id, uid FROM libraries WHERE uid IS NOT NULL")}
        pins = [pin for pin in p.get("home_pins") or [] if pin.get("type") in ("genre", "filter") and uids.get(str(pin.get("lib")))]
        kept = {pin["id"] for pin in pins}

        def key(k):
            if k.startswith("lib:"):
                return f"lib@{uids[k[4:]]}" if k[4:] in uids else None
            if k.startswith("pin:"):
                return k if k[4:].isdigit() and int(k[4:]) in kept else None
            return k
        out = dict(p)
        out["hidden_libraries"] = [uids[i] for i in p.get("hidden_libraries") or [] if i in uids]
        out["lib_langs"] = {uids[k]: v for k, v in (p.get("lib_langs") or {}).items() if k in uids}
        out["home_pins"] = [dict(pin, lib=uids[str(pin["lib"])]) for pin in pins]
        for k in self.LIB_KEYS:
            if k in p:
                out[k] = [v for v in (key(x) for x in p[k]) if v]
        if "start_page" in p:
            out["start_page"] = key(p["start_page"]) or "home"
        wp = p.get("wallpaper") or ""
        m = re.match(r"^/library/metadata/(\d{1,12})/art/(\d{1,14})$", wp)
        out["wallpaper"] = {"ref": self.ref_of(int(m.group(1))), "v": m.group(2)} if m else wp
        if "muted_shows" in p:  # shows you no longer want to hear about: named for all the servers
            out["muted_shows"] = [r for r in (self.ref_of(int(i)) for i in p["muted_shows"] if str(i).isdigit()) if r]
        return {"_v": 1, **out}  # present even when empty: going back to the default settings spreads too

    def prefs_in(self, user_id, incoming):
        """Preferences received from a linked server, translated for this server, then checked one by one."""
        from ..catalog import validate_prefs
        db = self.ctx.db
        if not isinstance(incoming, dict):
            return
        local = {}
        raw = db.scalar("SELECT data FROM prefs WHERE user_id=?", (user_id,))
        if raw:
            try:
                local = json.loads(raw)
            except ValueError:
                local = {}
        ids = {}
        for r in db.all("SELECT id, uid, merge_into FROM libraries WHERE uid IS NOT NULL"):
            ids[r["uid"]] = str(r["merge_into"] or r["id"])  # merged library: the one shown here
        p = {k: v for k, v in incoming.items() if k != "_v"}
        mine = [pin for pin in local.get("home_pins") or [] if pin.get("type") in ("collection", "playlist")]
        used = {pin["id"] for pin in mine}
        renum, pins = {}, []
        for pin in incoming.get("home_pins") or []:
            if not isinstance(pin, dict) or pin.get("lib") not in ids:
                continue
            new = pin.get("id")
            if not isinstance(new, int) or new in used:
                new = max(used | {0}) + 1
            renum[pin.get("id")] = new
            used.add(new)
            pins.append(dict(pin, id=new, lib=ids[pin["lib"]]))
        p["home_pins"] = mine + pins

        def key(k):
            if not isinstance(k, str):
                return None
            if k.startswith("lib@"):
                return f"lib:{ids[k[4:]]}" if k[4:] in ids else None
            if k.startswith("pin:"):
                n = int(k[4:]) if k[4:].isdigit() else None
                return f"pin:{renum[n]}" if n in renum else None
            return k
        p["hidden_libraries"] = [ids[u] for u in incoming.get("hidden_libraries") or [] if u in ids]
        if isinstance(incoming.get("lib_langs"), dict):
            p["lib_langs"] = {ids[u]: v for u, v in incoming["lib_langs"].items() if u in ids}
        for k in self.LIB_KEYS:
            if isinstance(incoming.get(k), list):
                lst = [v for v in (key(x) for x in incoming[k]) if v]
                # rows specific to this server (pinned collections, playlists): kept in their place
                lst += [x for x in local.get(k) or [] if x.startswith("pin:") and int(x[4:]) in {q["id"] for q in mine}
                        and x not in lst]
                p[k] = list(dict.fromkeys(lst))
        if "start_page" in incoming:
            p["start_page"] = key(incoming["start_page"]) or "home"
        if isinstance(incoming.get("muted_shows"), list):
            p["muted_shows"] = list(dict.fromkeys(str(i) for i in (self.resolve_ref(r) for r in incoming["muted_shows"][:500]) if i))
        wp = incoming.get("wallpaper")
        if isinstance(wp, dict):
            iid = self.resolve_ref(wp.get("ref"))
            p["wallpaper"] = f"/library/metadata/{iid}/art/{int(wp.get('v') or 1)}" if iid else ""
        clean = {}
        for k, v in p.items():
            try:
                clean.update(validate_prefs({k: v}))
            except HTTPError:
                continue  # setting unknown here (other version) or no longer valid: ours is kept
        if not clean.get("home_pins"):
            clean.pop("home_pins", None)
        db.run("INSERT INTO prefs(user_id,data) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
               (user_id, json.dumps(clean)))

    def user_payload(self, u):
        db = self.ctx.db
        d = {f: u[f] for f in USER_FIELDS}
        d.update(uid=u["uid"], rev=u["rev"] or 0, created_at=u["created_at"])
        d["prefs"] = self.prefs_out(u["id"])
        libs = [r["uid"] for r in db.all("SELECT l.uid FROM user_libraries ul JOIN libraries l ON l.id=ul.section_id "
                                         "WHERE ul.user_id=?", (u["id"],))]
        libs += [r["uid"] for r in db.all("SELECT uid FROM fed_pending_libs WHERE user_id=?", (u["id"],))]
        d["libraries"] = sorted(set(libs))
        return d

    def users_payload(self):
        return [self.user_payload(u) for u in self.ctx.db.all("SELECT * FROM users")]

    def users_feed(self, since):
        self._backfill("users")
        db = self.ctx.db
        c = self._snapshot()
        try:
            until = db.scalar("SELECT n FROM fed_clock")
            rows = db.all("SELECT * FROM users WHERE fseq>? OR ?=0", (since, since))
            gone = [r["ref"] for r in db.all("SELECT ref FROM fed_gone WHERE kind='user' AND seq>?", (since,))]
            users = [self.user_payload(u) for u in rows]
        finally:
            c.execute("COMMIT")
        return {"users": users, "gone": gone, "until": until}

    def pull_users(self, peer):
        data = self.call_ok(peer, "GET", f"/fed/users?since={int(peer['users_cursor'])}")
        todo = self.apply_users(peer, data.get("users") or [], data.get("gone") or [])
        self.ctx.db.run("UPDATE peers SET users_cursor=? WHERE id=?", (int(data["until"]), peer["id"]))
        for lid, uid, ver in todo:
            self.fetch_avatar(peer, lid, uid, ver)

    def apply_users(self, peer, users, gone, mode="lww"):
        """Accounts received from a linked server. mode: "lww" (the most recent wins), "keep" (a server joins:
        our accounts keep their data), "adopt" (we join: accounts with the same username take
        those of the server joined). -> avatars to fetch [(local id, uid, version)]."""
        db = self.ctx.db
        avatars, logout = [], []
        with db.tx():
            db.run("UPDATE fed_clock SET applying=1")
            try:
                for u in users:
                    if not (isinstance(u, dict) and HEX32.match(str(u.get("uid"))) and isinstance(u.get("username"), str)
                            and USERNAME.match(u["username"]) and isinstance(u.get("password_hash"), str)):
                        continue
                    if db.scalar("SELECT 1 FROM fed_gone WHERE kind='user' AND ref=?", (u["uid"],)):
                        continue  # deleted here: the deletion wins
                    rev = int(u.get("rev") or 0)
                    local = db.one("SELECT * FROM users WHERE uid=?", (u["uid"],))
                    force = adopted = renamed_in = False
                    if not local:
                        same = find_user(db, u["username"])
                        if same and mode == "keep":
                            continue  # joining: the other server already renamed or chose to merge
                        if same and mode == "lww":
                            # created on both sides before the synchronisation: two people, two accounts. Both
                            # servers rename the same one (the highest uid); the change goes back to the other.
                            # new name derived from the renamed account: the same choice on both servers
                            loser = same["username"] if same["uid"] > u["uid"] else u["username"]
                            new = free_username(db, loser)
                            if same["uid"] > u["uid"]:
                                db.run("UPDATE users SET username=? WHERE id=?", (new, same["id"]))
                                self.notice(same["username"], new)
                            else:
                                u = dict(u, username=new)
                                renamed_in = True
                                self.notice(same["username"], new)
                            same = None
                        if same:  # joining, same person according to the administrator: the data of the server joined
                            db.run("UPDATE users SET uid=? WHERE id=?", (u["uid"], same["id"]))
                            local = dict(same, uid=u["uid"])
                            force, adopted = True, True
                    if local and not force and (local["rev"] or 0) >= rev:
                        if adopted:
                            # our version is the most recent: it goes back to the other server under the shared uid,
                            # otherwise each would keep its own (two passwords for the same account)
                            db.run("UPDATE users SET rev=rev WHERE id=?", (local["id"],))
                        continue
                    fields = {f: u.get(f) for f in USER_FIELDS}
                    for f in ("is_admin", "all_libraries", "disabled", "managed", "home", "allow_unrated", "kids",
                              "totp_enabled"):
                        fields[f] = 1 if u.get(f, f in ("all_libraries", "home", "allow_unrated")) else 0
                    fields["display_name"] = str(fields["display_name"] or u["username"])[:50]
                    fields["avatar_color"] = str(fields["avatar_color"] or "#e0a84f")[:7]
                    fields["language"] = i18n.valid(fields["language"]) or None
                    if fields["max_age"] is not None and not isinstance(fields["max_age"], int):
                        fields["max_age"] = None
                    for f in LIMIT_FIELDS:
                        if f not in u:
                            fields.pop(f)  # server of an older version: our limits stay
                        elif f == "bedtime":
                            fields[f] = u[f] if isinstance(u[f], str) and BEDTIME.match(u[f]) else None
                        elif not isinstance(u[f], int) or isinstance(u[f], bool) or u[f] <= 0:
                            fields[f] = None
                    if local:
                        if mode == "adopt" and local["is_admin"]:
                            fields["is_admin"] = 1  # linking a server does not lose its administration
                        other = find_user(db, fields["username"])
                        if other and other["id"] != local["id"]:
                            continue  # username taken by another account here: settled at the next change
                        sets = ",".join(f"{k}=?" for k in fields)
                        db.run(f"UPDATE users SET {sets}, rev=? WHERE id=?", [*fields.values(), rev, local["id"]])
                        lid = local["id"]
                        if mode == "lww" and (fields["password_hash"] != local["password_hash"] or fields["disabled"]):
                            logout.append(lid)
                        old_avatar = local["avatar_ver"]
                    else:
                        cols = ["uid", "rev", "created_at", *fields]
                        lid = db.run(f"INSERT INTO users({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                                     [u["uid"], rev, int(u.get("created_at") or time.time()), *fields.values()]).lastrowid
                        if renamed_in:
                            db.run("UPDATE users SET rev=rev WHERE id=?", (lid,))  # the new name goes back to the other
                        old_avatar = None
                    db.run("DELETE FROM user_libraries WHERE user_id=?", (lid,))
                    db.run("DELETE FROM fed_pending_libs WHERE user_id=?", (lid,))
                    for luid in u.get("libraries") or []:
                        if not isinstance(luid, str) or not HEX32.match(luid):
                            continue
                        lib = db.scalar("SELECT id FROM libraries WHERE uid=?", (luid,))
                        if lib:
                            db.run("INSERT OR IGNORE INTO user_libraries(user_id,section_id) VALUES(?,?)", (lid, str(lib)))
                        else:  # library not received yet (catalogue of a linked server): set when it arrives
                            db.run("INSERT OR IGNORE INTO fed_pending_libs(user_id,uid) VALUES(?,?)", (lid, luid))
                    if isinstance(u.get("prefs"), dict) and u["prefs"]:
                        self.prefs_in(lid, u["prefs"])  # theme, languages, home…: the same on each server
                    if fields["avatar_ver"] != old_avatar or (fields["avatar_ver"] and not self.has_avatar(lid)):
                        avatars.append((lid, u["uid"], fields["avatar_ver"]))
                for uid in gone:
                    row = db.one("SELECT id FROM users WHERE uid=?", (str(uid),))
                    if row:
                        db.run("DELETE FROM users WHERE id=?", (row["id"],))
                        self.remove_avatar_file(row["id"])
                for lid in logout:
                    db.run("DELETE FROM sessions WHERE user_id=?", (lid,))
            finally:
                db.run("UPDATE fed_clock SET applying=0")
        return avatars

    def notice(self, old, new):
        """Account renamed to avoid a duplicate username: shown in Linked servers."""
        log.warning("Serveurs liés : identifiant %s créé sur deux serveurs, l'un des comptes devient %s", old, new)
        db = self.ctx.db
        try:
            items = json.loads(db.setting("fed_notices") or "[]")
        except ValueError:
            items = []
        items = [*items, {"username": old, "renamed": new, "at": int(time.time())}][-20:]
        db.set_setting("fed_notices", json.dumps(items))

    def avatar_path(self, lid):
        return os.path.join(self.ctx.data_dir, "avatars", f"{int(lid)}.jpg")

    def has_avatar(self, lid):
        return os.path.isfile(self.avatar_path(lid))

    def remove_avatar_file(self, lid):
        try:
            os.remove(self.avatar_path(lid))
        except OSError:
            pass

    def fetch_avatar(self, peer, lid, uid, ver):
        if not ver:
            self.remove_avatar_file(lid)
            return
        try:
            resp, close = self.request(peer, "GET", f"/fed/avatar/{uid}", timeout=20)
            try:
                data = resp.read(2 * 1024 * 1024) if resp.status == 200 else None
            finally:
                close()
        except (FedError, OSError, http.client.HTTPException):
            return
        if data:
            os.makedirs(os.path.dirname(self.avatar_path(lid)), exist_ok=True)
            tmp = self.avatar_path(lid) + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, self.avatar_path(lid))

    # catalogue -------------------------------------------------------------
    def _backfill(self, table):
        """Change numbers of the rows changed while no server was linked (shows before seasons
        before episodes: an item always arrives after its parent)."""
        db = self.ctx.db
        if table == "items":
            where, order, key = "peer_id IS NULL AND fseq IS NULL", \
                "CASE type WHEN 'show' THEN 0 WHEN 'season' THEN 1 ELSE 2 END, id", "id"
        else:  # comptes, progression
            where, order, key = "fseq IS NULL", "rowid", "rowid"
        if not db.scalar(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1"):
            return
        with db.tx():
            n = db.scalar("SELECT n FROM fed_clock")
            ids = [r["k"] for r in db.all(f"SELECT {key} AS k FROM {table} WHERE {where} ORDER BY {order}")]
            db.conn().executemany(f"UPDATE {table} SET fseq=? WHERE {key}=?", [(n + i + 1, k) for i, k in enumerate(ids)])
            db.run("UPDATE fed_clock SET n=?", (n + len(ids),))

    def catalog_feed(self, since, limit):
        self._backfill("items")
        db = self.ctx.db
        c = self._snapshot()
        try:
            clock = db.scalar("SELECT n FROM fed_clock")
            # photo libraries: not shared between servers (personal photos, and nothing to merge)
            rows = db.all("SELECT * FROM items WHERE peer_id IS NULL AND fseq>? AND type NOT IN ('photo','album','book') "
                          "ORDER BY fseq LIMIT ?", (since, limit))
            more = len(rows) == limit
            until = rows[-1]["fseq"] if more else clock
            gone = [int(r["ref"]) for r in db.all("SELECT ref FROM fed_gone WHERE kind='item' AND seq>? AND seq<=?",
                                                  (since, until))]
            libs = db.all("SELECT id, uid, name, kind, position FROM libraries WHERE peer_id IS NULL AND kind NOT IN ('photo','audiobook')")
        finally:
            c.execute("COMMIT")
        items = [{k: v for k, v in r.items() if k not in ITEM_LOCAL or k in ("id", "library_id", "parent_id", "show_id")}
                 for r in rows]
        return {"libraries": libs, "items": items, "gone": gone, "until": until, "more": more}

    def item_columns(self):
        if self._columns is None:
            self._columns = {r["name"] for r in self.ctx.db.all("PRAGMA table_info(items)")} - ITEM_LOCAL
        return self._columns

    def local_item(self, pid, remote):
        if not remote:
            return None
        return self.ctx.db.scalar("SELECT id FROM items WHERE peer_id=? AND remote_id=?", (pid, int(remote)))

    def pull_catalog(self, peer):
        cursor = int(peer["catalog_cursor"])
        for _ in range(200):
            data = self.call_ok(peer, "GET", f"/fed/catalog?since={cursor}&limit={CATALOG_PAGE}", timeout=120)
            self.apply_catalog(peer, data)
            cursor = int(data["until"])
            self.ctx.db.run("UPDATE peers SET catalog_cursor=? WHERE id=?", (cursor, peer["id"]))
            self.ctx.db.release()
            if not data.get("more"):
                break

    def apply_catalog(self, peer, data):
        db = self.ctx.db
        pid = peer["id"]
        cols = self.item_columns()
        orphans = self.orphans.setdefault(pid, {})
        with db.tx():
            seen = set()
            for l in data.get("libraries") or []:
                rid = int(l["id"])
                seen.add(rid)
                kind = l.get("kind") if l.get("kind") in ("movie", "show") else "movie"
                name = str(l.get("name") or "?")[:40]
                luid = str(l.get("uid") or "")
                if not HEX32.match(luid):
                    continue
                cur = db.scalar("SELECT id FROM libraries WHERE peer_id=? AND remote_id=?", (pid, rid))
                if cur:
                    db.run("UPDATE libraries SET name=?, kind=?, position=?, uid=? WHERE id=?",
                           (name, kind, 1000 + int(l.get("position") or 0), luid, cur))
                else:
                    lid = db.run("INSERT INTO libraries(name,kind,paths,position,created_at,uid,peer_id,remote_id) "
                                 "VALUES(?,?,'[]',?,?,?,?,?)",
                                 (name, kind, 1000 + int(l.get("position") or 0), int(time.time()), luid, pid, rid)).lastrowid
                    # accounts that already had access to this library (received before it)
                    db.run("UPDATE fed_clock SET applying=1")
                    db.run("INSERT OR IGNORE INTO user_libraries(user_id,section_id) SELECT user_id, ? FROM fed_pending_libs "
                           "WHERE uid=?", (str(lid), luid))
                    db.run("DELETE FROM fed_pending_libs WHERE uid=?", (luid,))
                    db.run("UPDATE fed_clock SET applying=0")
            if data.get("libraries") is not None:
                for r in db.all("SELECT id, remote_id FROM libraries WHERE peer_id=?", (pid,)):
                    if r["remote_id"] not in seen:
                        db.run("DELETE FROM items WHERE src_lib=?", (r["id"],))  # including those placed elsewhere (merge)
                        db.run("DELETE FROM libraries WHERE id=?", (r["id"],))
            libmap = {r["remote_id"]: r["id"] for r in db.all("SELECT id, remote_id FROM libraries WHERE peer_id=?", (pid,))}
            for it in data.get("items") or []:
                rid = int(it["id"])
                lib = libmap.get(int(it.get("library_id") or 0))
                if not lib:
                    continue
                vals = {k: v for k, v in it.items() if k in cols}
                parent, show = self.local_item(pid, it.get("parent_id")), self.local_item(pid, it.get("show_id"))
                # original place; the place shown (merge, server off) is decided by relink()
                vals.update(src_lib=lib, src_parent=parent, src_show=show)
                if vals.get("meta_state") == "pending":
                    vals["meta_state"] = "done"  # metadata is looked up on the origin server
                cur = self.local_item(pid, rid)
                if cur:
                    sets = ",".join(f"{k}=?" for k in vals)
                    db.run(f"UPDATE items SET {sets} WHERE id=?", [*vals.values(), cur])
                else:
                    vals.update(peer_id=pid, remote_id=rid, library_id=lib, parent_id=parent, show_id=show)
                    for k, default in (("title", "?"), ("sort_title", "?"), ("search", ""), ("type", "movie"),
                                       ("added_at", int(time.time())), ("updated_at", int(time.time()))):
                        if vals.get(k) is None:
                            vals[k] = default
                    cur = db.run(f"INSERT INTO items({','.join(vals)}) VALUES({','.join('?' * len(vals))})",
                                 list(vals.values())).lastrowid
                if (it.get("parent_id") and not parent) or (it.get("show_id") and not show):
                    orphans[cur] = (it.get("parent_id"), it.get("show_id"))
            gone = [int(g) for g in data.get("gone") or []]
            for i in range(0, len(gone), 500):
                chunk = gone[i:i + 500]
                db.run(f"DELETE FROM items WHERE peer_id=? AND remote_id IN ({','.join('?' * len(chunk))})", [pid, *chunk])
            for lid, (rp, rs) in list(orphans.items()):
                parent, show = self.local_item(pid, rp), self.local_item(pid, rs)
                if (parent or not rp) and (show or not rs):
                    db.run("UPDATE items SET src_parent=?, src_show=? WHERE id=?", (parent, show, lid))
                    orphans.pop(lid)
        self.relink(pid)

    # fusion ----------------------------------------------------------------
    def relink_all(self):
        if not self.has_peers():
            return
        for p in self.ctx.db.all("SELECT id FROM peers"):
            self.relink(p["id"])

    def relink(self, pid):
        """Place shown for the items received from a linked server.

        - Library not merged: as is (one more library).
        - Merged with a library of this server: a title we also have (same TMDB, IMDb or
          TVDB id, or same title and year) is hidden and becomes a second copy of ours (alias_of); a season
          or an episode we lack goes under our show. The rest joins our library.
        - Server unreachable: everything goes back to its library, hidden; our titles stay.
        """
        db = self.ctx.db
        peer = db.one("SELECT online FROM peers WHERE id=?", (pid,))
        if not peer:
            return
        changes = []
        for lib in db.all("SELECT l.id, l.kind, l.merge_into, t.kind AS target_kind FROM libraries l "
                          "LEFT JOIN libraries t ON t.id=l.merge_into AND t.peer_id IS NULL WHERE l.peer_id=?", (pid,)):
            rows = db.all("SELECT id, type, idx, title, year, tmdb_id, imdb_id, tvdb_id, library_id, parent_id, show_id, "
                          "alias_of, src_parent, src_show FROM items WHERE src_lib=?", (lib["id"],))
            target = lib["merge_into"] if peer["online"] and lib["target_kind"] == lib["kind"] else None
            alias = {}
            if target:
                index = {}
                for r in db.all("SELECT id, type, title, year, tmdb_id, imdb_id, tvdb_id FROM items WHERE library_id=? "
                                "AND peer_id IS NULL AND type IN ('movie','show')", (target,)):
                    for key in self._match_keys(r):
                        index.setdefault(key, r)
                for r in rows:
                    if r["type"] not in ("movie", "show"):
                        continue
                    for key in self._match_keys(r):
                        hit = index.get(key)
                        # same title and year, but two different movies at TMDB (remake): not the same
                        if hit and not (key[1] == "title" and r["tmdb_id"] and hit["tmdb_id"]
                                        and r["tmdb_id"] != hit["tmdb_id"]):
                            alias[r["id"]] = hit["id"]
                            break
                for kind in ("season", "episode"):
                    for r in rows:
                        if r["type"] != kind or r["src_parent"] not in alias or r["idx"] is None:
                            continue
                        hit = db.scalar("SELECT id FROM items WHERE parent_id=? AND type=? AND idx=? AND peer_id IS NULL "
                                        "ORDER BY id LIMIT 1", (alias[r["src_parent"]], kind, r["idx"]))
                        if hit:
                            alias[r["id"]] = hit
            for r in rows:
                if not peer["online"] or not target:
                    want = (lib["id"], r["src_parent"], r["src_show"], None)
                elif r["id"] in alias:
                    want = (lib["id"], r["src_parent"], r["src_show"], alias[r["id"]])
                else:
                    want = (target, alias.get(r["src_parent"], r["src_parent"]), alias.get(r["src_show"], r["src_show"]), None)
                if want != (r["library_id"], r["parent_id"], r["show_id"], r["alias_of"]):
                    changes.append((*want, r["id"]))
        if changes:
            with db.tx():
                db.conn().executemany("UPDATE items SET library_id=?, parent_id=?, show_id=?, alias_of=? WHERE id=?", changes)

    @staticmethod
    def _match_keys(r):
        keys = []
        for col in ("tmdb_id", "imdb_id", "tvdb_id"):
            if r[col]:
                keys.append((r["type"], col, str(r[col])))
        if r["title"]:
            keys.append((r["type"], "title", fold(r["title"]), r["year"]))
        return keys

    def set_online(self, pid, online):
        db = self.ctx.db
        cur = db.scalar("SELECT online FROM peers WHERE id=?", (pid,))
        if cur is not None and bool(cur) != online:
            db.run("UPDATE peers SET online=? WHERE id=?", (1 if online else 0, pid))
            log.warning("Serveur lié %s %s", pid, "de nouveau joignable" if online else "injoignable : ses titres sont masqués")
            if online and getattr(self.ctx, "images", None):
                self.ctx.images.forget_missing()  # its pictures are asked for again
            self.relink(pid)

    # progression -----------------------------------------------------------
    def progress_feed(self, since, limit):
        self._backfill("progress")
        db = self.ctx.db
        c = self._snapshot()
        try:
            clock = db.scalar("SELECT n FROM fed_clock")
            rows = db.all("SELECT p.rating_key, p.position_ms, p.duration_ms, p.watched, p.view_count, p.updated_at, "
                          "p.last_watched_at, p.fseq, u.uid AS user FROM progress p JOIN users u ON u.id=p.user_id "
                          "JOIN items i ON i.id=p.rating_key WHERE i.peer_id IS NULL AND p.fseq>? ORDER BY p.fseq LIMIT ?",
                          (since, limit))
        finally:
            c.execute("COMMIT")
        more = len(rows) == limit
        return {"rows": rows, "until": rows[-1]["fseq"] if more else clock, "more": more}

    def pull_progress(self, peer):
        cursor = int(peer["progress_cursor"])
        for _ in range(100):
            data = self.call_ok(peer, "GET", f"/fed/progress?since={cursor}&limit={PROGRESS_PAGE}", timeout=60)
            self.apply_progress(peer, data.get("rows") or [])
            cursor = int(data["until"])
            self.ctx.db.run("UPDATE peers SET progress_cursor=? WHERE id=?", (cursor, peer["id"]))
            self.ctx.db.release()
            if not data.get("more"):
                break

    def apply_progress(self, peer, rows):
        db, x = self.ctx.db, self.x
        users = {}
        with db.tx():
            for r in rows:
                if r["user"] not in users:
                    users[r["user"]] = db.scalar("SELECT id FROM users WHERE uid=?", (str(r["user"]),))
                uid = users[r["user"]]
                item = self.local_item(peer["id"], r["rating_key"])
                if item:  # title merged with one of ours: the progress goes to ours
                    item = db.scalar("SELECT COALESCE(alias_of, id) FROM items WHERE id=?", (item,))
                if not uid or not item:
                    continue
                rk = str(item)
                cur = db.one("SELECT updated_at FROM progress WHERE user_id=? AND rating_key=?", (uid, rk))
                if cur and cur["updated_at"] > int(r["updated_at"] or 0):
                    continue  # more recent here (watched from this server)
                if not cur:
                    row = db.one(CARD_SQL + "WHERE i.id=?", (item,))
                    x.upsert_meta(uid, x.card(row))
                db.run("UPDATE progress SET position_ms=?, duration_ms=?, watched=?, view_count=?, updated_at=?, "
                       "last_watched_at=? WHERE user_id=? AND rating_key=?",
                       (int(r["position_ms"] or 0), int(r["duration_ms"] or 0), 1 if r["watched"] else 0,
                        int(r["view_count"] or 0), int(r["updated_at"] or 0), r["last_watched_at"], uid, rk))
