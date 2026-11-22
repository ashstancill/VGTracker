"""
VGTracker - a small self-hosted video game tracker.
Flask + SQLite. Syncs playtime from Steam and PlayStation Network, records how much
was played on which day so hours can be split by year, and pulls cover art, Metascores
and HowLongToBeat times.
"""
import csv
import datetime as dt
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import (Flask, abort, g, jsonify, redirect, request,
                   send_from_directory, session)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "vgtracker.db")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
os.makedirs(DATA_DIR, exist_ok=True)

log = logging.getLogger("vgtracker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024


def _secret_key():
    path = os.path.join(DATA_DIR, "secret.key")
    if not os.path.exists(path):
        try:
            with open(path, "w") as f:
                f.write(secrets.token_hex(32))
        except PermissionError:
            raise SystemExit(f"Can't write to {DATA_DIR}. The data folder must be owned by the user the container "
                             f"runs as (uid {os.getuid()}): run  chown -R {os.getuid()}:{os.getgid()} <your data folder>")
    with open(path) as f:
        return f.read().strip()


app.secret_key = _secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = dt.timedelta(days=90)
WIDGET_TOKEN = hashlib.sha256((app.secret_key + "widget").encode()).hexdigest()[:24]

STATUSES = ["playing", "completed", "planning", "paused", "dropped", "ignored", ""]
# Settings that hold credentials: never sent to the browser.
SECRET_KEYS = {"password_hash", "steam_key", "psn_npsso", "psn_refresh", "psn_refresh_exp", "rawg_key", "xbox_key"}

# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS games (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  norm TEXT DEFAULT '',
  cover TEXT DEFAULT '',
  banner TEXT DEFAULT '',
  platforms TEXT DEFAULT '',
  status TEXT DEFAULT '',
  rating REAL,
  notes TEXT DEFAULT '',
  started TEXT DEFAULT '',
  completed_date TEXT DEFAULT '',
  metascore INTEGER,
  hltb_main REAL, hltb_extra REAL, hltb_complete REAL, hltb_id TEXT DEFAULT '',
  released TEXT DEFAULT '',
  genres TEXT DEFAULT '',
  rawg_id INTEGER,
  steam_appid INTEGER,
  meta_checked INTEGER DEFAULT 0,
  hltb_checked INTEGER DEFAULT 0,
  manual_minutes INTEGER DEFAULT 0,
  favourite INTEGER DEFAULT 0,
  added TEXT DEFAULT '',
  updated TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  ext_id TEXT NOT NULL,
  name TEXT DEFAULT '',
  platform TEXT DEFAULT '',
  minutes INTEGER DEFAULT 0,
  first_played TEXT DEFAULT '',
  last_played TEXT DEFAULT '',
  image TEXT DEFAULT '',
  UNIQUE (source, ext_id));
CREATE TABLE IF NOT EXISTS play_log (
  id INTEGER PRIMARY KEY,
  game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  day TEXT NOT NULL,
  minutes INTEGER NOT NULL,
  source TEXT DEFAULT 'manual');
