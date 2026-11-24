"""Detection of changes in the libraries.

On an SMB share, Linux is not told about files added from another machine
(inotify only sees local writes). So the modification dates of the FOLDERS are compared
regularly (adding, deleting or renaming a file changes them): this is
very light, even over thousands of folders. After a change, we wait until the folders
are stable (copy finished), then only the library concerned is scanned again.
"""
import json
import logging
import os
import threading
import time

log = logging.getLogger("noxa.watch")
SKIP = {"@eadir", "#recycle", "$recycle.bin", "lost+found", ".trickplay"}


MAX_PARTIAL = 500  # beyond this number of folders touched, a full scan is simpler


def snapshot(paths):
    """-> {folder: modification date} for all the folders of the libraries (files not read)."""
    snap = {}
    for root in paths:
        try:
            snap[root] = os.stat(root).st_mtime_ns
        except OSError:
            continue  # share not mounted: its folders "disappear" but the scanner deletes nothing
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if e.name.startswith(".") or e.name.lower() in SKIP:
                            continue
                        try:
                            if e.is_dir(follow_symlinks=True):
                                snap[e.path] = e.stat().st_mtime_ns
                                stack.append(e.path)
                        except OSError:
                            continue
            except OSError:
                continue
    return snap


def vanished_dirs(db, lib):
    """Indexed folders of this library that no longer exist (show or movie moved elsewhere).

    Safety net when the SOURCE library of a move saw no change (SMB
    cache, move done during an outage…): only the top-level folders are tested (one per
    show / movie), so a few hundred calls at most, and only after a detection elsewhere."""
    roots = [r for r in json.loads(lib["paths"]) if r and os.path.isdir(r)]  # share not mounted: nothing is touched
    if not roots:
        return set()
    by_top = {}
    for r in db.all("SELECT key FROM items WHERE library_id=? AND type IN ('movie','episode','extra') AND peer_id IS NULL",
                    (lib["id"],)):
        d = os.path.dirname(r["key"])
        for root in roots:
            if d.startswith(os.path.join(root, "")):
                top = os.path.join(root, os.path.relpath(d, root).split(os.sep)[0])
                by_top.setdefault(top, set()).add(d)
                break
    out = set()
    for top, dirs in by_top.items():
        if not os.path.isdir(top):
            out.add(top)
            out |= dirs
    return out


class Watcher:
    def __init__(self, ctx):
        self.ctx = ctx
        self.prints = {}      # library -> {folder: date}
        self.pending = {}     # library -> {"t": instant, "dirs": folders to check again, or None = everything}
        self.last_check = {}
        self.force_at = None  # webhook: scan requested
        self.force_scope = None  # {library: folders}; None = full scan
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.events = []      # latest triggers (shown in the admin)

    def interval(self):
        return self.ctx.setting_int("watch_interval", 60)

    def request_scan(self, reason="webhook", delay=10, scope=None):
        """scope: {library: folders} to check only these folders (otherwise full scan)."""
        with self.lock:
            full = self.force_at is not None and self.force_scope is None
            if scope and not full:
                merged = dict(self.force_scope or {})
                for lib, dirs in scope.items():
                    merged[lib] = set(merged.get(lib, set())) | set(dirs)
                self.force_scope = merged
            else:
                self.force_scope = None
            self.force_at = time.time() + delay
            what = ", ".join(os.path.basename(d) or d for dirs in (scope or {}).values() for d in sorted(dirs))[:200]
            self._event(f"Scan demandé ({reason})" + (f" : {what}" if scope else ""))

    def defer(self, library_id, dirs=None):
        """The scanner skipped files being copied: another pass will come (on these folders only)."""
        with self.lock:
            self._add_pending(library_id, dirs)

    def _add_pending(self, library_id, dirs):
        cur = self.pending.get(library_id)
        if cur is None:
            cur = {"t": time.time(), "dirs": set()}
            self.pending[library_id] = cur
        cur["t"] = time.time()
        if dirs is None or cur["dirs"] is None:
            cur["dirs"] = None
        else:
            cur["dirs"] |= set(dirs)
            if len(cur["dirs"]) > MAX_PARTIAL:
                cur["dirs"] = None

    def _event(self, text):
        self.events.insert(0, {"t": int(time.time()), "text": text})
        del self.events[20:]

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.stopped.set()

    def _loop(self):
        while not self.stopped.wait(5):
            try:
                self.tick()
            except Exception:
                log.exception("Surveillance des bibliothèques")
            finally:
                self.ctx.db.release()

    def tick(self, now=None):
        now = now or time.time()
        scanner = self.ctx.scanner
        if self.force_at and now >= self.force_at:
            with self.lock:
                ids, scope = None, None
                if self.force_scope:
                    # what the watcher had already noted for these libraries is added
                    ids, scope = list(self.force_scope), {}
                    for lib, dirs in self.force_scope.items():
                        p = self.pending.get(lib)
                        if p and p["dirs"] is None:
                            continue  # missing from scope = full scan of this library only
                        scope[lib] = set(dirs) | (p["dirs"] if p else set())
            ok = scanner.start(ids, scope=scope or None)
            if ok:
                with self.lock:
                    self.force_at = None
                    self.force_scope = None
                    for lib in (ids or list(self.pending)):
                        self.pending.pop(lib, None)
            return
        interval = self.interval()
        if interval <= 0:
            return
        libs = self.ctx.db.all("SELECT id, name, paths FROM libraries WHERE peer_id IS NULL")
        known = {l["id"] for l in libs}
        for k in list(self.prints):
            if k not in known:
                self.prints.pop(k, None)
                self.pending.pop(k, None)
        due = []
        detected = set()
        for lib in libs:
            if now - self.last_check.get(lib["id"], 0) < interval:
                continue
            self.last_check[lib["id"]] = now
            snap = snapshot(json.loads(lib["paths"]))
            old = self.prints.get(lib["id"])
            self.prints[lib["id"]] = snap
            if old is None:
                continue  # first pass: reference
            if snap != old:
                changed = {d for d, m in snap.items() if old.get(d) != m} | {d for d in old if d not in snap}
                with self.lock:
                    self._add_pending(lib["id"], changed)
                log.info("Changement détecté dans « %s » (%d dossier(s))", lib["name"], len(changed))
                detected.add(lib["id"])
                continue  # wait for a stable pass (copy in progress?)
            if lib["id"] in self.pending:
                due.append(lib)
        if detected:
            # a folder that arrived here may have been moved from another library
            for lib in libs:
                if lib["id"] in detected:
                    continue
                gone = vanished_dirs(self.ctx.db, lib)
                if gone:
                    log.info("« %s » : %d dossier(s) indexé(s) disparu(s)", lib["name"], len(gone))
                    with self.lock:
                        self._add_pending(lib["id"], gone)
        if due and not scanner.status()["running"]:
            ids = [l["id"] for l in due]
            with self.lock:
                scope = {l["id"]: set(self.pending[l["id"]]["dirs"]) for l in due
                         if self.pending.get(l["id"], {}).get("dirs") is not None}
            if scanner.start(ids, scope=scope or None):
                with self.lock:
                    for i in ids:
                        self.pending.pop(i, None)
                    parts = []
                    for l in due:
                        n = len(scope[l["id"]]) if l["id"] in scope else None
                        parts.append(f"{l['name']} ({n} dossier(s))" if n is not None else f"{l['name']} (complet)")
                    self._event("Nouveautés détectées : scan de " + ", ".join(parts))
