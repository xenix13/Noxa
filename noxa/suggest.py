"""Local suggestions, without an outside service: from what the user watched recently
(genres, directors, actors), their libraries are searched for the movies and shows they have not seen yet."""
import json
import math
import threading
import time

from .catalog import CARD_SQL, MIN_RESUME_MS, RK
from .web import HTTPError

RECENT_DAYS = 90         # window of "what I watched recently"
HALF_LIFE_DAYS = 21      # a viewing from 3 weeks ago counts half as much
LIKE_WEIGHT = 1.5        # a "like" counts as a strong recent viewing
DISLIKE_WEIGHT = 0.8     # a "dislike" pushes away the titles that look like it
CACHE_SECONDS = 300
ROW_SIZE = 24
MIN_ROW = 4              # below this, the row is not shown


def _names(people, key, limit):
    out = []
    for p in (people.get(key) or [])[:limit]:
        name = p.get("name") if isinstance(p, dict) else p
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


def features(row):
    """Genres and people of a movie or a show."""
    try:
        genres = [g for g in json.loads(row["genres"] or "[]") if isinstance(g, str)]
    except (TypeError, ValueError):
        genres = []
    try:
        people = json.loads(row["people"] or "{}")
        if not isinstance(people, dict):
            people = {}
    except (TypeError, ValueError):
        people = {}
    return set(genres), set(_names(people, "directors", 3)), set(_names(people, "cast", 8))


def similarity(a, b):
    """Closeness between two titles: genres in common, same director, actors in common."""
    ga, da, ca = a
    gb, db_, cb = b
    score = 0.0
    if ga and gb:
        score += 3 * len(ga & gb) / len(ga | gb)
    score += 2 * len(da & db_)
    score += 0.8 * min(len(ca & cb), 4)
    return score