CREATE INDEX IF NOT EXISTS play_log_day ON play_log(day);
CREATE TABLE IF NOT EXISTS aw_rules (app TEXT PRIMARY KEY, game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS sources_game ON sources(game_id);
"""

GAME_FIELDS = ["title", "cover", "banner", "platforms", "status", "rating", "notes", "started", "completed_date",
               "metascore", "hltb_main", "hltb_extra", "hltb_complete", "released", "genres", "rawg_id",
               "steam_appid", "favourite"]
NUM_FIELDS = {"rating", "metascore", "hltb_main", "hltb_extra", "hltb_complete", "rawg_id", "steam_appid", "favourite"}

DEFAULT_SETTINGS = {
    "setup_done": "0",
    "display_name": "",
    "steam_id": "",
    "hltb_enabled": "1",
    "sync_hours": "6",
    "attribute_year": "1",
    "last_sync": "",
    "last_sync_result": "",
    "tracking_since": "",
}


def connect():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = connect()
    db.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        db.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (k, v))
    db.commit()
    db.close()


init_db()


def setting(db, key, default=""):
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r and r[0] is not None else default


def set_setting(db, key, value):
    db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, "" if value is None else str(value)))


def rows(db, sql, *args):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


def today():
    return dt.date.today().isoformat()


# Titles from different stores ("The Last of Us™ Part I" vs "The Last of Us Part I") are matched on this.
def norm_title(t):
    t = (t or "").lower()
    t = re.sub(r"[™®©]", "", t)
    t = t.replace("&", " and ")
    t = re.sub(r"\b(ps4|ps5|playstation ?[45])\b( (and|&) (ps4|ps5))?", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------- http helpers

UA = "VGTracker/1.0 (+self-hosted)"


def http(url, data=None, headers=None, method=None, timeout=25):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    if isinstance(data, dict):
        data = json.dumps(data).encode()
        h.setdefault("Content-Type", "application/json")
    elif isinstance(data, str):
        data = data.encode()
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body else {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def http_location(url, headers):
    """GET a URL without following redirects and return the Location header."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
    try:
        with opener.open(req, timeout=25) as r:
            return r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.headers.get("Location", "")


# ---------------------------------------------------------------- Steam

def steam_resolve_id(key, value):
    """Accept a SteamID64, a profile URL or a custom URL name and return the SteamID64."""
    value = (value or "").strip().rstrip("/")
    m = re.search(r"/profiles/(\d{17})", value)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d{17}", value):
        return value
    m = re.search(r"/id/([^/?#]+)", value)
    vanity = m.group(1) if m else value
    if not vanity:
        raise ValueError("Enter your SteamID64, profile URL or custom URL name")
    r = http("https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/?" +
             urllib.parse.urlencode({"key": key, "vanityurl": vanity}))
    resp = r.get("response", {})
    if resp.get("success") != 1:
        raise ValueError(f"Couldn't find a Steam profile called '{vanity}'")
    return resp["steamid"]


def steam_owned_games(key, steamid):
    r = http("https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?" + urllib.parse.urlencode({
        "key": key, "steamid": steamid, "include_appinfo": 1, "include_played_free_games": 1, "format": "json"}))
    resp = r.get("response", {})
    if "games" not in resp:
        raise ValueError("Steam returned no games. Check that your profile's 'Game details' privacy is set to Public.")
    out = []
    for gm in resp["games"]:
        lp = gm.get("rtime_last_played") or 0
        out.append({
            "source": "steam",
            "ext_id": str(gm["appid"]),
            "name": gm.get("name") or f"App {gm['appid']}",
            "platform": "Steam",
            "minutes": int(gm.get("playtime_forever") or 0),
            "first_played": "",
            "last_played": dt.datetime.fromtimestamp(lp).isoformat(timespec="seconds") if lp else "",
            "image": steam_cover(gm["appid"]),
            "steam_appid": gm["appid"],
        })
    return out


def steam_cover(appid):
    return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_600x900.jpg"


def steam_banner(appid):
    return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"


def steam_store_search(q):
    r = http("https://store.steampowered.com/api/storesearch/?" +
             urllib.parse.urlencode({"term": q, "l": "english", "cc": "GB"}))
    out = []
    for it in r.get("items", [])[:12]:
        ms = it.get("metascore")
        out.append({
            "title": it.get("name"),
            "cover": steam_cover(it["id"]),
            "banner": steam_banner(it["id"]),
            "steam_appid": it["id"],
            "metascore": int(ms) if str(ms or "").isdigit() else None,
            "released": "",
            "platforms": "PC",
            "source": "steam",
        })
    return out


def steam_appdetails(appid):
    r = http("https://store.steampowered.com/api/appdetails?" + urllib.parse.urlencode(
        {"appids": appid, "cc": "gb", "l": "english"}))
    d = (r.get(str(appid)) or {})
    return d.get("data") if d.get("success") else None


# ---------------------------------------------------------------- PlayStation Network
# Uses the same (unofficial) endpoints as the PlayStation app. Authentication starts from
# the "npsso" cookie you get by signing in at playstation.com.

PSN_AUTH = "https://ca.account.sony.com/api/authz/v3/oauth"
PSN_CLIENT_ID = "09515159-7237-4370-9b40-3806e67c0891"
PSN_BASIC = "Basic MDk1MTUxNTktNzIzNy00MzcwLTliNDAtMzgwNmU2N2MwODkxOnVjUGprYTV0bnRCMktxc1A="
PSN_REDIRECT = "com.scee.psxandroid.scecompcall://redirect"
PSN_SCOPE = "psn:mobile.v2.core psn:clientapp"


def psn_tokens_from_npsso(npsso):
    loc = http_location(f"{PSN_AUTH}/authorize?" + urllib.parse.urlencode({
        "access_type": "offline", "client_id": PSN_CLIENT_ID, "redirect_uri": PSN_REDIRECT,
        "response_type": "code", "scope": PSN_SCOPE}), {"Cookie": f"npsso={npsso}"})
    m = re.search(r"[?&]code=([^&]+)", loc or "")
    if not m:
        raise ValueError("PlayStation didn't accept that NPSSO token. Sign in at playstation.com and copy a fresh one.")
    return _psn_token({"code": m.group(1), "redirect_uri": PSN_REDIRECT,
                       "grant_type": "authorization_code", "token_format": "jwt"})


def psn_tokens_from_refresh(refresh):
    return _psn_token({"refresh_token": refresh, "grant_type": "refresh_token",
                       "token_format": "jwt", "scope": PSN_SCOPE})


def _psn_token(form):
    return http(f"{PSN_AUTH}/token", data=urllib.parse.urlencode(form), headers={
        "Content-Type": "application/x-www-form-urlencoded", "Authorization": PSN_BASIC}, method="POST")


def psn_access_token(db):
    """Get an access token, using the stored refresh token where possible."""
    refresh, exp = setting(db, "psn_refresh"), float(setting(db, "psn_refresh_exp", "0") or 0)
    tok = None
    if refresh and exp > time.time() + 60:
        try:
            tok = psn_tokens_from_refresh(refresh)
        except Exception as e:  # fall back to the NPSSO token
            log.warning("PSN refresh failed: %s", e)
    if not tok:
        npsso = setting(db, "psn_npsso")
        if not npsso:
            return None
        tok = psn_tokens_from_npsso(npsso)
    if tok.get("refresh_token"):
        set_setting(db, "psn_refresh", tok["refresh_token"])
        set_setting(db, "psn_refresh_exp", time.time() + int(tok.get("refresh_token_expires_in") or 0))
        db.commit()
    return tok["access_token"]


def iso_duration_minutes(s):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, sec = (float(x or 0) for x in m.groups())
    return int(d * 1440 + h * 60 + mi + sec / 60)


def psn_played_games(access):
    out, offset = [], 0
    while True:
        r = http("https://m.np.playstation.com/api/gamelist/v2/users/me/titles?" + urllib.parse.urlencode({
            "categories": "ps4_game,ps5_native_game", "limit": 200, "offset": offset}),
            headers={"Authorization": f"Bearer {access}"})
        for t in r.get("titles", []):
            cat = t.get("category", "")
            out.append({
                "source": "psn",
                "ext_id": str(t.get("concept", {}).get("id") or t.get("titleId")),
                "name": t.get("name") or t.get("titleId"),
                "platform": "PS5" if "ps5" in cat else "PS4",
                "minutes": iso_duration_minutes(t.get("playDuration")),
                "first_played": (t.get("firstPlayedDateTime") or "")[:19],
                "last_played": (t.get("lastPlayedDateTime") or "")[:19],
                "image": t.get("imageUrl") or "",
            })
        nxt = r.get("nextOffset")
        if not nxt or nxt <= offset or offset > 5000:
            break
        offset = nxt
    # One game can appear once per platform version - merge them by concept id
    merged = {}
    for it in out:
        m = merged.get(it["ext_id"])
        if not m:
            merged[it["ext_id"]] = it
            continue
        m["minutes"] += it["minutes"]
        m["first_played"] = min(filter(None, [m["first_played"], it["first_played"]]), default="")
        m["last_played"] = max(m["last_played"], it["last_played"])
        if it["platform"] == "PS5":
            m["platform"] = "PS5"
    return list(merged.values())


# ---------------------------------------------------------------- Xbox (via OpenXBL, https://xbl.io)
# Xbox has no public API for players, so this uses OpenXBL: a free service you sign in to with your
# Microsoft account, which gives you an API key. Title history lists what you've played and when;
# playtime ("MinutesPlayed") comes from a separate stats call per game.

XBL = "https://xbl.io/api/v2"
XBOX_STATS_PER_SYNC = 40  # OpenXBL's free tier allows a limited number of requests an hour


def xbl(key, path):
    return http(XBL + path, headers={"X-Authorization": key, "Accept-Language": "en-GB"})


def xbox_title_history(key):
    out = []
    for t in xbl(key, "/player/titleHistory").get("titles", []):
        if (t.get("type") or "Game") != "Game":
            continue
        devices = t.get("devices") or []
        lp = ((t.get("titleHistory") or {}).get("lastTimePlayed") or "")[:19]
        out.append({
            "source": "xbox",
            "ext_id": str(t.get("titleId")),
            "name": t.get("name") or str(t.get("titleId")),
            "platform": "Xbox" if any("xbox" in d.lower() for d in devices) or not devices else "PC (Xbox app)",
            "minutes": 0,
            "first_played": "",
            "last_played": lp,
            "image": t.get("displayImage") or "",
        })
    return out


def _find_stat(obj, name):
    """Find a stat value by name anywhere in the (deeply nested) stats response."""
    if isinstance(obj, dict):
        if str(obj.get("name", "")).lower() == name.lower() and "value" in obj:
            return obj["value"]
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            r = _find_stat(v, name)
            if r is not None:
                return r
    return None


def xbox_minutes(key, title_id):
    v = _find_stat(xbl(key, f"/achievements/stats/{title_id}"), "MinutesPlayed")
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def xbox_items(db, key):
    """Title history plus playtime. Playtime is only fetched for games played since the last sync (and a
    batch of not-yet-seen games each sync), to stay inside OpenXBL's rate limit. Games whose playtime
    hasn't been fetched yet are left out until it has, so their old hours aren't logged as new play."""
    titles = xbox_title_history(key)
    known = {r["ext_id"]: dict(r) for r in rows(db, "SELECT * FROM sources WHERE source='xbox'")}
    changed = [t for t in titles if t["ext_id"] in known and t["last_played"] > (known[t["ext_id"]]["last_played"] or "")]
    unseen = sorted([t for t in titles if t["ext_id"] not in known], key=lambda t: t["last_played"], reverse=True)
    budget = XBOX_STATS_PER_SYNC
    out = []
    for t in titles:
        k = known.get(t["ext_id"])
        if k and t not in changed:
            t["minutes"] = k["minutes"]
            out.append(t)
    for t in changed + unseen:
        if budget <= 0:
            if t["ext_id"] in known:  # keep last known playtime, try again next sync
                t["minutes"] = known[t["ext_id"]]["minutes"]
                t["last_played"] = known[t["ext_id"]]["last_played"]
                out.append(t)
            continue
        budget -= 1
        try:
            t["minutes"] = xbox_minutes(key, t["ext_id"])
        except urllib.error.HTTPError as e:
            if e.code == 429:
                budget = 0
                if t["ext_id"] in known:
                    t["minutes"], t["last_played"] = known[t["ext_id"]]["minutes"], known[t["ext_id"]]["last_played"]
                    out.append(t)
                continue
            t["minutes"] = 0
        out.append(t)
    pending = sum(1 for t in titles if t["ext_id"] not in known) - sum(1 for t in out if t["ext_id"] not in known)
    return out, max(0, pending)


# ---------------------------------------------------------------- ActivityWatch (Battle.net, EA, Epic, emulators...)
# Launchers like Battle.net have no API for playtime, but ActivityWatch records which app is in the
# foreground. You map an app (e.g. Overwatch.exe) to a game, and its time per day is added to the game.

def aw_get(base, path):
    return http(base.rstrip("/") + path, timeout=60)


def aw_window_buckets(base):
    return [b for b in aw_get(base, "/api/0/buckets/") if b.startswith("aw-watcher-window")]


def aw_app_time(base, days):
    """Seconds in the foreground per app, per day: {day: {app_lower: (app, seconds)}}."""
    out = {}
    buckets = aw_window_buckets(base)
    for i in range(0, len(days), 31):
        chunk = days[i:i + 31]
        periods = []
        for d in chunk:
            start = dt.datetime.combine(d, dt.time()).astimezone()
            periods.append(f"{start.isoformat()}/{(start + dt.timedelta(days=1)).isoformat()}")
        for b in buckets:
            res = http(base.rstrip("/") + "/api/0/query/", data={"timeperiods": periods, "query": [
                f'events = flood(query_bucket("{b}"));',
                'events = merge_events_by_keys(events, ["app"]);',
                "RETURN = events;"]}, method="POST", timeout=120)
            for d, evs in zip(chunk, res):
                day = out.setdefault(d.isoformat(), {})
                for e in evs or []:
                    app_name = (e.get("data") or {}).get("app") or ""
                    if not app_name:
                        continue
                    prev = day.get(app_name.lower(), (app_name, 0))
                    day[app_name.lower()] = (app_name, prev[1] + (e.get("duration") or 0))
    return out


def aw_sync(db):
    base = setting(db, "aw_url")
    rules = {r["app"]: r["game_id"] for r in rows(db, "SELECT * FROM aw_rules")}
    if not base or not rules:
        return None
    start = setting(db, "aw_synced_until")
    first = dt.date(dt.date.today().year, 1, 1)
    # Re-read the last couple of days each time, since today's total keeps growing
    since = max(first, dt.date.fromisoformat(start) - dt.timedelta(days=2)) if start else first
    days = [since + dt.timedelta(days=i) for i in range((dt.date.today() - since).days + 1)]
    data = aw_app_time(base, days)
    minutes = 0
    for app_l, gid in rules.items():
        src = "aw:" + app_l
        db.execute("DELETE FROM play_log WHERE source=? AND day>=?", (src, since.isoformat()))
        for day, apps in data.items():
            sec = apps.get(app_l, (None, 0))[1]
            if sec >= 60:
                db.execute("INSERT INTO play_log (game_id, day, minutes, source) VALUES (?,?,?,?)",
                           (gid, day, int(sec // 60), src))
                minutes += int(sec // 60)
        tot = db.execute("SELECT COALESCE(SUM(minutes),0), MAX(day), MIN(day) FROM play_log WHERE source=?", (src,)).fetchone()
        ex = db.execute("SELECT id FROM sources WHERE source=? AND ext_id=?", (src, app_l)).fetchone()
        if ex:
            db.execute("UPDATE sources SET game_id=?, minutes=?, last_played=?, first_played=? WHERE id=?",
                       (gid, tot[0], tot[1] or "", tot[2] or "", ex[0]))
        else:
            db.execute("""INSERT INTO sources (game_id, source, ext_id, name, platform, minutes, first_played, last_played)
                          VALUES (?,?,?,?,?,?,?,?)""", (gid, src, app_l, app_l, "PC", tot[0], tot[2] or "", tot[1] or ""))
            add_platform(db, gid, "PC")
        if tot[1] and tot[1] >= (dt.date.today() - dt.timedelta(days=14)).isoformat():
            mark_playing(db, gid, tot[2])
    set_setting(db, "aw_synced_until", today())
    db.commit()
    return {"apps": len(rules), "minutes": minutes}


# ---------------------------------------------------------------- RAWG (search, Metascores, genres)

def rawg_search(key, q, n=12):
    r = http("https://api.rawg.io/api/games?" + urllib.parse.urlencode(
        {"key": key, "search": q, "page_size": n, "search_precise": "true"}))
    out = []
    for it in r.get("results", []):
        out.append({
            "title": it.get("name"),
            "cover": it.get("background_image") or "",
            "banner": it.get("background_image") or "",
            "rawg_id": it.get("id"),
            "metascore": it.get("metacritic"),
            "released": it.get("released") or "",
            "platforms": ", ".join(p["platform"]["name"] for p in (it.get("platforms") or [])[:6]),
            "genres": ", ".join(x["name"] for x in (it.get("genres") or [])),
            "source": "rawg",
        })
    return out


# ---------------------------------------------------------------- HowLongToBeat

def hltb_lookup(title):
    """Returns (main, extra, completionist, id) in hours, or None. HLTB has no official API,
    so this uses the howlongtobeatpy package, which follows their site changes."""
    try:
        from howlongtobeatpy import HowLongToBeat
    except ImportError:
        return None
    res = HowLongToBeat().search(title)
    if not res:
        return None
    best = max(res, key=lambda r: r.similarity)
    if best.similarity < 0.55:
        return None
    nz = lambda v: round(float(v), 1) if v else None  # noqa: E731
    return nz(best.main_story), nz(best.main_extra), nz(best.completionist), str(best.game_id)


# ---------------------------------------------------------------- metadata enrichment

def enrich_game(db, gid, force=False):
    gm = db.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()
    if not gm:
        return
    gm = dict(gm)
    upd = {}
    if force or not gm["meta_checked"]:
        key = setting(db, "rawg_key")
        try:
            if key:
                res = rawg_search(key, gm["title"], 5)
                best = next((r for r in res if norm_title(r["title"]) == gm["norm"]), res[0] if res else None)
                if best and (best["rawg_id"] == gm["rawg_id"] or norm_title(best["title"]) == gm["norm"]
                             or gm["norm"] in norm_title(best["title"])):
                    upd["rawg_id"] = best["rawg_id"]
                    if best["metascore"] and (force or not gm["metascore"]):
                        upd["metascore"] = best["metascore"]
                    if best["genres"] and not gm["genres"]:
                        upd["genres"] = best["genres"]
                    if best["released"] and not gm["released"]:
                        upd["released"] = best["released"]
                    if best["cover"] and not gm["cover"]:
                        upd["cover"] = best["cover"]
                    if best["banner"] and not gm["banner"]:
                        upd["banner"] = best["banner"]
            if gm["steam_appid"] and not (upd.get("metascore") or gm["metascore"]) or \
                    gm["steam_appid"] and not gm["genres"] and not upd.get("genres"):
                d = steam_appdetails(gm["steam_appid"])
                if d:
                    mc = (d.get("metacritic") or {}).get("score")
                    if mc and not gm["metascore"]:
                        upd["metascore"] = mc
                    if not gm["genres"] and not upd.get("genres"):
                        upd["genres"] = ", ".join(x["description"] for x in d.get("genres") or [])
                    rd = (d.get("release_date") or {}).get("date", "")
                    if rd and not gm["released"] and not upd.get("released"):
                        for fmt in ("%d %b, %Y", "%b %d, %Y", "%d %B %Y"):
                            try:
                                upd["released"] = dt.datetime.strptime(rd, fmt).date().isoformat()
                                break
                            except ValueError:
                                pass
            upd["meta_checked"] = 1
        except Exception as e:
            log.warning("metadata lookup failed for %s: %s", gm["title"], e)
    if setting(db, "hltb_enabled", "1") == "1" and (force or not gm["hltb_checked"]):
        try:
            h = hltb_lookup(gm["title"])
            if h:
                upd.update(hltb_main=h[0], hltb_extra=h[1], hltb_complete=h[2], hltb_id=h[3])
            upd["hltb_checked"] = 1
        except Exception as e:
            log.warning("HLTB lookup failed for %s: %s", gm["title"], e)
    if upd:
        db.execute(f"UPDATE games SET {', '.join(k + '=?' for k in upd)} WHERE id=?", (*upd.values(), gid))
        db.commit()


# ---------------------------------------------------------------- sync

def find_or_create_game(db, it):
    if it.get("steam_appid"):
        r = db.execute("SELECT id FROM games WHERE steam_appid=?", (it["steam_appid"],)).fetchone()
        if r:
            return r[0], False
    n = norm_title(it["name"])
    r = db.execute("SELECT id FROM games WHERE norm=? ORDER BY id LIMIT 1", (n,)).fetchone()
    if r:
        if it.get("steam_appid"):
            db.execute("UPDATE games SET steam_appid=COALESCE(steam_appid, ?) WHERE id=?", (it["steam_appid"], r[0]))
        return r[0], False
    title = re.sub(r"\s*[™®]\s*", " ", it["name"]).strip()
    title = re.sub(r"\s{2,}", " ", title)
    cur = db.execute("""INSERT INTO games (title, norm, cover, banner, platforms, steam_appid, added, updated)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (title, n, it.get("image", ""),
                      steam_banner(it["steam_appid"]) if it.get("steam_appid") else "",
                      it["platform"], it.get("steam_appid"), now_iso(), now_iso()))
    return cur.lastrowid, True


def add_platform(db, gid, platform):
    cur = db.execute("SELECT platforms FROM games WHERE id=?", (gid,)).fetchone()[0] or ""
    plats = [p.strip() for p in cur.split(",") if p.strip()]
    if platform and platform not in plats:
        plats.append(platform)
        db.execute("UPDATE games SET platforms=? WHERE id=?", (", ".join(plats), gid))


def mark_playing(db, gid, day):
    db.execute("""UPDATE games SET status='playing', updated=?,
                  started=CASE WHEN started='' OR started IS NULL THEN ? ELSE started END
                  WHERE id=? AND status IN ('', 'planning', 'paused')""", (now_iso(), day, gid))


def apply_items(db, items, first_run):
    """Store a list of synced games. New playtime since the last sync is logged on the day it was played,
    which is what lets hours be split by year."""
    this_year = str(dt.date.today().year)
    recent = (dt.date.today() - dt.timedelta(days=14)).isoformat()
    attribute = setting(db, "attribute_year", "1") == "1"
    stats = {"games": 0, "new": 0, "updated": 0, "minutes": 0}
    for it in items:
        stats["games"] += 1
        src = db.execute("SELECT * FROM sources WHERE source=? AND ext_id=?", (it["source"], it["ext_id"])).fetchone()
        lp_day = (it["last_played"] or "")[:10]
        if src is None:
            gid, created = find_or_create_game(db, it)
            stats["new"] += created
            db.execute("""INSERT INTO sources (game_id, source, ext_id, name, platform, minutes, first_played,
                          last_played, image) VALUES (?,?,?,?,?,?,?,?,?)""",
                       (gid, it["source"], it["ext_id"], it["name"], it["platform"], it["minutes"],
                        it["first_played"], it["last_played"], it["image"]))
            add_platform(db, gid, it["platform"])
            if it["minutes"] > 0 and lp_day[:4] == this_year:
                # First time we've seen this game. We only know the lifetime total, so it's counted towards
                # this year if it was first played this year, or (optionally) if it was last played this year.
                if (it["first_played"] or "")[:4] == this_year or not first_run or attribute:
                    db.execute("INSERT INTO play_log (game_id, day, minutes, source) VALUES (?,?,?,?)",
                               (gid, lp_day, it["minutes"], it["source"]))
                    stats["minutes"] += it["minutes"]
            if lp_day and lp_day >= recent and it["minutes"] > 0:
                mark_playing(db, gid, (it["first_played"] or lp_day)[:10])
        else:
            gid = src["game_id"]
            delta = it["minutes"] - (src["minutes"] or 0)
            if delta > 0:
                day = lp_day if lp_day and lp_day > (src["last_played"] or "")[:10] else today()
                db.execute("INSERT INTO play_log (game_id, day, minutes, source) VALUES (?,?,?,?)",
                           (gid, day, delta, it["source"]))
                stats["updated"] += 1
                stats["minutes"] += delta
                mark_playing(db, gid, day)
            db.execute("""UPDATE sources SET name=?, platform=?, minutes=?, first_played=?, last_played=?, image=?
                          WHERE id=?""", (it["name"], it["platform"], it["minutes"],
                                          it["first_played"] or src["first_played"],
                                          it["last_played"] or src["last_played"], it["image"], src["id"]))
            if it["image"]:
                db.execute("UPDATE games SET cover=? WHERE id=? AND (cover='' OR cover IS NULL)", (it["image"], gid))
    db.commit()
    return stats


_sync_lock = threading.Lock()


def run_sync():
    """Sync every connected account. Returns a summary dict."""
    if not _sync_lock.acquire(blocking=False):
        return {"ok": False, "message": "A sync is already running"}
    try:
        db = connect()
        result, msgs = {"ok": True}, []

        def first(src):  # first sync of this account: its lifetime totals can't be dated
            new = not setting(db, f"synced_{src}") and not (src in ("steam", "psn") and setting(db, "last_sync"))
            set_setting(db, f"synced_{src}", "1")
            return new
        key, sid = setting(db, "steam_key"), setting(db, "steam_id")
        if key and sid:
            try:
                s = apply_items(db, steam_owned_games(key, sid), first("steam"))
                result["steam"] = s
                msgs.append(f"Steam: {s['games']} games, {s['updated']} with new playtime")
            except Exception as e:
                result["ok"] = False
                msgs.append(f"Steam failed: {e}")
        if setting(db, "psn_npsso") or setting(db, "psn_refresh"):
            try:
                tok = psn_access_token(db)
                s = apply_items(db, psn_played_games(tok), first("psn"))
                result["psn"] = s
                msgs.append(f"PlayStation: {s['games']} games, {s['updated']} with new playtime")
            except Exception as e:
                result["ok"] = False
                msgs.append(f"PlayStation failed: {e}")
        if setting(db, "xbox_key"):
            try:
                items, pending = xbox_items(db, setting(db, "xbox_key"))
                s = apply_items(db, items, first("xbox"))
                result["xbox"] = s
                msgs.append(f"Xbox: {s['games']} games, {s['updated']} with new playtime"
                            + (f" ({pending} still to fetch)" if pending else ""))
            except Exception as e:
                result["ok"] = False
                msgs.append(f"Xbox failed: {e}")
        if setting(db, "aw_url"):
            try:
                s = aw_sync(db)
                if s:
                    msgs.append(f"ActivityWatch: {s['apps']} apps tracked")
            except Exception as e:
                result["ok"] = False
                msgs.append(f"ActivityWatch failed: {e}")
        if not msgs:
            msgs.append("No accounts connected")
        if not setting(db, "tracking_since"):
            set_setting(db, "tracking_since", today())
        set_setting(db, "last_sync", now_iso())
        set_setting(db, "last_sync_result", " · ".join(msgs))
        db.commit()
        db.close()
        result["message"] = " · ".join(msgs)
        log.info("sync: %s", result["message"])
        wake_enricher()
        return result
    finally:
        _sync_lock.release()


# ---------------------------------------------------------------- background workers

_enrich_wake = threading.Event()


def wake_enricher():
    _enrich_wake.set()


def enricher_loop():
    while True:
        _enrich_wake.wait(timeout=600)
        _enrich_wake.clear()
        try:
            db = connect()
            # Games you're tracking come first, then the rest of the synced library
            todo = [r[0] for r in db.execute(
                """SELECT id FROM games WHERE status != 'ignored' AND (meta_checked=0 OR hltb_checked=0)
                   ORDER BY status='' , id DESC LIMIT 400""")]
            for gid in todo:
                enrich_game(db, gid)
                time.sleep(1.2)  # be polite to the APIs
            db.close()
        except Exception as e:
            log.warning("enricher: %s", e)


def scheduler_loop():
    time.sleep(20)
    while True:
        try:
            db = connect()
            hours = float(setting(db, "sync_hours", "6") or 0)
            last = setting(db, "last_sync")
            done = setting(db, "setup_done") == "1"
            db.close()
            due = not last or dt.datetime.fromisoformat(last) + dt.timedelta(hours=hours) <= dt.datetime.now()
            if done and hours > 0 and due:
                run_sync()
        except Exception as e:
            log.warning("scheduler: %s", e)
        time.sleep(300)


def start_background():
    if os.environ.get("VGT_NO_BACKGROUND"):
        return
    threading.Thread(target=enricher_loop, daemon=True, name="enricher").start()
    threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()
    wake_enricher()


start_background()

# ---------------------------------------------------------------- auth


def password_hash():
    return setting(get_db(), "password_hash")


def password_enabled():
    return bool(APP_PASSWORD or password_hash())


def check_password(pw):
    if APP_PASSWORD:
        return hmac.compare_digest(pw, APP_PASSWORD)
    h = password_hash()
    return bool(h) and check_password_hash(h, pw)


def authed():
    return not password_enabled() or session.get("ok")


@app.before_request
def guard():
    p = request.path
    if p in ("/login", "/healthz") or p.startswith("/static/"):
        return None
    if p == "/api/widget" and hmac.compare_digest(request.args.get("token", ""), WIDGET_TOKEN):
        return None
    if not authed():
        if p.startswith("/api/"):
            return jsonify(error="login required"), 401
        return redirect("/login")
    return None


LOGIN_HTML = """<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>VGTracker</title>
<link rel="icon" href="/static/icon.svg"><link rel="stylesheet" href="/static/style.css"></head>
<body class="login-body"><form method="post" class="login-card"><img src="/static/icon.svg" alt="" width="56">
<h1>VGTracker</h1><p class="muted">Enter the password to continue</p>{err}
<input type="password" name="password" autofocus placeholder="Password">
<button class="btn primary" type="submit">Sign in</button></form></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if not password_enabled():
        return redirect("/")
    err = ""
    if request.method == "POST":
        if check_password(request.form.get("password", "")):
            session.permanent = True
            session["ok"] = True
            return redirect("/")
        err = '<p class="err">Wrong password</p>'
    return LOGIN_HTML.format(err=err)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

# ---------------------------------------------------------------- API: state


def game_rows(db):
    """All games with total minutes, minutes per year and last played date."""
    games = rows(db, "SELECT * FROM games ORDER BY title COLLATE NOCASE")
    src = {}
    for s in rows(db, "SELECT game_id, source, platform, minutes, first_played, last_played FROM sources"):
        src.setdefault(s["game_id"], []).append(s)
    years, last_log = {}, {}
    for r in db.execute("SELECT game_id, substr(day,1,4) y, SUM(minutes) m, MAX(day) d FROM play_log GROUP BY game_id, y"):
        years.setdefault(r[0], {})[r[1]] = r[2]
        last_log[r[0]] = max(last_log.get(r[0], ""), r[3])
    for gm in games:
        ss = src.get(gm["id"], [])
        gm["sources"] = ss
        gm["minutes"] = sum(s["minutes"] or 0 for s in ss) + (gm["manual_minutes"] or 0)
        gm["year_minutes"] = years.get(gm["id"], {})
        gm["last_played"] = max([s["last_played"] or "" for s in ss] + [last_log.get(gm["id"], "")])
        gm["first_played"] = min([s["first_played"] for s in ss if s["first_played"]], default="")
        for k in ("norm", "meta_checked", "hltb_checked"):
            gm.pop(k, None)
    return games


@app.get("/api/state")
def state():
    db = get_db()
    settings = {r["key"]: r["value"] for r in rows(db, "SELECT * FROM settings") if r["key"] not in SECRET_KEYS}
    settings["has_steam_key"] = bool(setting(db, "steam_key"))
    settings["has_psn"] = bool(setting(db, "psn_npsso") or setting(db, "psn_refresh"))
    settings["psn_expires"] = setting(db, "psn_refresh_exp")
    settings["has_rawg_key"] = bool(setting(db, "rawg_key"))
    settings["has_xbox"] = bool(setting(db, "xbox_key"))
    try:
        import howlongtobeatpy  # noqa: F401
        settings["hltb_available"] = True
    except ImportError:
        settings["hltb_available"] = False
    pending = db.execute("SELECT COUNT(*) FROM games WHERE status!='ignored' AND (meta_checked=0 OR hltb_checked=0)"
                         ).fetchone()[0]
    return jsonify(
        settings=settings,
        games=game_rows(db),
        recent=rows(db, """SELECT p.*, g.title FROM play_log p JOIN games g ON g.id=p.game_id
                           WHERE g.status != 'ignored' ORDER BY day DESC, p.id DESC LIMIT 60"""),
        password_enabled=password_enabled(),
        password_from_env=bool(APP_PASSWORD),
        widget_url=f"/api/widget?token={WIDGET_TOKEN}",
        enrich_pending=pending,
        aw_rules=rows(db, "SELECT r.app, r.game_id, g.title FROM aw_rules r JOIN games g ON g.id=r.game_id ORDER BY r.app"),
        today=today(),
    )

# ---------------------------------------------------------------- API: games


def clean_game(data):
    out = {}
    for f in GAME_FIELDS:
        if f not in data:
            continue
        v = data[f]
        if f in NUM_FIELDS:
            if v in ("", None):
                v = None
            else:
                try:
                    v = float(v) if f in ("rating", "hltb_main", "hltb_extra", "hltb_complete") else int(float(v))
                except (TypeError, ValueError):
                    abort(400, f"{f} must be a number")
            if f == "rating" and v is not None and not 0 <= v <= 10:
                abort(400, "rating must be between 0 and 10")
        elif f == "status":
            v = v or ""
            if v not in STATUSES:
                abort(400, "unknown status")
        else:
            v = "" if v is None else str(v).strip()
        out[f] = v
    if "title" in out:
        if not out["title"]:
            abort(400, "title is required")
        out["norm"] = norm_title(out["title"])
    return out


@app.post("/api/games")
def add_game():
    db = get_db()
    data = request.get_json(force=True) or {}
    d = clean_game(data)
    if not d.get("title"):
        abort(400, "title is required")
    dup = None
    if d.get("steam_appid"):
        dup = db.execute("SELECT id FROM games WHERE steam_appid=?", (d["steam_appid"],)).fetchone()
    if not dup:
        dup = db.execute("SELECT id FROM games WHERE norm=?", (d["norm"],)).fetchone()
    if dup:  # already in the library (often from a sync) - update it instead of duplicating
        gid = dup[0]
        keep = {k: v for k, v in d.items() if k in ("status", "rating", "notes", "started", "completed_date")
                and v not in ("", None)}
        fill = {k: v for k, v in d.items() if k in ("cover", "banner", "metascore", "released", "genres", "rawg_id")
                and v not in ("", None)}
        for k, v in fill.items():
            db.execute(f"UPDATE games SET {k}=? WHERE id=? AND ({k} IS NULL OR {k}='')", (v, gid))
        if keep.get("status") == "completed" and not keep.get("completed_date"):
            keep["completed_date"] = db.execute("SELECT completed_date FROM games WHERE id=?", (gid,)).fetchone()[0] or today()
        if keep:
            db.execute(f"UPDATE games SET {', '.join(k + '=?' for k in keep)} WHERE id=?", (*keep.values(), gid))
        if d.get("platforms"):
            add_platform(db, gid, d["platforms"])
    else:
        if d.get("status") == "playing" and not d.get("started"):
            d["started"] = today()
        if d.get("status") == "completed" and not d.get("completed_date"):
            d["completed_date"] = today()
        d["added"] = d["updated"] = now_iso()
        cur = db.execute(f"INSERT INTO games ({', '.join(d)}) VALUES ({', '.join('?' * len(d))})", tuple(d.values()))
        gid = cur.lastrowid
    db.commit()
    wake_enricher()
    return jsonify(id=gid, existed=bool(dup))


@app.put("/api/games/<int:gid>")
def update_game(gid):
    db = get_db()
    d = clean_game(request.get_json(force=True) or {})
    if not d:
        return jsonify(ok=True)
    old = db.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone() or abort(404)
    st = d.get("status")
    if st == "completed" and old["status"] != "completed" and not d.get("completed_date") and not old["completed_date"]:
        d["completed_date"] = today()
    if st == "playing" and not d.get("started") and not old["started"]:
        d["started"] = today()
    if "title" in d and d["title"] != old["title"]:
        d["meta_checked"] = 0
        d["hltb_checked"] = 0
    d["updated"] = now_iso()
    db.execute(f"UPDATE games SET {', '.join(k + '=?' for k in d)} WHERE id=?", (*d.values(), gid))
    db.commit()
    if "meta_checked" in d:
        wake_enricher()
    return jsonify(ok=True)


@app.delete("/api/games/<int:gid>")
def delete_game(gid):
    db = get_db()
    if db.execute("SELECT 1 FROM sources WHERE game_id=?", (gid,)).fetchone():
        # Synced games would come straight back on the next sync, so hide them instead
        db.execute("UPDATE games SET status='ignored' WHERE id=?", (gid,))
    else:
        db.execute("DELETE FROM games WHERE id=?", (gid,))
    db.commit()
    return jsonify(ok=True)


@app.post("/api/games/<int:gid>/refresh")
def refresh_game(gid):
    db = get_db()
    enrich_game(db, gid, force=True)
    return jsonify(ok=True)


@app.post("/api/games/<int:gid>/merge")
def merge_game(gid):
    """Merge this game into another (e.g. the Steam and PlayStation copies of the same game)."""
    db = get_db()
    into = int((request.get_json(force=True) or {}).get("into") or 0)
    if into == gid:
        abort(400, "pick a different game")
    a = db.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone() or abort(404)
    b = db.execute("SELECT * FROM games WHERE id=?", (into,)).fetchone() or abort(404)
    db.execute("UPDATE sources SET game_id=? WHERE game_id=?", (into, gid))
    db.execute("UPDATE play_log SET game_id=? WHERE game_id=?", (into, gid))
    db.execute("UPDATE aw_rules SET game_id=? WHERE game_id=?", (into, gid))
    fill = {}
    for k in ("cover", "banner", "rating", "notes", "started", "completed_date", "metascore", "hltb_main",
              "hltb_extra", "hltb_complete", "released", "genres", "rawg_id", "steam_appid"):
        if b[k] in (None, "") and a[k] not in (None, ""):
            fill[k] = a[k]
    if not b["status"] and a["status"]:
        fill["status"] = a["status"]
    fill["manual_minutes"] = (b["manual_minutes"] or 0) + (a["manual_minutes"] or 0)
    db.execute(f"UPDATE games SET {', '.join(k + '=?' for k in fill)} WHERE id=?", (*fill.values(), into))
    for p in (a["platforms"] or "").split(","):
        add_platform(db, into, p.strip())
    db.execute("DELETE FROM games WHERE id=?", (gid,))
    db.commit()
    return jsonify(ok=True)


@app.post("/api/games/<int:gid>/sessions")
def add_session(gid):
    """Log time played by hand (for consoles that can't be synced)."""
    db = get_db()
    data = request.get_json(force=True) or {}
    try:
        minutes = int(round(float(data.get("hours") or 0) * 60))
    except ValueError:
        abort(400, "hours must be a number")
    day = data.get("day") or today()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        abort(400, "bad date")
    if minutes == 0:
        abort(400, "enter some time")
    db.execute("SELECT 1 FROM games WHERE id=?", (gid,)).fetchone() or abort(404)
    db.execute("INSERT INTO play_log (game_id, day, minutes, source) VALUES (?,?,?, 'manual')", (gid, day, minutes))
    db.execute("UPDATE games SET manual_minutes=manual_minutes+?, updated=? WHERE id=?", (minutes, now_iso(), gid))
    if minutes > 0:
        mark_playing(db, gid, day)
    db.commit()
    return jsonify(ok=True)


@app.get("/api/games/<int:gid>/sessions")
def list_sessions(gid):
    return jsonify(rows(get_db(), "SELECT * FROM play_log WHERE game_id=? ORDER BY day DESC, id DESC", gid))


@app.delete("/api/sessions/<int:sid>")
def delete_session(sid):
    db = get_db()
    s = db.execute("SELECT * FROM play_log WHERE id=?", (sid,)).fetchone() or abort(404)
    if s["source"] in ("manual", "import"):
        db.execute("UPDATE games SET manual_minutes=MAX(0, manual_minutes-?) WHERE id=?", (s["minutes"], s["game_id"]))
    db.execute("DELETE FROM play_log WHERE id=?", (sid,))
    db.commit()
    return jsonify(ok=True)

# ---------------------------------------------------------------- API: search


@app.get("/api/search")
def search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify(results=[], provider="")
    db = get_db()
    key = setting(db, "rawg_key")
    try:
        if key:
            return jsonify(results=rawg_search(key, q), provider="RAWG")
        return jsonify(results=steam_store_search(q), provider="Steam")
    except Exception as e:
        abort(502, f"Search failed: {e}")

# ---------------------------------------------------------------- API: settings & accounts


@app.post("/api/settings")
def save_settings():
    db = get_db()
    data = request.get_json(force=True) or {}
    allowed = {"display_name", "hltb_enabled", "sync_hours", "attribute_year", "setup_done"}
    for k, v in data.items():
        if k in allowed or re.fullmatch(r"goty_\d{4}", k):
            set_setting(db, k, v)
    db.commit()
    return jsonify(ok=True)


@app.post("/api/accounts/steam")
def connect_steam():
    db = get_db()
    data = request.get_json(force=True) or {}
    if data.get("remove"):
        set_setting(db, "steam_key", "")
        set_setting(db, "steam_id", "")
        db.commit()
        return jsonify(ok=True)
    key = (data.get("key") or "").strip() or setting(db, "steam_key")
    if not re.fullmatch(r"[0-9A-Fa-f]{32}", key or ""):
        abort(400, "That doesn't look like a Steam Web API key (32 letters and numbers)")
    try:
        sid = steam_resolve_id(key, data.get("steam_id"))
        games = steam_owned_games(key, sid)
    except urllib.error.HTTPError as e:
        abort(400, "Steam rejected the API key" if e.code in (401, 403) else f"Steam error {e.code}")
    except Exception as e:
        abort(400, str(e))
    set_setting(db, "steam_key", key)
    set_setting(db, "steam_id", sid)
    db.commit()
    played = sum(1 for x in games if x["minutes"] > 0)
    return jsonify(ok=True, steam_id=sid, games=len(games), played=played)


@app.post("/api/accounts/psn")
def connect_psn():
    db = get_db()
    data = request.get_json(force=True) or {}
    if data.get("remove"):
        for k in ("psn_npsso", "psn_refresh", "psn_refresh_exp"):
            set_setting(db, k, "")
        db.commit()
        return jsonify(ok=True)
    npsso = (data.get("npsso") or "").strip()
    m = re.search(r'"npsso"\s*:\s*"([^"]+)"', npsso)  # allow pasting the whole JSON
    if m:
        npsso = m.group(1)
    if len(npsso) < 20:
        abort(400, "Paste the 64-character npsso value")
    try:
        tok = psn_tokens_from_npsso(npsso)
        games = psn_played_games(tok["access_token"])
    except Exception as e:
        abort(400, str(e))
    set_setting(db, "psn_npsso", npsso)
    set_setting(db, "psn_refresh", tok.get("refresh_token", ""))
    set_setting(db, "psn_refresh_exp", time.time() + int(tok.get("refresh_token_expires_in") or 0))
    db.commit()
    return jsonify(ok=True, games=len(games))


@app.post("/api/accounts/xbox")
def connect_xbox():
    db = get_db()
    data = request.get_json(force=True) or {}
    if data.get("remove"):
        set_setting(db, "xbox_key", "")
        db.commit()
        return jsonify(ok=True)
    key = (data.get("key") or "").strip()
    if len(key) < 10:
        abort(400, "Paste your OpenXBL API key")
    try:
        titles = xbox_title_history(key)
    except urllib.error.HTTPError as e:
        abort(400, "OpenXBL rejected that key" if e.code in (401, 403) else f"OpenXBL error {e.code}")
    except Exception as e:
        abort(400, str(e))
    set_setting(db, "xbox_key", key)
    db.commit()
    return jsonify(ok=True, games=len(titles))


@app.post("/api/aw")
def connect_aw():
    db = get_db()
    url = ((request.get_json(force=True) or {}).get("url") or "").strip().rstrip("/")
    if url:
        if not re.match(r"https?://", url):
            url = "http://" + url
        try:
            n = len(aw_window_buckets(url))
        except Exception as e:
            abort(400, f"Couldn't reach ActivityWatch at {url}: {e}")
        if not n:
            abort(400, "Connected, but ActivityWatch has no window-tracking data (aw-watcher-window)")
    set_setting(db, "aw_url", url)
    set_setting(db, "aw_synced_until", "")
    db.commit()
    return jsonify(ok=True, url=url)


@app.get("/api/aw/apps")
def aw_apps():
    """The apps you've used most over the last 60 days, to pick games from."""
    db = get_db()
    base = setting(db, "aw_url")
    if not base:
        abort(400, "Connect ActivityWatch first")
    days = [dt.date.today() - dt.timedelta(days=i) for i in range(59, -1, -1)]
    try:
        data = aw_app_time(base, days)
    except Exception as e:
        abort(502, f"ActivityWatch: {e}")
    tot = {}
    for apps in data.values():
        for k, (name, sec) in apps.items():
            n, s = tot.get(k, (name, 0))
            tot[k] = (n, s + sec)
    rules = {r["app"]: r for r in rows(db, "SELECT r.app, r.game_id, g.title FROM aw_rules r JOIN games g ON g.id=r.game_id")}
    out = [{"app": n, "key": k, "minutes": int(s // 60), "game_id": rules.get(k, {}).get("game_id"),
            "title": rules.get(k, {}).get("title", "")}
           for k, (n, s) in sorted(tot.items(), key=lambda x: -x[1][1]) if s >= 600][:80]
    return jsonify(apps=out)


@app.post("/api/aw/rules")
def aw_rule():
    """Map an app to a game (by title, creating the game if needed), or remove the mapping."""
    db = get_db()
    data = request.get_json(force=True) or {}
    app_l = (data.get("app") or "").strip().lower()
    title = (data.get("title") or "").strip()
    if not app_l:
        abort(400, "app is required")
    src = "aw:" + app_l
    db.execute("DELETE FROM play_log WHERE source=?", (src,))
    db.execute("DELETE FROM sources WHERE source=?", (src,))
    db.execute("DELETE FROM aw_rules WHERE app=?", (app_l,))
    gid = None
    if title:
        r = db.execute("SELECT id FROM games WHERE norm=?", (norm_title(title),)).fetchone()
        if r:
            gid = r[0]
        else:
            gid = db.execute("INSERT INTO games (title, norm, platforms, added, updated) VALUES (?,?,?,?,?)",
                             (title, norm_title(title), "PC", now_iso(), now_iso())).lastrowid
        db.execute("INSERT INTO aw_rules VALUES (?, ?)", (app_l, gid))
    set_setting(db, "aw_synced_until", "")  # re-read the year so the new game gets its history
    db.commit()
    if gid:
        try:
            aw_sync(db)
        except Exception as e:
            log.warning("ActivityWatch sync after rule change: %s", e)
        wake_enricher()
    return jsonify(ok=True, game_id=gid)


@app.post("/api/accounts/rawg")
def connect_rawg():
    db = get_db()
    key = ((request.get_json(force=True) or {}).get("key") or "").strip()
    if key:
        try:
            rawg_search(key, "Portal", 1)
        except urllib.error.HTTPError as e:
            abort(400, "RAWG rejected that key" if e.code in (401, 403) else f"RAWG error {e.code}")
        except Exception as e:
            abort(400, str(e))
        # Let games that only got Steam data try again with RAWG
        db.execute("UPDATE games SET meta_checked=0 WHERE metascore IS NULL OR genres=''")
    set_setting(db, "rawg_key", key)
    db.commit()
    wake_enricher()
    return jsonify(ok=True)


@app.post("/api/sync")
def sync_now():
    return jsonify(run_sync())


@app.post("/api/refresh-all")
def refresh_all():
    db = get_db()
    db.execute("UPDATE games SET meta_checked=0, hltb_checked=0 WHERE status != 'ignored'")
    db.commit()
    wake_enricher()
    return jsonify(ok=True)


@app.post("/api/password")
def set_password():
    if APP_PASSWORD:
        abort(400, "The password is set by APP_PASSWORD in your Docker config - change it there")
    db = get_db()
    pw = (request.get_json(force=True) or {}).get("password", "")
    if pw:
        if len(pw) < 4:
            abort(400, "password must be at least 4 characters")
        set_setting(db, "password_hash", generate_password_hash(pw))
        session.permanent = True
        session["ok"] = True
    else:
        set_setting(db, "password_hash", "")
    db.commit()
    return jsonify(ok=True)

# ---------------------------------------------------------------- Yamtrack import

YAM_STATUS = {"completed": "completed", "in progress": "playing", "in_progress": "playing", "playing": "playing",
              "planning": "planning", "plan to play": "planning", "paused": "paused", "on hold": "paused",
              "dropped": "dropped", "repeating": "playing"}


def _date(v):
    m = re.match(r"(\d{4}-\d{2}-\d{2})", (v or "").strip())
    return m.group(1) if m else ""


@app.post("/api/import/yamtrack")
def import_yamtrack():
    """Import games from a Yamtrack CSV export (Settings > Import/Export > Export CSV in Yamtrack)."""
    db = get_db()
    f = request.files.get("file")
    if not f:
        abort(400, "choose a CSV file")
    text = f.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        abort(400, "that file is empty")
    hdr = {h.lower().strip(): h for h in reader.fieldnames}
    col = lambda r, *names: next((r.get(hdr[n]) for n in names if n in hdr and r.get(hdr[n]) not in (None, "")), "")  # noqa: E731
    added = updated = skipped = 0
    for r in reader:
        mtype = col(r, "media_type", "type").lower()
        if mtype and mtype != "game":
            skipped += 1
            continue
        title = col(r, "title", "name")
        if not title:
            skipped += 1
            continue
        status = YAM_STATUS.get(col(r, "status").lower().strip(), "")
        try:
            score = float(col(r, "score", "rating") or "nan")
            score = None if score != score else max(0.0, min(10.0, score))
        except ValueError:
            score = None
        try:
            progress = int(float(col(r, "progress") or 0))  # Yamtrack stores game progress as minutes played
        except ValueError:
            progress = 0
        start, end = _date(col(r, "start_date", "started")), _date(col(r, "end_date", "completed", "finished"))
        image, notes = col(r, "image", "cover"), col(r, "notes")
        n = norm_title(title)
        ex = db.execute("SELECT * FROM games WHERE norm=?", (n,)).fetchone()
        if ex:
            upd = {}
            if status and not ex["status"]:
                upd["status"] = status
            if score is not None and ex["rating"] is None:
                upd["rating"] = score
            for k, v in (("notes", notes), ("started", start), ("cover", image)):
                if v and not ex[k]:
                    upd[k] = v
            if end and status == "completed" and not ex["completed_date"]:
                upd["completed_date"] = end
            has_sync = db.execute("SELECT 1 FROM sources WHERE game_id=? AND minutes>0", (ex["id"],)).fetchone()
            if progress and not has_sync and not ex["manual_minutes"]:
                upd["manual_minutes"] = progress
                db.execute("INSERT INTO play_log (game_id, day, minutes, source) VALUES (?,?,?, 'import')",
                           (ex["id"], end or start or today(), progress))
            if upd:
                db.execute(f"UPDATE games SET {', '.join(k + '=?' for k in upd)} WHERE id=?", (*upd.values(), ex["id"]))
                updated += 1
            else:
                skipped += 1
        else:
            cur = db.execute("""INSERT INTO games (title, norm, cover, status, rating, notes, started, completed_date,
                                manual_minutes, added, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                             (title, n, image, status, score, notes, start,
                              end if status == "completed" else "", progress, now_iso(), now_iso()))
            if progress:
                db.execute("INSERT INTO play_log (game_id, day, minutes, source) VALUES (?,?,?, 'import')",
                           (cur.lastrowid, end or start or today(), progress))
            added += 1
    db.commit()
    wake_enricher()
    return jsonify(added=added, updated=updated, skipped=skipped)

# ---------------------------------------------------------------- year round-up


def played_in_year(db, y):
    ids = {r[0] for r in db.execute("SELECT DISTINCT game_id FROM play_log WHERE substr(day,1,4)=?", (y,))}
    ids |= {r[0] for r in db.execute("""SELECT DISTINCT game_id FROM sources
                                        WHERE substr(last_played,1,4)=? OR substr(first_played,1,4)=?""", (y, y))}
    ids |= {r[0] for r in db.execute("""SELECT id FROM games WHERE substr(started,1,4)=?
                                        OR substr(completed_date,1,4)=?""", (y, y))}
    ign = {r[0] for r in db.execute("SELECT id FROM games WHERE status='ignored'")}
    return ids - ign


def available_years(db):
    ys = set()
    for sql in ("SELECT DISTINCT substr(day,1,4) FROM play_log",
                "SELECT DISTINCT substr(last_played,1,4) FROM sources WHERE last_played!=''",
                "SELECT DISTINCT substr(completed_date,1,4) FROM games WHERE completed_date!=''",
                "SELECT DISTINCT substr(started,1,4) FROM games WHERE started!=''"):
        ys |= {r[0] for r in db.execute(sql) if r[0] and r[0].isdigit()}
    ys.add(str(dt.date.today().year))
    return sorted(ys, reverse=True)


@app.get("/api/roundup")
def roundup():
    db = get_db()
    y = request.args.get("year") or str(dt.date.today().year)
    if not re.fullmatch(r"\d{4}", y):
        abort(400, "bad year")
    games = {gm["id"]: gm for gm in game_rows(db)}
    ids = played_in_year(db, y)
    played = []
    for i in ids:
        gm = games.get(i)
        if not gm:
            continue
        gm = dict(gm)
        gm["ymin"] = gm["year_minutes"].get(y)
        played.append(gm)
    played.sort(key=lambda x: (x["ymin"] or 0), reverse=True)
    months = [0] * 12
    for r in db.execute("""SELECT CAST(substr(day,6,2) AS INTEGER), SUM(minutes) FROM play_log
                           WHERE substr(day,1,4)=? AND game_id NOT IN (SELECT id FROM games WHERE status='ignored') GROUP BY 1""", (y,)):
        months[r[0] - 1] = r[1]
    # Each logged chunk of playtime knows where it came from, so the split is exact for synced time
    platform_min = {}
    for r in db.execute("""SELECT game_id, source, SUM(minutes) FROM play_log WHERE substr(day,1,4)=?
                           GROUP BY game_id, source""", (y,)):
        gm = games.get(r[0])
        if not gm or gm["status"] == "ignored":
            continue
        src = [s["platform"] for s in gm["sources"] if s["source"] == r[1]]
        own = [p.strip() for p in (gm["platforms"] or "").split(",") if p.strip()]
        key = src[0] if src else (own[0] if own else "Other")
        platform_min[key] = platform_min.get(key, 0) + r[2]
    genres = {}
    for gm in played:
        for gn in (gm["genres"] or "").split(","):
            if gn.strip():
                genres[gn.strip()] = genres.get(gn.strip(), 0) + 1
    completed = sorted([gm for gm in played if (gm["completed_date"] or "")[:4] == y],
                       key=lambda x: x["completed_date"])
    rated = sorted([gm for gm in played if gm["rating"] is not None], key=lambda x: -x["rating"])
    goty_id = setting(db, f"goty_{y}")
    goty = games.get(int(goty_id)) if goty_id.isdigit() else None
    if not goty:
        pool = [gm for gm in completed if gm["rating"] is not None] or rated
        goty = max(pool, key=lambda x: (x["rating"], x["ymin"] or 0)) if pool else None
    takes = sorted([gm for gm in played if gm["rating"] is not None and gm["metascore"]],
                   key=lambda x: -abs(x["rating"] * 10 - x["metascore"]))[:3]
    busiest = db.execute("""SELECT day, SUM(minutes) m FROM play_log WHERE substr(day,1,4)=? AND source != 'import' AND game_id NOT IN (SELECT id FROM games WHERE status='ignored')
                            GROUP BY day ORDER BY m DESC LIMIT 1""", (y,)).fetchone()
    backlog = [gm for gm in completed if (gm["added"] or "")[:4] < y and gm["added"]]
    return jsonify(
        year=y,
        years=available_years(db),
        games=played,
        total_minutes=sum(months),
        untracked=sum(1 for gm in played if not gm["ymin"]),
        months=months,
        platforms=sorted(platform_min.items(), key=lambda x: -x[1]),
        genres=sorted(genres.items(), key=lambda x: -x[1])[:8],
        completed=completed,
        dropped=[gm for gm in played if gm["status"] == "dropped"],
        started=[gm for gm in played if (gm["started"] or "")[:4] == y],
        top_rated=rated[:5],
        avg_rating=round(sum(x["rating"] for x in rated) / len(rated), 1) if rated else None,
        goty=goty,
        goty_picked=bool(goty_id),
        hot_takes=takes,
        busiest_day={"day": busiest[0], "minutes": busiest[1]} if busiest else None,
        backlog_cleared=len(backlog),
        tracking_since=setting(db, "tracking_since"),
    )

# ---------------------------------------------------------------- widget (for Homepage / Glance)


@app.get("/api/widget")
def widget():
    db = get_db()
    y = str(dt.date.today().year)
    games = game_rows(db)
    playing = sorted([gm for gm in games if gm["status"] == "playing"], key=lambda x: x["last_played"], reverse=True)
    mins = db.execute("SELECT COALESCE(SUM(minutes),0) FROM play_log WHERE substr(day,1,4)=? AND game_id NOT IN (SELECT id FROM games WHERE status='ignored')", (y,)).fetchone()[0]
    return jsonify(
        playing=len(playing),
        now_playing=playing[0]["title"] if playing else "",
        hours_this_year=round(mins / 60, 1),
        played_this_year=len(played_in_year(db, y)),
        completed_this_year=sum(1 for gm in games if (gm["completed_date"] or "")[:4] == y),
        backlog=sum(1 for gm in games if gm["status"] == "planning"),
        last_sync=setting(db, "last_sync"),
    )

# ---------------------------------------------------------------- backup


@app.get("/api/backup")
def backup():
    db = get_db()
    data = {
        "app": "vgtracker", "version": 1, "exported": now_iso(),
        "settings": [r for r in rows(db, "SELECT * FROM settings") if r["key"] not in SECRET_KEYS],
        "games": rows(db, "SELECT * FROM games"),
        "sources": rows(db, "SELECT * FROM sources"),
        "play_log": rows(db, "SELECT * FROM play_log"),
    }
    resp = jsonify(data)
    resp.headers["Content-Disposition"] = f"attachment; filename=vgtracker-backup-{today()}.json"
    return resp


@app.post("/api/restore")
def restore():
    db = get_db()
    f = request.files.get("file")
    if not f:
        abort(400, "choose a backup file")
    try:
        data = json.loads(f.read())
        assert data.get("app") == "vgtracker"
    except Exception:
        abort(400, "that isn't a VGTracker backup")
    db.execute("DELETE FROM play_log")
    db.execute("DELETE FROM sources")
    db.execute("DELETE FROM games")
    for table in ("games", "sources", "play_log"):
        for r in data.get(table, []):
            cols = [c for c in r if re.fullmatch(r"[a-z_0-9]+", c)]
            db.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       tuple(r[c] for c in cols))
    for r in data.get("settings", []):
        if r["key"] not in SECRET_KEYS:
            set_setting(db, r["key"], r["value"])
    db.commit()
    return jsonify(ok=True)


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(502)
def err(e):
    if request.path.startswith("/api/"):
        return e.description if isinstance(e.description, str) else str(e), e.code
    return e


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