def register(app, ctx, x):
    db = ctx.db
    age_filter, allowed_set, attach_state, card, visible = x.age_filter, x.allowed_set, x.attach_state, x.card, x.visible
    cache, lock = {}, threading.Lock()

    def seen_titles(uid):
        """Movies watched or started, shows with at least one episode watched or started: never suggested."""
        ids = set()
        for r in db.all("SELECT rating_key, type, show_key FROM progress WHERE user_id=? "
                        "AND (watched=1 OR position_ms>=? OR view_count>0)", (uid, MIN_RESUME_MS)):
            if r["show_key"]:
                ids.add(int(r["show_key"]))
            elif str(r["rating_key"]).isdigit():
                ids.add(int(r["rating_key"]))
        for r in db.all("SELECT h.rating_key FROM history h WHERE h.user_id=?", (uid,)):
            if str(r["rating_key"]).isdigit():
                ids.add(int(r["rating_key"]))
        if ids:  # episodes in the history -> their show
            for chunk in _chunks(sorted(ids), 900):
                marks = ",".join("?" * len(chunk))
                for r in db.all(f"SELECT show_id FROM items WHERE id IN ({marks}) AND show_id IS NOT NULL", chunk):
                    ids.add(r["show_id"])
        return ids

    def seeds(uid):
        """Titles watched recently (movie, or show of the episode), with a weight that drops over time."""
        since = int(time.time()) - RECENT_DAYS * 86400
        events = db.all("SELECT rating_key, watched_at AS t FROM history WHERE user_id=? AND watched_at>? "
                        "ORDER BY watched_at DESC LIMIT 400", (uid, since))
        events += db.all("SELECT rating_key, updated_at AS t FROM progress WHERE user_id=? AND updated_at>? "
                         "AND position_ms>=? AND watched=0 ORDER BY updated_at DESC LIMIT 100", (uid, since, MIN_RESUME_MS))
        keys = sorted({int(e["rating_key"]) for e in events if str(e["rating_key"]).isdigit()})
        to_title = {}
        for chunk in _chunks(keys, 900):
            marks = ",".join("?" * len(chunk))
            for r in db.all(f"SELECT id, type, show_id FROM items WHERE id IN ({marks})", chunk):
                if r["type"] == "movie":
                    to_title[r["id"]] = r["id"]
                elif r["type"] == "episode" and r["show_id"]:
                    to_title[r["id"]] = r["show_id"]
        now = time.time()
        weight, last = {}, {}
        for e in events:
            tid = to_title.get(int(e["rating_key"])) if str(e["rating_key"]).isdigit() else None
            if not tid:
                continue
            w = 0.5 ** ((now - e["t"]) / 86400 / HALF_LIFE_DAYS)
            weight[tid] = weight.get(tid, 0) + w
            last[tid] = max(last.get(tid, 0), e["t"])
        # a show binge-watched must not crush everything else
        weight = {k: min(v, 2.5) for k, v in weight.items()}
        return weight, last

    def opinions(uid):
        """{title: opinion}; opinion 1 = like, -1 = dislike, 0 = do not suggest anymore."""
        return {r["item_id"]: r["value"] for r in db.all("SELECT item_id, value FROM ratings WHERE user_id=?", (uid,))}

    def candidates(user, exclude):
        allowed = sorted(int(i) for i in allowed_set(user))
        if not allowed:
            return []
        af, aa = age_filter(user)
        marks = ",".join("?" * len(allowed))
        rows = db.all("SELECT i.id, i.type, i.genres, i.people, i.rating, i.added_at, COALESCE(sh.age, i.age) AS eff_age "
                      "FROM items i LEFT JOIN items sh ON sh.id=i.show_id "
                      f"WHERE i.type IN ('movie','show') AND i.library_id IN ({marks})" + af,
                      [*allowed, *aa])
        return [r for r in rows if r["id"] not in exclude]

    def cards(user, ids):
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = {r["id"]: r for r in db.all(CARD_SQL + f"WHERE i.id IN ({marks})", ids)}
        out = [card(rows[i]) for i in ids if i in rows and visible(user, rows[i])]
        return attach_state(user["id"], out)

    def compute(user):
        uid = user["id"]
        weight, last = seeds(uid)
        ops = opinions(uid)
        for tid, v in ops.items():
            if v == 1:
                weight[tid] = max(weight.get(tid, 0), LIKE_WEIGHT)
        dislikes = [tid for tid, v in ops.items() if v == -1]
        if not weight:
            return {"suggestions": [], "because": []}
        seed_ids = sorted(weight)
        marks = ",".join("?" * len(seed_ids))
        seed_rows = {r["id"]: r for r in db.all(f"SELECT id, title, genres, people FROM items WHERE id IN ({marks})", seed_ids)}
        seed_feat = {i: features(r) for i, r in seed_rows.items()}
        # profile: genres, directors and actors weighted by how recent the viewings are
        genre_w, dir_w, cast_w = {}, {}, {}
        for i, (g, d, c) in seed_feat.items():
            w = weight[i]
            for name in g:
                genre_w[name] = genre_w.get(name, 0) + w / math.sqrt(len(g))
            for name in d:
                dir_w[name] = dir_w.get(name, 0) + w
            for name in c:
                cast_w[name] = cast_w.get(name, 0) + w
        if dislikes:  # what looks like a disliked title moves back
            marks_d = ",".join("?" * len(dislikes))
            for r in db.all(f"SELECT id, genres, people FROM items WHERE id IN ({marks_d})", dislikes):
                g, d, c = features(r)
                for name in g:
                    genre_w[name] = genre_w.get(name, 0) - DISLIKE_WEIGHT / math.sqrt(len(g))
                for name in d:
                    dir_w[name] = dir_w.get(name, 0) - DISLIKE_WEIGHT
                for name in c:
                    cast_w[name] = cast_w.get(name, 0) - DISLIKE_WEIGHT / 2
        top = max(genre_w.values(), default=1) or 1
        if top <= 0:
            top = 1
        pool = candidates(user, seen_titles(uid) | set(weight) | set(ops))
        feats = {r["id"]: features(r) for r in pool}
        scored = []
        for r in pool:
            g, d, c = feats[r["id"]]
            s = sum(genre_w.get(n, 0) for n in g) / top / (math.sqrt(len(g)) if g else 1)
            s += 1.5 * sum(min(dir_w.get(n, 0), 2) for n in d)
            s += 0.4 * sum(min(cast_w.get(n, 0), 2) for n in c)
            if s <= 0:
                continue
            s += 0.03 * (r["rating"] or 0)  # same score: the best rated first
            scored.append((s, r["added_at"] or 0, r["id"]))
        scored.sort(reverse=True)
        suggestions = [i for _, _, i in scored[:ROW_SIZE]]

        # "Because you watched …": the two most recent titles that have enough neighbours
        # (what already leads the suggestions comes after, to vary what is seen)
        because, used, shown = [], set(), set(suggestions[:8])
        for sid in sorted(last, key=last.get, reverse=True)[:6]:
            if len(because) == 2 or sid not in seed_feat:
                continue
            near = sorted(((similarity(seed_feat[sid], feats[r["id"]]), r["id"]) for r in pool), reverse=True)
            ids = [i for s, i in near if s >= 1.2 and i not in used]
            ids = ([i for i in ids if i not in shown] + [i for i in ids if i in shown])[:ROW_SIZE]
            if len(ids) >= MIN_ROW:
                because.append((sid, ids))
                used.update(ids[:6])
        return {"suggestions": suggestions, "because": because}

    def suggestions(user):
        """"Suggestions" and "Because you watched…" rows of the home page."""
        stamp = (db.scalar("SELECT MAX(id) FROM history WHERE user_id=?", (user["id"],)),
                 db.scalar("SELECT MAX(updated_at) FROM progress WHERE user_id=? AND position_ms>=?", (user["id"], MIN_RESUME_MS)),
                 db.scalar("SELECT COUNT(*) FROM items WHERE type IN ('movie','show')"),
                 tuple(sorted(allowed_set(user))), user.get("max_age"),
                 db.scalar("SELECT MAX(updated_at) FROM ratings WHERE user_id=?", (user["id"],)))
        key = user["id"]
        with lock:
            hit = cache.get(key)
        if hit and hit[0] == stamp and time.time() - hit[1] < CACHE_SECONDS:
            raw = hit[2]
        else:
            raw = compute(user)
            with lock:
                cache[key] = (stamp, time.time(), raw)
        out = {"suggestions": cards(user, raw["suggestions"]) if len(raw["suggestions"]) >= MIN_ROW else [], "because": []}
        for sid, ids in raw["because"]:
            seed = cards(user, [sid])
            items = cards(user, ids)
            if seed and len(items) >= MIN_ROW:
                out["because"].append({"seed": seed[0], "items": items})
        return out

    def similar_to(user, row):
        """Titles close to a movie or a show (title page): no history needed."""
        if row["type"] in ("episode", "season") and row["show_id"]:
            row = db.one("SELECT * FROM items WHERE id=?", (row["show_id"],))
        if not row or row["type"] not in ("movie", "show"):
            return []
        base = features(row)
        hidden = {i for i, v in opinions(user["id"]).items() if v <= 0}
        pool = candidates(user, {row["id"]} | hidden)
        near = sorted(((similarity(base, features(r)), r["rating"] or 0, r["id"]) for r in pool), reverse=True)
        return cards(user, [i for s, _, i in near if s >= 1.2][:ROW_SIZE])

    x.suggestions = suggestions
    x.similar_to = similar_to

    def title_of(row):
        """Opinions are about a movie or a show (an episode points to its show)."""
        if row["type"] in ("episode", "season") and row["show_id"]:
            return row["show_id"]
        return row["id"] if row["type"] in ("movie", "show") else None

    def my_opinion(user, row):
        tid = title_of(row)
        return db.scalar("SELECT value FROM ratings WHERE user_id=? AND item_id=?", (user["id"], tid)) if tid else None
    x.my_opinion = my_opinion

    @app.route("PUT", f"/api/items/{RK}/opinion")
    def item_opinion(req):
        row = x.require_item(req.user, req.params["rk"])
        tid = title_of(row)
        if not tid:
            raise HTTPError(400, "Avis possible sur un film ou une série uniquement")
        v = req.json().get("value")
        if v is None:
            db.run("DELETE FROM ratings WHERE user_id=? AND item_id=?", (req.user["id"], tid))
        elif v in (1, -1, 0) and not isinstance(v, bool):
            db.run("INSERT INTO ratings(user_id,item_id,value,updated_at) VALUES(?,?,?,?) "
                   "ON CONFLICT(user_id,item_id) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                   (req.user["id"], tid, v, int(time.time())))
        else:
            raise HTTPError(400, "Avis invalide")
        with lock:
            cache.pop(req.user["id"], None)  # suggestions computed again with this new opinion
        return {"value": v}

    # personal ratings and hidden titles ------------------------------------------------------------
    def my_stars(user, row):
        tid = title_of(row)
        return db.scalar("SELECT stars FROM stars WHERE user_id=? AND item_id=?", (user["id"], tid)) if tid else None
    x.my_stars = my_stars

    def is_hidden(user, row):
        tid = title_of(row)
        return bool(tid and db.scalar("SELECT 1 FROM hidden_items WHERE user_id=? AND item_id=?", (user["id"], tid)))
    x.is_hidden = is_hidden

    def set_stars(uid, tid, v):
        """Rating from 1 to 5 (None: removed) of a movie or a show, and the opinion that follows from it."""
        now = int(time.time())
        if v is None:
            db.run("DELETE FROM stars WHERE user_id=? AND item_id=?", (uid, tid))
        else:
            db.run("INSERT INTO stars(user_id,item_id,stars,updated_at) VALUES(?,?,?,?) ON CONFLICT(user_id,item_id) "
                   "DO UPDATE SET stars=excluded.stars, updated_at=excluded.updated_at", (uid, tid, v, now))
            opinion = 1 if v >= 4 else -1 if v <= 2 else None
            if opinion is None:
                db.run("DELETE FROM ratings WHERE user_id=? AND item_id=? AND value<>0", (uid, tid))
            else:
                db.run("INSERT INTO ratings(user_id,item_id,value,updated_at) VALUES(?,?,?,?) ON CONFLICT(user_id,item_id) "
                       "DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (uid, tid, opinion, now))
        with lock:
            cache.pop(uid, None)
    x.set_stars = set_stars

    @app.route("PUT", f"/api/items/{RK}/stars")
    def item_stars(req):
        """Rating from 1 to 5 stars (null: removed). It also steers the suggestions: 4 or 5 = like,
        1 or 2 = dislike, 3 = no opinion."""
        row = x.require_item(req.user, req.params["rk"])
        tid = title_of(row)
        if not tid:
            raise HTTPError(400, "Avis possible sur un film ou une série uniquement")
        v = req.json().get("stars")
        if v is not None and not (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5):
            raise HTTPError(400, "Note invalide (1 à 5)")
        set_stars(req.user["id"], tid, v)
        return {"stars": v, "opinion": my_opinion(req.user, row)}

    @app.route("PUT", f"/api/items/{RK}/hidden")
    def item_hidden(req):
        """Hide a title for oneself: it disappears from one's lists (libraries, home, search, suggestions)."""
        row = x.require_item(req.user, req.params["rk"])
        tid = title_of(row)
        if not tid:
            raise HTTPError(400, "Seuls un film ou une série se masquent")
        if req.json().get("hidden"):
            db.run("INSERT OR IGNORE INTO hidden_items(user_id,item_id,added_at) VALUES(?,?,?)",
                   (req.user["id"], tid, int(time.time())))
        else:
            db.run("DELETE FROM hidden_items WHERE user_id=? AND item_id=?", (req.user["id"], tid))
        with lock:
            cache.pop(req.user["id"], None)
        return {"hidden": is_hidden(req.user, row)}

    @app.route("GET", "/api/me/hidden")
    def my_hidden(req):
        rows = db.all(CARD_SQL + "JOIN hidden_items hd ON hd.item_id=i.id WHERE hd.user_id=? ORDER BY hd.added_at DESC",
                      (req.user["id"],))
        return [card(r) for r in rows]

    @app.route("GET", f"/api/items/{RK}/similar")
    def item_similar(req):
        return {"items": similar_to(req.user, x.require_item(req.user, req.params["rk"]))}


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
