#!/usr/bin/env python3
"""Vortex Tracker: runs a small local website (light + dark, purple) that tracks a player's stats."""
import os, re, json, time, random, html as htmllib, sqlite3, webbrowser, threading, traceback
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urljoin, quote
import requests

BASE = "https://playvortex.io"
DEPLOYED = bool(os.environ.get("PORT"))             # Render (and most hosts) set PORT; locally it stays unset
PORT = int(os.environ.get("PORT", 8765))
HOST = "0.0.0.0" if DEPLOYED else "127.0.0.1"
DB_PATH = "vortex_dashboard.db"                      # used locally when DATABASE_URL isn't set
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()   # Supabase Postgres connection string (deployed)
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()     # if set, debug routes need ?token=...
LOCK = threading.Lock()
# (path, label). Label = prefix the data is stored under. For each label the FIRST path that answers wins.
# These are guesses. Open http://localhost:8765/api/probe?id=95204 to see which ones respond, and add the
# real ones from your browser DevTools (Network > Fetch/XHR) at the top of the list.
ENDPOINTS = [
    ("/api/users/{id}", "user"), ("/api/users/{id}/profile", "profile"),
    ("/api/users/{id}/friends", "friends"), ("/api/friends/{id}", "friends"),
    ("/api/friends?userId={id}", "friends"), ("/api/friends?user_id={id}", "friends"),
    ("/api/users/{id}/followers", "followers"), ("/api/users/{id}/following", "following"),
    ("/api/users/{id}/games", "games"), ("/api/users/{id}/badges", "badges"),
    ("/api/users/{id}/groups", "groups"), ("/api/groups/user/{id}", "groups"),
    ("/api/groups?userId={id}", "groups"), ("/api/groups?user_id={id}", "groups"),
    ("/api/users/{id}/clans", "clans"), ("/api/users/{id}/inventory", "inventory"),
    ("/api/users/{id}/presence", "presence"), ("/api/users/{id}/stats", "stats"),
]
# Live player counts change constantly, so they are shown but never logged as "changes".
NOISY = re.compile(r"\.active$")

_ENT = re.compile(r"&(#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);")
def unent(v):
    """Turn HTML entities like &#34; or &amp; back into real characters (only when a full entity is present)."""
    return htmllib.unescape(v) if isinstance(v, str) and _ENT.search(v) else v

def flatten(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        out[f"{prefix}.count"] = len(obj)
        for i, v in enumerate(obj[:200]):
            out.update(flatten(v, f"{prefix}.{i}"))
    else:
        out[prefix] = unent(obj)
    return out

_IMG = re.compile(r"<img\b[^>]*>", re.I)
_ATTR = re.compile(r'([\w:-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
_HINT = re.compile(r"badge|admin|creator|staff|verified|moderator|developer|founder|\bcc\b", re.I)
_FILE = re.compile(r"""["'(]((?:https?:)?/[^"'()\s]*badge[^"'()\s]*\.(?:png|svg|webp|jpe?g|gif))""", re.I)

def scrape_badges(html):
    """Pull badge images straight off the profile page (the site's own icons)."""
    found, seen = [], set()
    def add(src, name):
        url = urljoin(BASE + "/", src)
        if url in seen or "avatar" in url.lower():
            return
        seen.add(url)
        if not name:
            name = re.sub(r"[-_]+", " ", url.split("?")[0].rsplit("/", 1)[-1].rsplit(".", 1)[0]).strip()
        found.append((url, name))
    for tag in _IMG.findall(html):
        a = {m[0].lower(): (m[1] or m[2]) for m in _ATTR.findall(tag)}
        src = a.get("src") or a.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue
        hay = " ".join([src, a.get("alt", ""), a.get("title", ""), a.get("class", "")])
        if "thumbnail" in src.lower():
            continue
        if _HINT.search(hay):
            add(src, a.get("alt") or a.get("title") or "")
    for src in _FILE.findall(html):
        add(src, "")
    return found

def fetch_created(s, uid, data):
    """Games this user made, with live players + total visits (from /api/games and /api/game-stats)."""
    try:
        g = s.get(BASE + "/api/games", timeout=15)
        if g.status_code != 200:
            return False
        games = g.json()
        st = s.get(BASE + "/api/game-stats", timeout=15)
        stats = st.json() if st.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        return False
    if isinstance(games, dict):
        games = next((v for v in games.values() if isinstance(v, list)), [])
    if not isinstance(games, list):
        return False
    if not isinstance(stats, dict):
        stats = {}
    mine = sorted((x for x in games if isinstance(x, dict) and str(x.get("creator_id")) == str(uid)),
                  key=lambda x: str(x.get("id")).zfill(12))
    data["created.count"] = len(mine)
    for i, x in enumerate(mine):
        gs = stats.get(str(x.get("id")), {}) if isinstance(stats.get(str(x.get("id")), {}), dict) else {}
        data[f"created.{i}.id"] = x.get("id")
        data[f"created.{i}.name"] = x.get("name")
        data[f"created.{i}.description"] = x.get("description")
        data[f"created.{i}.thumbnail_version"] = x.get("thumbnail_version")
        data[f"created.{i}.active"] = gs.get("active", 0)
        data[f"created.{i}.visits"] = gs.get("visits", 0)
    return True

def fetch_snapshot(uid):
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (personal stat tracker)"
    data, found, name = {}, [], None
    try:
        html = s.get(f"{BASE}/users/{uid}/profile", timeout=15).content.decode("utf-8", "replace")
        m = re.search(r'(\d+)\s*friends\s*\|\s*(\d+)\s*followers\s*\|\s*Joined\s*([A-Za-z]+ \d+, \d{4})', html)
        if m:
            data["meta.friends"], data["meta.followers"], data["meta.joined"] = int(m[1]), int(m[2]), m[3]
        t = re.search(r"<title>(.*?)</title>", html, re.S)
        if t:
            name = unent(t[1]).replace("- Vortex", "").strip()
            data["meta.name"] = name
        d = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]*)"', html)
        if d:
            data["meta.description"] = unent(d[1])
        lo = re.search(r"[Ll]ast (?:online|seen|active)\W{0,6}([^<\"|]{2,40})", html)
        if lo:
            data["meta.last_online"] = unent(lo[1].strip())
        badges = scrape_badges(html)
        if badges:
            data["profilebadges.count"] = len(badges)
            for i, (url, bname) in enumerate(badges):
                data[f"profilebadges.{i}.image"] = url
                data[f"profilebadges.{i}.name"] = bname
        print(f"[tracker] user {uid}: {len(badges)} badge image(s) found on profile page")
    except requests.RequestException:
        pass
    got = set()
    for ep, label in ENDPOINTS:
        if label in got:
            continue
        try:
            r = s.get(BASE + ep.format(id=uid), timeout=15)
            if r.status_code != 200:
                continue
            payload = r.json()
        except (requests.RequestException, ValueError):
            continue
        got.add(label)
        data.update(flatten(payload, label))
        found.append(ep)
        time.sleep(0.3)
    fill_missing_lists(s, uid, data, got, found)
    if fetch_created(s, uid, data):
        found.append("/api/games")
    return data, found

# ---------------------------------------------------------------------------------------------
# Self-healing API discovery + automatic star-code <-> Vortex username matching.
# The site's real API paths aren't documented, so instead of hardcoding more guesses this reads the
# site's own JavaScript, pulls out every /api/... path it calls, and tries the ones that look like
# followers / following / friends / groups / badges lists. If the API doesn't answer, it falls back
# to scraping the same lists from the site's HTML pages.
# ---------------------------------------------------------------------------------------------
STAR_CODES = [("QuintupleOne", "11111"), ("2Tone", "2Tone"), ("Gaming", "Gaming"), ("Kaazmaz", "Kaaz"), ("Keyhitt3", "Key"),
              ("Kingvaylo", "Vaylo"), ("MiningRoger", "Roger"), ("OllieVortex", "OllieXD"), ("OneSpruce", "Spruce"),
              ("Owenyahhhh", "Owen"), ("Shiv", "Shiv"), ("Superhappywars", "Super"), ("TheBlockMan", "Block"),
              ("Xotgamez", "Xot"), ("Yk.itslux", "Lux"), ("Kostas", "Kostas")]
LABELS = ["following", "followers", "friends", "groups", "badges", "clans", "inventory", "presence", "stats"]
LEARN_LOCK = threading.Lock()
LEARNED = {"ts": 0.0, "id_eps": [], "static": [], "search": [], "scripts": 0}
EP_FAIL, EP_OK = {}, set()
_API_STR = re.compile(r"""[`"'](/api/[A-Za-z0-9_\-./${}:?=&%]{2,160})[`"']""")
_API_CAT = re.compile(r"""[`"'](/api/[A-Za-z0-9_\-./?=&]*)[`"']\s*\+\s*[A-Za-z_$][\w$.\[\]()]*\s*(?:\+\s*[`"']([A-Za-z0-9_\-./?=&]*)[`"'])?""")
_PLACE = re.compile(r"\$\{[^}]*\}|(?<=/):[A-Za-z_]\w*|\{[A-Za-z_]\w*\}")
_LINK = re.compile(r"""<a\b[^>]*?href=["'](?:https?://[^"'/]*)?/users/(\d+)[^"']*["'][^>]*>(.*?)</a>""", re.S | re.I)

def _session():
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (personal stat tracker)"
    return s

def _norm(x):
    return re.sub(r"[^a-z0-9]", "", str(x or "").lower())

def _sim(a, b):
    import difflib
    a, b = _norm(a), _norm(b)
    if not a or not b: return 0.0
    if a == b: return 1.0
    sc = difflib.SequenceMatcher(None, a, b).ratio()
    if min(len(a), len(b)) >= 4 and (a in b or b in a): sc = max(sc, .88)
    return sc

def learn_endpoints(force=False):
    """Reads the site's JS bundles and collects the /api/... paths they call (cached for an hour)."""
    with LEARN_LOCK:
        warm = LEARNED["id_eps"] or LEARNED["static"]
        if not force and time.time() - LEARNED["ts"] < (3600 if warm else 600):
            return LEARNED
        LEARNED["ts"] = time.time()
        s = _session()
        id_eps, static, search, seen = [], [], [], set()
        def consider(path):
            path = path.strip().rstrip(".,;")
            if not path or path.endswith((".js", ".css", ".png", ".svg", ".jpg", ".webp")): return
            if _PLACE.search(path):
                if re.search(r"search|find|lookup|by-username|username", path, re.I): search.append(_PLACE.sub("{q}", path))
                else: id_eps.append(_PLACE.sub("{id}", path))
            else: static.append(path)
        try:
            html = s.get(BASE + "/", timeout=15).text
            queue = [urljoin(BASE + "/", m) for m in re.findall(r"""<script[^>]+src=["']([^"']+)["']""", html)]
            queue += [urljoin(BASE + "/", m) for m in re.findall(r"""<link[^>]+href=["']([^"']+\.js)["']""", html)]
            while queue and len(seen) < 24:
                u = queue.pop(0)
                if u in seen or urlparse(u).netloc != urlparse(BASE).netloc: continue
                seen.add(u)
                try: js = s.get(u, timeout=20).text
                except requests.RequestException: continue
                for m in _API_STR.findall(js): consider(m)
                for m in _API_CAT.finditer(js): consider(m[1] + "{id}" + (m[2] or ""))
                for m in re.findall(r"""[`"'](\./[\w.-]+\.js|/assets/[\w./-]+\.js)[`"']""", js): queue.append(urljoin(u, m))
        except requests.RequestException:
            pass
        uniq = lambda l: list(dict.fromkeys(l))
        LEARNED.update(id_eps=uniq(id_eps), static=uniq(static), search=uniq(search), scripts=len(seen))
        print(f"[tracker] learned {len(LEARNED['id_eps'])} id-based, {len(LEARNED['static'])} static, {len(LEARNED['search'])} search API paths from {len(seen)} script(s)")
        return LEARNED

def people_from(j, out=None, d=0, inherit=""):
    """Walks any JSON and returns {id, name, code} for every object that looks like a user."""
    out = [] if out is None else out
    if d > 6: return out
    if isinstance(j, list):
        for o in j[:800]: people_from(o, out, d + 1, inherit)
    elif isinstance(j, dict):
        uid = j.get("id", j.get("user_id", j.get("userId")))
        nm = j.get("username") or j.get("display_name") or j.get("displayName") or j.get("name")
        code = next((str(v) for k, v in j.items() if isinstance(v, (str, int)) and re.search(r"(star|creator|support).*code|^code$", str(k), re.I)), "") or inherit
        if uid is not None and nm and str(uid).isdigit() and not isinstance(nm, (dict, list)):
            out.append({"id": str(uid), "name": unent(str(nm)), "code": code})
        for v in j.values():
            if isinstance(v, (list, dict)): people_from(v, out, d + 1, code)
    return out

def people_from_html(html, skip=""):
    out, seen = [], set()
    for pid, inner in _LINK.findall(html):
        if pid == str(skip) or pid in seen: continue
        alt = re.search(r"""(?:alt|title)=["']([^"']+)["']""", inner)
        txt = unent(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner)).strip())
        nm = re.sub(r"^@", "", txt or (unent(alt[1]) if alt else ""))
        if not nm or len(nm) > 40 or nm.lower() in ("profile", "view", "view profile"): continue
        seen.add(pid); out.append((pid, nm))
    return out

def scrape_list_pages(s, uid, lab):
    """Fallback when no JSON API answers: read the list straight off the site's own page."""
    got, seen = [], set()
    for path in (f"/users/{uid}/{lab}", f"/users/{uid}/profile/{lab}"):
        for page in range(1, 6):
            try: r = s.get(BASE + path + (f"?page={page}" if page > 1 else ""), timeout=15)
            except requests.RequestException: break
            if r.status_code != 200: break
            if "json" in r.headers.get("Content-Type", ""):
                try: new = [(p["id"], p["name"]) for p in people_from(r.json()) if p["id"] != str(uid) and p["id"] not in seen]
                except ValueError: new = []
            else:
                new = [p for p in people_from_html(r.text, uid) if p[0] not in seen]
            if not new: break
            for p in new: seen.add(p[0]); got.append(p)
            time.sleep(0.2)
        if got: break
    return got

def fill_missing_lists(s, uid, data, got, found):
    """Called after the normal ENDPOINTS pass: tries learned API paths, then HTML pages, for lists still missing."""
    missing = [l for l in LABELS if l not in got]
    if not missing: return
    L = learn_endpoints()
    tries = 0
    for ep in L["id_eps"]:
        lab = next((l for l in LABELS if l in ep.lower()), None)
        if lab is None or lab in got or tries > 60: continue
        if EP_FAIL.get(ep, 0) >= 3 and ep not in EP_OK: continue
        tries += 1
        try:
            r = s.get(BASE + ep.replace("{id}", str(uid)), timeout=15)
            if r.status_code != 200:
                EP_FAIL[ep] = EP_FAIL.get(ep, 0) + 1; continue
            payload = r.json()
        except (requests.RequestException, ValueError):
            EP_FAIL[ep] = EP_FAIL.get(ep, 0) + 1; continue
        EP_OK.add(ep); got.add(lab); found.append(ep + " (learned)")
        data.update(flatten(payload, lab)); time.sleep(0.2)
    for lab in ("followers", "following", "friends"):
        if lab in got: continue
        people = scrape_list_pages(s, uid, lab)
        if people:
            got.add(lab); found.append(f"/users/{uid}/{lab} (page scrape)")
            for i, (pid, nm) in enumerate(people):
                data[f"{lab}.{i}.id"], data[f"{lab}.{i}.username"] = pid, nm

# ----- star codes: find the content creators and match them to their codes -----
STAR_STATE = {"running": False, "last": 0.0, "log": [], "matched": {}}
CREATOR_EPS = ["/api/users/content-creators", "/api/content-creators", "/api/creators", "/api/star-codes", "/api/starcodes",
               "/api/star_codes", "/api/creator-codes", "/api/support-a-creator", "/api/users/creators",
               "/api/users?role=content_creator", "/api/users?role=creator", "/api/users?badge=content-creator",
               "/api/users?is_content_creator=true", "/api/badges/content-creator/users"]
CREATOR_PAGES = ["/creators", "/content-creators", "/star-codes", "/starcodes", "/support-a-creator", "/leaderboard"]

def creator_candidates():
    """Everyone the site lists as a content creator / star-code holder (JSON endpoints first, then HTML pages)."""
    s, out, log = _session(), {}, []
    L = learn_endpoints()
    eps = CREATOR_EPS + [p for p in L["static"] if re.search(r"creator|star|code|partner|featured|verified", p, re.I)]
    for ep in dict.fromkeys(eps):
        try:
            r = s.get(BASE + ep, timeout=15)
            if r.status_code != 200: continue
            found = people_from(r.json())
        except (requests.RequestException, ValueError):
            continue
        log.append(f"{ep}: {len(found)} people")
        for p in found: out.setdefault(p["id"], p)
    for pg in CREATOR_PAGES:
        try: r = s.get(BASE + pg, timeout=15)
        except requests.RequestException: continue
        if r.status_code != 200 or "json" in r.headers.get("Content-Type", ""): continue
        found = people_from_html(r.text)
        log.append(f"{pg}: {len(found)} people (page scrape)")
        for pid, nm in found: out.setdefault(pid, {"id": pid, "name": nm, "code": ""})
    STAR_STATE["log"] = log
    return list(out.values())

def match_star(name, code, cands):
    best = None
    for c in cands:
        sc = _sim(name, c["name"])
        if c.get("code") and _norm(c["code"]) == _norm(code): sc = max(sc, .99)
        elif len(_norm(code)) >= 4: sc = max(sc, _sim(code, c["name"]) * .9)
        if not best or sc > best[0]: best = (sc, c)
    return best[1] if best and best[0] >= .7 else None

def profile_name(s, uid):
    try:
        t = re.search(r"<title>(.*?)</title>", s.get(f"{BASE}/users/{uid}/profile", timeout=15).text, re.S)
        return unent(t[1]).replace("- Vortex", "").strip() if t else None
    except requests.RequestException:
        return None

def save_star(uid, code, name):
    link_star(uid, code)
    with LOCK:
        c = db()
        c.execute("INSERT OR REPLACE INTO starnames VALUES (?,?)", (uid, name))
        c.commit(); c.close()

def resolve_stars(force=False):
    """Matches every star code to a real Vortex account (creator lists first, then exact-name search) and stores it."""
    if STAR_STATE["running"] or (not force and time.time() - STAR_STATE["last"] < 300): return
    STAR_STATE["running"] = True
    try:
        with LOCK:
            c = db(); linked = {r[0] for r in c.execute("SELECT code FROM starlinks")}; c.close()
        todo = [(n, cd) for n, cd in STAR_CODES if force or cd.lower() not in linked]
        if not todo: return
        cands, s = creator_candidates(), _session()
        print(f"[tracker] star codes: {len(todo)} to match against {len(cands)} content creators")
        for name, code in todo:
            hit = match_star(name, code, cands)
            how = "creator list"
            if not hit:
                how = "name search"
                res = find_users(name)["results"]
                hit = next((r for r in res if _norm(r["name"]) == _norm(name)), None)
            if not hit: continue
            nm = hit["name"] or profile_name(s, hit["id"]) or name
            save_star(hit["id"], code, nm)
            STAR_STATE["matched"][code] = {"id": hit["id"], "name": nm, "via": how}
            print(f"[tracker] star code {code} -> {nm} (id {hit['id']}, via {how})")
    except Exception:
        traceback.print_exc()
    finally:
        STAR_STATE["last"] = time.time(); STAR_STATE["running"] = False

def probe(uid):
    """Tries every endpoint and reports status + shape, so you can see which paths the site really answers."""
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (personal stat tracker)"
    out = []
    for ep, label in ENDPOINTS:
        url = BASE + ep.format(id=uid)
        info = {"label": label, "url": url}
        try:
            r = s.get(url, timeout=15)
            info["status"] = r.status_code
            if r.status_code == 200:
                try:
                    j = r.json()
                    info["shape"] = ("object with keys: " + ", ".join(list(j)[:12])) if isinstance(j, dict) else f"list of {len(j)}"
                except ValueError:
                    info["shape"] = "not JSON"
        except requests.RequestException as e:
            info["status"] = str(e)[:80]
        out.append(info)
    return out

def _sniff(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n": return "image/png"
    if b[:3] == b"\xff\xd8\xff": return "image/jpeg"
    if b[:6] in (b"GIF87a", b"GIF89a"): return "image/gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "image/webp"
    if b"<svg" in b[:400]: return "image/svg+xml"
    return None

IMG_CACHE = {}
def fetch_img(url):
    """Fetch a playvortex.io image server-side (with a proper Referer) so hotlink blocks don't break it."""
    pu = urlparse(url)
    if pu.scheme not in ("http", "https") or pu.netloc != urlparse(BASE).netloc:
        return None
    hit = IMG_CACHE.get(url)
    if hit and time.time() - hit[0] < (3600 if hit[1] else 60):
        return hit[1]
    res = None
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Referer": BASE + "/", "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
        ct = r.headers.get("Content-Type", "").split(";")[0].strip()
        kind = ct if ct.startswith("image/") else _sniff(r.content)  # some hosts send images as octet-stream
        if r.status_code == 200 and r.content and kind:
            res = (kind, r.content)
    except requests.RequestException:
        pass
    if len(IMG_CACHE) > 600:
        IMG_CACHE.clear()
    IMG_CACHE[url] = (time.time(), res)
    return res

def discover():
    """Reads the site's own JS and lists path-like strings about images, to find the real image URLs."""
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (personal stat tracker)"
    html = s.get(BASE + "/", timeout=15).text
    queue = [urljoin(BASE + "/", m) for m in re.findall(r'<script[^>]+src="([^"]+)"', html)]
    seen, found = set(), set()
    while queue and len(seen) < 16:
        u = queue.pop(0)
        if u in seen: continue
        seen.add(u)
        try: js = s.get(u, timeout=20).text
        except requests.RequestException: continue
        for m in re.findall(r"""["'`]((?:/|https?://)[^"'`\s]{3,140})["'`]""", js):
            if re.search(r"thumbnail|icon|picture|avatar|asset|logo|group", m, re.I): found.add(m)
        for m in re.findall(r"""["'`](\./[\w.-]+\.js|/assets/[\w./-]+\.js)["'`]""", js):
            queue.append(urljoin(u, m))
    return {"scripts_read": sorted(seen), "image_related_strings": sorted(found)[:400]}

# One tiny adapter so the same code runs on SQLite (local) and Postgres/Supabase (deployed).
PG_COLS = {"latest": (["uid", "key"], ["uid", "key", "value"]), "starlinks": (["code"], ["code", "uid"]), "starnames": (["uid"], ["uid", "name"])}
DDL = ["CREATE TABLE IF NOT EXISTS history (uid TEXT, ts TEXT, key TEXT, old TEXT, value TEXT)",
       "CREATE TABLE IF NOT EXISTS latest (uid TEXT, key TEXT, value TEXT, PRIMARY KEY(uid,key))",
       "CREATE TABLE IF NOT EXISTS samples (uid TEXT, ts TEXT, key TEXT, value REAL)",
       "CREATE TABLE IF NOT EXISTS searches (uid TEXT, ts TEXT)",
       "CREATE TABLE IF NOT EXISTS starlinks (code TEXT PRIMARY KEY, uid TEXT)",
       "CREATE TABLE IF NOT EXISTS starnames (uid TEXT PRIMARY KEY, name TEXT)",
       "CREATE INDEX IF NOT EXISTS samples_k ON samples (uid, key, ts)"]
_DB_READY = False

class _Conn:
    def __init__(self, raw, pg): self.raw, self.pg = raw, pg
    def execute(self, sql, params=None):
        if not self.pg:
            return self.raw.execute(sql, params or ())
        m = re.match(r"\s*INSERT OR REPLACE INTO (\w+) VALUES", sql, re.I)
        if m:
            pk, cols = PG_COLS[m[1]]
            sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in pk)
            sql = re.sub(r"INSERT OR REPLACE INTO", "INSERT INTO", sql, flags=re.I) + f" ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {sets}"
        sql = sql.replace(" REAL)", " DOUBLE PRECISION)")
        cur = self.raw.cursor()
        if params is None: cur.execute(sql)
        else: cur.execute(sql.replace("%", "%%").replace("?", "%s"), params)
        return cur
    def commit(self): self.raw.commit()
    def close(self): self.raw.close()

def db():
    global _DB_READY
    if DATABASE_URL:
        import psycopg2
        kw = {} if "sslmode=" in DATABASE_URL else {"sslmode": "require"}
        c = _Conn(psycopg2.connect(DATABASE_URL, connect_timeout=15, **kw), True)
    else:
        c = _Conn(sqlite3.connect(DB_PATH, timeout=30), False)
    if not _DB_READY:
        for q in DDL: c.execute(q)
        if c.pg:  # keep Supabase's public Data API from exposing these tables (this app connects as the owner, which bypasses RLS)
            for t in ("history", "latest", "samples", "searches", "starlinks", "starnames"):
                c.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        c.commit(); _DB_READY = True
    return c

def note_search(uid):
    with LOCK:
        c = db()
        c.execute("INSERT INTO searches VALUES (?,?)", (uid, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        c.commit(); c.close()

def _faces(c):
    """Players seen in tracked friend/follower lists, for the homepage background."""
    pat = re.compile(r"^(friends|followers|following)\.(?:[A-Za-z_]+\.)?(\d+)\.(.+)$")
    ent = {}
    for uid, key, val in c.execute("SELECT uid, key, value FROM latest WHERE key LIKE 'friends.%' OR key LIKE 'followers.%' OR key LIKE 'following.%'").fetchall():
        m = pat.match(key)
        if m: ent.setdefault((uid, m[1], m[2]), {})[m[3].lower()] = val
    out, seen = [], set()
    for o in ent.values():
        pid = next((o[k] for k in ("userid", "user_id", "friendid", "friend_id", "friend.id", "user.id", "id") if k in o), None)
        nm = next((o[k] for k in ("username", "displayname", "display_name", "name") if k in o), "")
        pid = str(pid) if pid is not None else ""
        if pid.isdigit() and pid not in seen:
            seen.add(pid); out.append({"id": pid, "name": nm})
    random.shuffle(out)
    return out[:60]

def home():
    """Recently searched + most searched users, for the homepage."""
    with LOCK:
        c = db()
        def name_of(u):
            r = c.execute("SELECT value FROM latest WHERE uid=? AND key='meta.name'", (u,)).fetchone()
            if not r: r = c.execute("SELECT name FROM starnames WHERE uid=?", (u,)).fetchone()
            return r[0] if r else None
        recent = [{"id": u, "name": name_of(u), "ts": ts} for u, ts in
                  c.execute("SELECT uid, MAX(ts) m FROM searches GROUP BY uid ORDER BY m DESC LIMIT 12").fetchall()]
        popular = [{"id": u, "name": name_of(u), "count": n} for u, n in
                   c.execute("SELECT uid, COUNT(*) n FROM searches GROUP BY uid ORDER BY n DESC, MAX(ts) DESC LIMIT 12").fetchall()]
        stars = {code: {"uid": u, "name": name_of(u)} for code, u in c.execute("SELECT code, uid FROM starlinks").fetchall()}
        faces = _faces(c)
        c.close()
    return {"recent": recent, "popular": popular, "stars": stars, "faces": faces, "resolving": STAR_STATE["running"]}

def link_star(uid, code):
    with LOCK:
        c = db()
        c.execute("INSERT OR REPLACE INTO starlinks VALUES (?,?)", (code[:40].lower(), uid))
        c.commit(); c.close()

# Guesses for a "find a user by name" endpoint. First one that returns users wins.
FIND_ENDPOINTS = ["/api/users/search?q={q}", "/api/users/search?query={q}", "/api/users?search={q}", "/api/users?q={q}",
                  "/api/search/users?q={q}", "/api/search?q={q}&type=users", "/api/users/by-username/{q}",
                  "/api/users/username/{q}", "/api/users/lookup?username={q}"]

def find_users(name):
    learn_endpoints()
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (personal stat tracker)"
    def one(o, out):
        if isinstance(o, dict):
            uid = o.get("id", o.get("user_id", o.get("userId")))
            nm = o.get("username") or o.get("display_name") or o.get("displayName") or o.get("name")
            if uid is not None and nm: out.append({"id": str(uid), "name": str(nm)})
    def collect(j):
        out = []
        if isinstance(j, list):
            for o in j[:30]: one(o, out)
        elif isinstance(j, dict):
            one(j, out)
            for v in j.values():
                if isinstance(v, list):
                    for o in v[:30]: one(o, out)
                elif isinstance(v, dict): one(v, out)
        return out
    results, tried = [], []
    for ep in list(LEARNED["search"]) + FIND_ENDPOINTS:
        try: r = s.get(BASE + ep.replace("{q}", quote(name)), timeout=15)
        except requests.RequestException: continue
        tried.append([ep, r.status_code])
        if r.status_code != 200: continue
        try: res = collect(r.json())
        except ValueError: continue
        if res:
            results = res; break
    return {"results": results, "tried": tried}

def track(uid):
    data, found = fetch_snapshot(uid)
    with LOCK:
        return _save(uid, data, found)

# Numbers worth graphing: profile counts, list totals, visits, and per-game / total live numbers.
SAMPLE = re.compile(r"^(meta\.(friends|followers)|(?:[A-Za-z_]+\.)+count|[A-Za-z_]+\.visits|agg\..+)$")

def _sample(c, uid, ts, data):
    vals = {}
    for k, v in data.items():
        if SAMPLE.match(k):
            try: vals[k] = float(v)
            except (TypeError, ValueError): pass
    games = {}
    for k, v in data.items():
        m = re.match(r"created\.(\d+)\.(id|visits|active)$", k)
        if m: games.setdefault(m[1], {})[m[2]] = v
    tv = ta = 0.0
    for g in games.values():
        try:
            vis, act = float(g.get("visits") or 0), float(g.get("active") or 0)
        except (TypeError, ValueError):
            continue
        tv += vis; ta += act
        if g.get("id") is not None:
            vals[f"agg.game.{g['id']}.visits"] = vis
            vals[f"agg.game.{g['id']}.active"] = act
    if "created.count" in data:
        vals["agg.visits"], vals["agg.active"] = tv, ta
    now = datetime.fromisoformat(ts)
    for k, v in vals.items():
        last = c.execute("SELECT ts, value FROM samples WHERE uid=? AND key=? ORDER BY ts DESC LIMIT 1", (uid, k)).fetchone()
        # store on change, or at most every 5 minutes so flat stretches still draw a line
        if last and last[1] == v and (now - datetime.fromisoformat(last[0])).total_seconds() < 300:
            continue
        c.execute("INSERT INTO samples VALUES (?,?,?,?)", (uid, ts, k, v))

def series(uid, key):
    key = key[:120]
    with LOCK:
        c = db()
        rows = c.execute("SELECT ts, value FROM samples WHERE uid=? AND key=? ORDER BY ts DESC LIMIT 600", (uid, key)).fetchall()
        hist = c.execute("SELECT ts, value FROM history WHERE uid=? AND key=? ORDER BY ts DESC LIMIT 200", (uid, key)).fetchall()
        c.close()
    pts = {}
    for ts, v in hist:
        try: pts[ts] = float(v)
        except (TypeError, ValueError): pass
    for ts, v in rows:
        pts[ts] = float(v)
    return [{"ts": t, "value": pts[t]} for t in sorted(pts)]

def _save(uid, data, found):
    c = db()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    old = dict(c.execute("SELECT key, value FROM latest WHERE uid=?", (uid,)))
    first = not old
    n = 0
    for k, v in data.items():
        v = v if isinstance(v, str) else json.dumps(v)
        if old.get(k) != v:
            if not first and not NOISY.search(k):
                c.execute("INSERT INTO history VALUES (?,?,?,?,?)", (uid, ts, k, old.get(k), v))
                n += 1
            c.execute("INSERT OR REPLACE INTO latest VALUES (?,?,?)", (uid, k, v))
    # Drop list entries that disappeared (e.g. an unfriended user) from sections we just fetched fine.
    secs = {k.split(".")[0] for k in data}
    for k in old:
        if k not in data and k.split(".")[0] in secs and re.search(r"\.\d+(\.|$)", k):
            c.execute("DELETE FROM latest WHERE uid=? AND key=?", (uid, k))
    _sample(c, uid, ts, data)
    c.commit()
    stats = dict(c.execute("SELECT key, value FROM latest WHERE uid=?", (uid,)))
    c.close()
    return {"stats": stats, "endpoints": found, "changes": n, "ts": ts}

def history(uid):
    with LOCK:
        c = db()
        rows = c.execute("SELECT ts,key,old,value FROM history WHERE uid=? ORDER BY ts DESC LIMIT 200", (uid,)).fetchall()
        c.close()
    return [{"ts": a, "key": b, "old": o, "value": v} for a, b, o, v in rows]

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Vortex Tracker</title>
<script>try{document.documentElement.dataset.theme=localStorage.getItem('vt-theme')||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light')}catch(e){document.documentElement.dataset.theme='light'}</script>
<style>
:root{--bg:#faf7ff;--card:#fff;--ink:#241238;--mut:#7c6a96;--line:#eadcfa;--pl:#f5edff;--pm:#e9d5ff;--acc:#a855f7;--acc-h:#9333ea;--pt:#7e22ce;--topbg:rgba(255,255,255,.88);--okbg:#dcfce7;--okfg:#15803d;--badbg:#fee2e2;--badfg:#b91c1c;--offbg:#f1f5f9;--offfg:#64748b;--shadow:0 1px 2px rgba(60,20,100,.04)}
[data-theme=dark]{--bg:#0e0916;--card:#171024;--ink:#f3eafe;--mut:#a695c4;--line:#2b1e42;--pl:#231636;--pm:#3a2560;--acc:#a855f7;--acc-h:#b878fa;--pt:#d8b4fe;--topbg:rgba(14,9,22,.85);--okbg:rgba(34,197,94,.15);--okfg:#4ade80;--badbg:rgba(239,68,68,.15);--badfg:#f87171;--offbg:#241a35;--offfg:#a695c4;--shadow:none}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;background:var(--bg);color:var(--ink);transition:background .2s,color .2s}
i.i{display:inline-block;width:1em;height:1em;background:currentColor;-webkit-mask:var(--m) center/contain no-repeat;mask:var(--m) center/contain no-repeat;flex:none;vertical-align:-.125em}
.top{border-bottom:1px solid var(--line);background:var(--topbg);backdrop-filter:blur(10px);position:sticky;top:0;z-index:10}
.top .in{max-width:1180px;margin:0 auto;padding:12px 24px;display:flex;align-items:center;gap:14px}
.brand{display:flex;align-items:center;gap:10px;font-weight:800;font-size:18px;color:var(--pt)}
.logo{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,#a855f7,#7e22ce);display:grid;place-items:center;color:#fff;font-size:18px}
.search{margin-left:auto;display:flex;gap:8px}
input[type=text]{border:1px solid var(--line);border-radius:10px;padding:9px 12px;font-size:14px;width:120px;outline:none;color:var(--ink);background:var(--card)}
input[type=text]:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--pm)}
button{display:inline-flex;align-items:center;gap:7px;border:0;border-radius:10px;padding:9px 16px;font-size:14px;font-weight:600;cursor:pointer;background:var(--acc);color:#fff;transition:background .15s}
button:hover{background:var(--acc-h)}button.soft{background:var(--pl);color:var(--pt)}button.soft:hover{background:var(--pm)}
button.icon{padding:9px 11px;font-size:17px}
.wrap{max-width:1180px;margin:0 auto;padding:22px 24px 80px}
.card{background:var(--card);border:1px solid var(--line);border-radius:22px;box-shadow:var(--shadow)}
.clk{cursor:pointer;transition:border-color .15s,transform .15s}.clk:hover{border-color:var(--acc);transform:translateY(-1px)}
.tb{display:flex;align-items:center;justify-content:flex-end;gap:16px;margin-bottom:16px;flex-wrap:wrap}
.sw{display:flex;align-items:center;gap:8px;color:var(--mut);font-size:13px;cursor:pointer;user-select:none}
.sw input{accent-color:var(--acc);width:16px;height:16px}
.upd{color:var(--mut);font-size:13px;display:flex;gap:6px;align-items:center}
.layout{display:grid;grid-template-columns:340px minmax(0,1fr);gap:22px;align-items:start}
.side,.main{display:flex;flex-direction:column;gap:16px;min-width:0}
.prof{padding:22px}
.pr{display:flex;gap:16px;align-items:center}
.av{position:relative;width:88px;height:88px;flex:none}
.av img{width:88px;height:88px;object-fit:cover;border-radius:22px;background:var(--pl);border:2px solid var(--pm)}
.dot{position:absolute;right:-4px;bottom:-4px;width:18px;height:18px;border-radius:50%;background:#22c55e;border:3px solid var(--card)}
.who{min-width:0}.who h1{margin:0;font-size:24px;letter-spacing:-.4px;word-break:break-word;line-height:1.15}
.rl{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-top:8px}
.rb{position:relative;display:inline-grid;place-items:center;width:26px;height:26px;font-size:24px;cursor:default}
.rb img{width:26px;height:26px;object-fit:contain}
.rb[data-tip]:hover::after{content:attr(data-tip);position:absolute;bottom:calc(100% + 6px);left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);font-size:12px;font-weight:600;padding:4px 9px;border-radius:8px;white-space:nowrap;z-index:5;pointer-events:none}
.idn{color:var(--mut);font-size:13px;margin-top:6px}
.bio{margin:16px 0 0;line-height:1.55;color:var(--ink);white-space:pre-wrap;font-size:14.5px}
.tags{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}
.chip{display:inline-flex;align-items:center;gap:7px;background:var(--pl);color:var(--pt);padding:6px 12px;border-radius:99px;font-size:13px;font-weight:600}
.chip.on{background:var(--okbg);color:var(--okfg)}.chip.off{background:var(--offbg);color:var(--offfg)}.chip.bad{background:var(--badbg);color:var(--badfg)}
.strip{display:grid;grid-template-columns:repeat(3,1fr);overflow:hidden}
.cell{position:relative;padding:15px 6px;text-align:center;border-left:1px solid var(--line);cursor:pointer;transition:background .15s}
.cell:first-child{border-left:0}.cell:hover{background:var(--pl)}
.cell b{display:block;font-size:21px;letter-spacing:-.5px}.cell span{font-size:12px;color:var(--mut);font-weight:500}
.cell .dl{top:6px;right:6px;font-size:10px;padding:1px 6px}
.blk{padding:18px 20px}
.bh{display:flex;align-items:center;gap:9px;font-weight:700;font-size:15px;margin-bottom:12px}
.bh i{color:var(--acc);font-size:18px}
.rows .row{display:flex;justify-content:space-between;gap:14px;padding:11px 0;border-top:1px solid var(--line);font-size:14px}
.rows .row:first-child{border-top:0}.rows .row span{color:var(--mut)}.rows .row b{font-weight:600;text-align:right}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px}
.stat{padding:18px;position:relative}
.si{width:36px;height:36px;border-radius:12px;background:var(--pl);color:var(--pt);display:grid;place-items:center;font-size:19px;margin-bottom:14px}
.sv{font-size:30px;font-weight:800;letter-spacing:-1px;line-height:1}
.sl{color:var(--mut);font-size:13px;margin-top:6px;font-weight:500}
.gi{position:absolute;right:14px;bottom:14px;color:var(--mut);opacity:.5;font-size:16px}.clk:hover .gi{opacity:1;color:var(--acc)}
.dl{position:absolute;top:16px;right:16px;font-size:12px;font-weight:700;padding:3px 8px;border-radius:99px}
.dl.up{background:var(--okbg);color:var(--okfg)}.dl.down{background:var(--badbg);color:var(--badfg)}
section{scroll-margin-top:76px}
.sh{display:flex;align-items:center;gap:10px;margin:8px 0 14px;flex-wrap:wrap}
.sh h2{display:flex;align-items:center;gap:10px;font-size:19px;margin:0;letter-spacing:-.2px}
.sh h2 i{color:var(--acc);font-size:21px}
.sh small{font-size:12px;font-weight:700;color:var(--pt);background:var(--pl);padding:2px 10px;border-radius:99px}
.sf{margin-left:auto;width:180px!important}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}
.th{position:relative;width:46px;height:46px;border-radius:13px;background:var(--pl);overflow:hidden;flex:none;display:grid;place-items:center;color:var(--pt);font-size:21px}
.th img{width:100%;height:100%;object-fit:cover;display:block}
.th .fb{display:none;place-items:center}.th.err .fb{display:grid}
.th.badge{background:transparent;overflow:visible;width:48px;height:48px}.th.badge img{object-fit:contain}
.th.badge.err{background:var(--pl);border-radius:14px}
.th.cover{width:100%;height:auto;aspect-ratio:16/9;border-radius:0;font-size:34px}
.item{display:flex;gap:14px;align-items:center;padding:14px}
.item .tx{min-width:0;flex:1}.item b{display:block;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item p{margin:3px 0 0;color:var(--mut);font-size:13px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.item .mt{margin-top:5px;color:var(--mut);font-size:12.5px}.item .mt b{display:inline;font-size:12.5px;color:var(--ink)}
.item .sm{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--mut);margin-top:3px}
.val{text-align:right;flex:none}.val b{font-size:16px;overflow:visible}.val span{display:block;font-size:11px;color:var(--mut);font-weight:600;letter-spacing:.3px}
.list1{display:flex;flex-direction:column;gap:10px}
.list1 .item{padding:10px}
.game{overflow:hidden}.game .bd{padding:14px 16px 16px}.game b{font-size:16px;display:block}
.game p{margin:5px 0 0;color:var(--mut);font-size:13px;line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:2.9em}
.gm{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
.gm span{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;font-weight:600;color:var(--pt);background:var(--pl);padding:5px 10px;border-radius:99px}
.gm span.live{background:var(--okbg);color:var(--okfg)}
.people{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.person{display:flex;align-items:center;gap:12px;padding:10px 12px;text-decoration:none;color:inherit;border-radius:16px}
.person .th{width:42px;height:42px;border-radius:50%;font-size:15px;font-weight:700}
.person b{font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.more{color:var(--mut);font-size:13px;margin:12px 2px 0}
.es{padding:38px 20px;text-align:center;display:flex;flex-direction:column;align-items:center;gap:6px}
.es .ei{width:48px;height:48px;border-radius:50%;background:var(--pl);color:var(--pt);display:grid;place-items:center;font-size:22px;margin-bottom:6px}
.es b{font-size:16px}.es p{margin:0;color:var(--mut);font-size:14px;line-height:1.5;max-width:420px}
.empty{color:var(--mut);font-size:14px;line-height:1.6;margin:0;padding:22px;text-align:center}
.tl{padding:6px 22px}
.chg{display:flex;align-items:center;gap:12px;padding:14px 0;border-top:1px solid var(--line);font-size:14px;line-height:1.4}
.chg:first-child{border-top:0}.chg .ico{width:34px;height:34px;border-radius:11px;background:var(--pl);color:var(--pt);display:grid;place-items:center;flex:none;font-size:17px}
.chg .up{color:var(--okfg);font-weight:700}.chg .down{color:var(--badfg);font-weight:700}.chg em{font-style:normal;color:var(--mut)}
.chg time{margin-left:auto;color:var(--mut);font-size:12px;white-space:nowrap;padding-left:10px}
.note{margin-top:10px;color:var(--mut);font-size:13px;line-height:1.5}
/* graph popup */
.ov{position:fixed;inset:0;background:rgba(10,5,20,.55);backdrop-filter:blur(4px);z-index:50;display:grid;place-items:center;padding:20px;overflow:auto}
.md{width:min(720px,100%);max-height:92vh;overflow:auto;padding:22px}
.mh{display:flex;align-items:center;gap:14px;margin-bottom:18px}
.mh h3{margin:0;font-size:20px;letter-spacing:-.3px}.mh p{margin:3px 0 0;color:var(--mut);font-size:13px}
.mh .mi{width:42px;height:42px;border-radius:13px;background:var(--pl);color:var(--pt);display:grid;place-items:center;font-size:22px;flex:none}
.mh button{margin-left:auto}
.gs+.gs{margin-top:22px;padding-top:20px;border-top:1px solid var(--line)}
.gh{display:flex;align-items:baseline;gap:10px;margin-bottom:10px}.gh span{color:var(--mut);font-weight:600;font-size:14px}.gh b{font-size:26px;letter-spacing:-.6px}
.sum{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.sum span{font-size:12.5px;font-weight:600;background:var(--pl);color:var(--pt);padding:4px 10px;border-radius:99px}
.sum span.up{background:var(--okbg);color:var(--okfg)}.sum span.down{background:var(--badbg);color:var(--badfg)}
.cw{position:relative}.cw svg{width:100%;height:auto;display:block;touch-action:none}
.cw .gl{stroke:var(--line);stroke-width:1}.cw .tx{fill:var(--mut);font-size:11px;font-family:inherit}
.cw .hv{stroke:var(--mut);stroke-width:1;stroke-dasharray:3 3;opacity:0}.cw .hd{stroke:var(--card);stroke-width:2.5;opacity:0}
.tip{position:absolute;top:0;transform:translateX(-50%);background:var(--ink);color:var(--bg);border-radius:10px;padding:6px 10px;font-size:12px;pointer-events:none;opacity:0;white-space:nowrap;display:flex;flex-direction:column;align-items:center;gap:1px}
.tip b{font-size:14px}
.car{position:relative}
.rail{display:flex;gap:14px;overflow-x:auto;scroll-snap-type:x proximity;padding:2px 2px 8px;scrollbar-width:none}
.rail::-webkit-scrollbar{display:none}
.fr{flex:none;width:112px;cursor:pointer;scroll-snap-align:start}
.fr .ph{position:relative;width:112px;height:112px;border-radius:18px;overflow:hidden;background:var(--pl);border:1px solid var(--line);transition:border-color .15s,transform .15s}
.fr:hover .ph{border-color:var(--acc);transform:translateY(-2px)}
.th.sq{width:100%;height:100%;border-radius:0;font-size:34px;font-weight:700}
.fr b{display:block;margin-top:8px;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.st{position:absolute;right:7px;bottom:7px;width:14px;height:14px;border-radius:50%;background:#64748b;border:3px solid var(--card)}.st.on{background:#22c55e}
.sh .nav{margin-left:auto;display:flex;gap:8px}
.own{font-weight:700;color:var(--acc)}
.item.clk{text-decoration:none;color:inherit}
.ggrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.ggrid.collapsed .xtra{display:none}
.gc{display:flex;flex-direction:column;gap:5px;padding:18px;text-decoration:none;color:inherit;min-width:0}
.gc .th{width:64px;height:64px;border-radius:16px;font-size:28px;margin-bottom:12px;background:var(--pl)}
.gc .th.err{background:var(--pl)}
.gc b.gn{font-size:17px;letter-spacing:-.2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gc .go{color:var(--mut);font-size:12.5px}
.gc p{margin:4px 0 0;color:var(--ink);font-size:13.5px;line-height:1.45;opacity:.85;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:2.9em}
.gc .gm2{margin-top:8px;color:var(--mut);font-size:13px}.gc .gm2 b{color:var(--ink)}
.bar{position:fixed;top:0;left:0;height:3px;width:0;background:linear-gradient(90deg,#a855f7,#ec4899);z-index:80;opacity:0;pointer-events:none}
.bar.on{opacity:1;width:80%;transition:width 8s cubic-bezier(.1,.7,.2,1)}
.ld{position:fixed;inset:0;z-index:70;background:color-mix(in srgb,var(--bg) 72%,transparent);backdrop-filter:blur(3px);display:none;place-content:center;justify-items:center;gap:14px}
.ld.on{display:grid}
.ld p{margin:0;font-weight:600}
.spin{width:46px;height:46px;border-radius:50%;border:4px solid var(--pm);border-top-color:var(--acc);animation:sp .8s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.hero{text-align:center;padding:36px 0 30px}
.hero .logo{width:56px;height:56px;border-radius:16px;font-size:30px;margin:0 auto}
.hero h1{font-size:34px;margin:16px 0 6px;letter-spacing:-1px}
.hero p{color:var(--mut);margin:0 0 22px}
.hsb{display:flex;gap:8px;justify-content:center}
.hsb input{width:min(320px,60vw);padding:12px 14px;font-size:15px}
.hs{display:block;color:var(--mut);font-size:12px;margin-top:2px}
.home{max-width:900px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
.th.pf{width:88px;height:88px;border-radius:22px;font-size:34px;font-weight:700}
.th img{object-position:50% 18%}.th.cover img,.th.badge img{object-position:center}
.os{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.oc{background:var(--pl);border-radius:14px;padding:10px 12px;min-width:0}
.oc span{display:block;font-size:11.5px;color:var(--mut);font-weight:500}
.oc b{display:block;font-size:15px;margin-top:3px;word-break:break-word;line-height:1.25}
.chg .bd2{flex:1;min-width:0}
.mini{padding:4px 10px;font-size:12px;border-radius:8px;margin-top:7px}
.dt{display:none;margin-top:10px;background:var(--pl);border-radius:12px;padding:10px 12px;font-size:13px}.dt.open{display:block}
.dt .dr{display:flex;justify-content:space-between;gap:12px;padding:5px 0;border-top:1px solid var(--line)}.dt .dr:first-child{border-top:0}
.dt .dk{color:var(--mut);flex:none}.dt .dv{text-align:right;word-break:break-word}
.dt .when{color:var(--mut);font-size:12px;margin-bottom:6px}
.sc{display:flex;align-items:center;justify-content:space-between;gap:10px;background:var(--pl);border-radius:14px;padding:12px 14px}
.sc b{font-size:24px;letter-spacing:.5px;color:var(--pt);word-break:break-all}
.sn{margin:10px 2px 0;color:var(--mut);font-size:12.5px}
.heroW{position:relative;overflow:hidden;width:100vw;margin-left:calc(50% - 50vw);min-height:440px;padding:84px 20px 60px;display:flex;align-items:center;justify-content:center}
.heroW::before{content:'';position:absolute;inset:0;z-index:1;pointer-events:none;background:radial-gradient(ellipse 52% 70% at 50% 50%,var(--bg) 22%,transparent 100%),linear-gradient(180deg,var(--bg),transparent 24%,transparent 76%,var(--bg))}
.mqw{position:absolute;inset:-60px -140px;display:flex;flex-direction:column;justify-content:center;gap:20px;opacity:.6;transform:rotate(-7deg);pointer-events:none;-webkit-mask-image:linear-gradient(90deg,transparent,#000 12%,#000 88%,transparent);mask-image:linear-gradient(90deg,transparent,#000 12%,#000 88%,transparent)}
.mqr{display:flex;gap:20px;width:max-content;animation:mq 90s linear infinite}
.mqr.rev{animation-direction:reverse;animation-duration:110s;margin-left:-70px}
@keyframes mq{to{transform:translateX(-50%)}}
.mtl{width:116px;height:116px;flex:none;border-radius:24px;overflow:hidden;background:var(--pl);border:1px solid var(--line);box-shadow:0 8px 22px rgba(30,10,60,.16)}
.mqw{opacity:.5}
.mtl .th{width:100%;height:100%;border-radius:0;font-size:36px}
.heroW .hero{position:relative;z-index:2;padding:0;width:100%}
.hero h1{font-size:38px}
.hsb{display:flex;align-items:center;gap:10px;max-width:560px;margin:0 auto;padding:6px 6px 6px 18px;border-radius:18px;border:1px solid var(--line);background:color-mix(in srgb,var(--card) 82%,transparent);backdrop-filter:blur(10px);box-shadow:0 10px 30px rgba(60,20,100,.12);transition:border-color .15s,box-shadow .15s}
.hsb:focus-within{border-color:var(--acc);box-shadow:0 0 0 3px var(--pm),0 10px 30px rgba(60,20,100,.12)}
.hsb>i{color:var(--mut);font-size:19px}
.hsb input{flex:1;min-width:0;width:auto;border:0;background:transparent;box-shadow:none!important;padding:10px 4px;font-size:16px;border-radius:0}
.hsb kbd{font:600 12px/1 inherit;color:var(--mut);border:1px solid var(--line);border-radius:7px;padding:4px 8px;background:var(--pl)}
.dim{opacity:.62}
.starcard{width:calc(100vw - 32px);margin-left:calc(50% - 50vw + 16px);background:var(--card);border:1px solid var(--line);border-radius:26px;padding:26px 0 20px;overflow:hidden;box-shadow:var(--shadow)}
.starcard .mqs{width:100%;margin-left:0;padding:0}
body{overflow-x:hidden}
.mqs{width:100vw;margin-left:calc(50% - 50vw);overflow:hidden;padding:12px 0 8px;-webkit-mask-image:linear-gradient(90deg,transparent,#000 12%,#000 88%,transparent);mask-image:linear-gradient(90deg,transparent,#000 12%,#000 88%,transparent)}
.mqs .mqr{gap:24px;animation-duration:70s}.mqs .mqr.rev{animation-duration:85s;margin-left:0}
.mqs:hover .mqr{animation-play-state:paused}
.rt{width:96px;flex:none;text-align:center;cursor:pointer;opacity:.82;transition:opacity .15s,transform .15s}
.rt:hover{opacity:1;transform:translateY(-3px)}
.rt .th{width:96px;height:96px;border-radius:24px;font-size:34px;box-shadow:0 8px 22px rgba(30,10,60,.18)}
.rt b{display:block;font-size:13px;margin-top:9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rt span{display:block;font-size:11.5px;color:var(--mut);margin-top:1px}
.chips{display:flex;gap:6px;justify-content:center;flex-wrap:wrap;margin-top:14px}
.chip{background:transparent;color:var(--mut);padding:8px 14px;border-radius:12px;font-weight:600}
.chip:hover{background:var(--pl);color:var(--pt)}
@media(max-width:560px){.hsb kbd{display:none}.hero h1{font-size:30px}.mtl{width:88px;height:88px;border-radius:20px}}
@media(prefers-reduced-motion:reduce){.mqr{animation:none}}
@media(max-width:940px){.layout{grid-template-columns:1fr}.top .in{padding:10px 16px}.wrap{padding:18px 16px 70px}}
</style></head><body>
<div class="top"><div class="in"><div class="brand" onclick="showHome()" style="cursor:pointer" title="Home"><div class="logo" id="lg"></div>Vortex Tracker</div>
<div class="search"><input type="text" id="uid" placeholder="User ID"><button onclick="search()" id="go"></button><button class="soft icon" id="th" onclick="toggleTheme()" title="Switch light / dark"></button></div></div></div>
<div class="bar" id="bar"></div><div class="ld" id="ld"><div class="spin"></div><p id="ldt">Loading…</p></div>
<div class="wrap" id="out"></div>
<script>
const $=id=>document.getElementById(id);let timer=null,busy=false,UID='',GR={},MID=null,STACK=[],CURN='',GOPEN=false,FOPEN=false,ACTOPEN=new Set();
const BASE='https://playvortex.io';
/* Icons: Tabler set (one of the libraries on Iconstack), drawn as masks so they follow the theme colors. */
const ic=n=>`<i class="i" style="--m:url(https://api.iconify.design/tabler/${n}.svg)"></i>`;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num=v=>(v!=null&&/^-?\d+$/.test(String(v))&&String(v).length<16)?+v:undefined;
const fmt=n=>n===undefined?'—':n.toLocaleString();
const fmtB=n=>n===undefined?'—':Math.abs(n)>=100000?new Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:1}).format(n):n.toLocaleString();
const dstr=t=>new Date(t).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
const words=p=>String(p).replace(/([a-z0-9])([A-Z])/g,'$1 $2').replace(/[_-]+/g,' ').trim().toLowerCase().replace(/^./,c=>c.toUpperCase());
const abs=u=>!u?'':/^https?:/.test(u)?u:u.startsWith('//')?'https:'+u:BASE+(u.startsWith('/')?'':'/')+u;
const ok=v=>v!=null&&v!==''&&v!=='null';
const NOISY=k=>/\.active$/.test(k);

function applyTheme(t){document.documentElement.dataset.theme=t;$('th').innerHTML=ic(t==='dark'?'sun':'moon')}
function toggleTheme(){const t=document.documentElement.dataset.theme==='dark'?'light':'dark';try{localStorage.setItem('vt-theme',t)}catch(e){}applyTheme(t)}
$('lg').innerHTML=ic('bolt');$('go').innerHTML=ic('search')+'Track';applyTheme(document.documentElement.dataset.theme||'light');

/* ---------- reading the stored data ---------- */
function items(s,sec){
  const re=new RegExp('^'+sec+'\\.((?:[A-Za-z_]+\\.)?)(\\d+)\\.(.+)$'),by={},order=[];
  for(const k in s){const m=k.match(re);if(!m)continue;const id=m[1]+m[2];if(!by[id]){by[id]={};order.push([+m[2],id])}by[id][m[3].toLowerCase()]=s[k]}
  return order.sort((a,b)=>a[0]-b[0]||(a[1]<b[1]?-1:1)).map(x=>by[x[1]]);
}
function ckey(s,sec){
  if(num(s[sec+'.count'])!==undefined)return sec+'.count';
  const re=new RegExp('^'+sec+'\\.[A-Za-z_]+\\.count$');
  return Object.keys(s).find(k=>re.test(k)&&num(s[k])!==undefined);
}
const countOf=(s,sec)=>{const k=ckey(s,sec);return k?num(s[k]):undefined};
function pick(o,names){
  for(const n of names)if(ok(o[n]))return o[n];
  for(const n of names)for(const k in o)if(k.endsWith('.'+n)&&ok(o[k]))return o[k];
}
const nameOf=o=>{const v=pick(o,['displayname','display_name','username','name','title','label']);return v&&!/^\d+$/.test(v)?v:undefined};
const idOf=o=>pick(o,['userid','user_id','id']);
const imgOf=o=>{for(const n of ['image','imageurl','image_url','icon','iconurl','icon_url','img','src','thumbnail','thumbnailurl','picture','avatar','avatarurl']){const v=pick(o,[n]);if(v&&/\/|\.(png|jpe?g|svg|webp|gif)/i.test(v))return abs(v)}return ''};
const initial=n=>esc((n||'?').trim().charAt(0).toUpperCase());

const px=u=>u&&u.startsWith(BASE+'/')?'/img?u='+encodeURIComponent(u):u;
function thumb(src,icon,cls,letter){
  const srcs=[].concat(src||[]).filter(Boolean).map(px);
  const fb=letter?`<span class="fb">${letter}</span>`:`<span class="fb">${ic(icon)}</span>`;
  const img=srcs.length?`<img src="${esc(srcs[0])}" data-alt="${esc(srcs.slice(1).join('|'))}" data-i="0" loading="lazy" referrerpolicy="no-referrer" onerror="gimg(this)">`:'';
  return `<div class="th ${cls||''} ${img?'':'err'}">${img}${fb}</div>`;
}
const anyImg=o=>{for(const k in o){const v=o[k];if(typeof v==='string'&&/(icon|image|img|thumb|logo|picture|avatar)/i.test(k)&&!/version|id$|count/i.test(k)&&/^(https?:)?\/|^[\w-]+\/[\w.\/-]+\.(png|jpe?g|webp|gif|svg)/i.test(v))return abs(v)}return ''};
function getBadges(s){
  const out=[],seen=new Set();
  for(const sec of ['profilebadges','badges'])for(const o of items(s,sec)){
    const img=imgOf(o);let name=nameOf(o);
    if(!name&&img)name=words(img.split('?')[0].split('/').pop().replace(/\.\w+$/,''));
    if(!name&&!img)continue;
    const key=(name||img).toLowerCase();if(seen.has(key))continue;seen.add(key);
    out.push({name:name||'Badge',img,desc:pick(o,['description','desc','details']),sub:pick(o,['game','gamename','game.name','awarder','source'])});
  }
  return out;
}
/* Yes/No account flags (is_staff, is_booster, …) become little icon badges next to the name. */
const RDEF={staff:['Staff','shield-half-filled','#f59e0b'],admin:['Admin','crown','#a855f7'],developer:['Developer','hammer','#94a3b8'],moderator:['Moderator','gavel','#38bdf8'],contentcreator:['Content Creator','video-filled','#ef4444'],booster:['Booster','star-filled','#ec4899'],verified:['Verified','rosette-discount-check','#3b82f6']};
const RALIAS={mod:'moderator',creator:'contentcreator',cc:'contentcreator',youtuber:'contentcreator'};
function getRoles(s,badges){
  const found=new Set();
  for(const k in s){
    let last=k.split('.').pop().toLowerCase().replace(/^is/,'').replace(/_/g,'');last=RALIAS[last]||last;
    if(s[k]==='true'&&RDEF[last])found.add(last);
    else if(last==='role'&&ok(s[k])&&isNaN(s[k])){const r=String(s[k]).toLowerCase().replace(/[^a-z]/g,'');if(RDEF[RALIAS[r]||r])found.add(RALIAS[r]||r)}
  }
  const have=badges.map(b=>b.name.toLowerCase()).join('|');
  return Object.keys(RDEF).filter(r=>found.has(r)).map(r=>({id:r,name:RDEF[r][0],icon:RDEF[r][1],color:RDEF[r][2]})).filter(r=>!have.includes(r.name.toLowerCase().split(' ')[0]));
}
function delta(h,keys){
  const x=h.find(e=>keys.includes(e.key)&&num(e.old)!==undefined&&num(e.value)!==undefined);
  if(!x)return '';const d=num(x.value)-num(x.old);if(!d)return '';
  return `<span class="dl ${d>0?'up':'down'}" title="Last change: ${esc(dstr(x.ts))}">${d>0?'+':''}${d.toLocaleString()}</span>`;
}

/* ---------- graph popup ---------- */
const reg=cfg=>{const id='g'+Object.keys(GR).length;GR[id]=cfg;return id};
function closeG(){if(MID){MID.remove();MID=null;document.body.style.overflow=''}}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeG()});
async function openG(id){
  const cfg=GR[id];if(!cfg)return;closeG();
  const ov=document.createElement('div');ov.className='ov';ov.onclick=e=>{if(e.target===ov)closeG()};
  ov.innerHTML=`<div class="md card"><div class="mh"><span class="mi">${ic(cfg.icon)}</span><div><h3>${esc(cfg.title)}</h3>${cfg.sub?`<p>${esc(cfg.sub)}</p>`:''}</div><button class="soft icon" onclick="closeG()" title="Close">${ic('x')}</button></div>
    ${cfg.sers.map((x,i)=>`<div class="gs"><div class="gh"><span>${esc(x.label)}</span><b>${fmt(x.cur)}</b></div><div class="ch" id="ch${i}"><p class="empty">Loading…</p></div></div>`).join('')}</div>`;
  document.body.appendChild(ov);document.body.style.overflow='hidden';MID=ov;
  await Promise.all(cfg.sers.map(async(x,i)=>{
    let pts=[];try{pts=await fetch(`/api/series?id=${UID}&key=${encodeURIComponent(x.key)}`).then(r=>r.json())}catch(e){}
    if(MID===ov)mountChart(ov.querySelector('#ch'+i),Array.isArray(pts)?pts:[],x.color||'#a855f7');
  }));
}
function mountChart(el,raw,color){
  const pts=raw.map(p=>({t:new Date(p.ts).getTime(),v:+p.value})).filter(p=>!isNaN(p.t)&&!isNaN(p.v));
  if(pts.length<2){el.innerHTML=`<p class="empty">${pts.length?`Only one reading so far (${fmt(Math.round(pts[0].v))}).`:'No readings yet.'}<br>The graph fills in as this number changes. The tracker checks again every minute.</p>`;return}
  const W=640,H=230,L=50,R=14,T=14,B=30;
  const vs=pts.map(p=>p.v);let mn=Math.min(...vs),mx=Math.max(...vs),lo=mn,hi=mx;
  if(lo===hi){lo-=1;hi+=1}else{const pad=(hi-lo)*.15;lo-=pad;hi+=pad}
  if(mn>=0)lo=Math.max(0,lo);
  const t0=pts[0].t,t1=pts[pts.length-1].t;
  const x=t=>L+(t1===t0?0:(t-t0)/(t1-t0))*(W-L-R),y=v=>T+(1-(v-lo)/(hi-lo))*(H-T-B);
  const line=pts.map((p,i)=>`${i?'L':'M'}${x(p.t).toFixed(1)} ${y(p.v).toFixed(1)}`).join(' ');
  const area=`${line} L${x(t1).toFixed(1)} ${H-B} L${x(t0).toFixed(1)} ${H-B} Z`;
  const gid='g'+Math.random().toString(36).slice(2,8);
  const grid=[0,1,2,3].map(i=>{const v=lo+(hi-lo)*i/3;return `<line class="gl" x1="${L}" x2="${W-R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/><text class="tx" x="${L-8}" y="${(y(v)+4).toFixed(1)}" text-anchor="end">${esc(new Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:1}).format(Math.round(v)))}</text>`}).join('');
  const dots=pts.length<=40?pts.map(p=>`<circle cx="${x(p.t).toFixed(1)}" cy="${y(p.v).toFixed(1)}" r="3" fill="${color}"/>`).join(''):'';
  const ch=vs[vs.length-1]-vs[0];
  el.innerHTML=`<div class="sum"><span class="${ch>0?'up':ch<0?'down':''}">${ch>0?'+':''}${fmt(Math.round(ch))} over this period</span><span>Peak ${fmt(Math.round(mx))}</span><span>Low ${fmt(Math.round(mn))}</span></div>
  <div class="cw"><svg viewBox="0 0 ${W} ${H}"><defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".35"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
  ${grid}<path d="${area}" fill="url(#${gid})"/><path d="${line}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>${dots}
  <text class="tx" x="${L}" y="${H-8}">${esc(dstr(t0))}</text><text class="tx" x="${W-R}" y="${H-8}" text-anchor="end">${esc(dstr(t1))}</text>
  <line class="hv" x1="0" x2="0" y1="${T}" y2="${H-B}"/><circle class="hd" r="5" fill="${color}" cx="0" cy="0"/></svg><div class="tip"></div></div>`;
  const svg=el.querySelector('svg'),tip=el.querySelector('.tip'),vl=el.querySelector('.hv'),dot=el.querySelector('.hd');
  svg.addEventListener('pointermove',e=>{
    const r=svg.getBoundingClientRect(),mx2=(e.clientX-r.left)/r.width*W;let b=pts[0],bd=1e9;
    for(const p of pts){const d=Math.abs(x(p.t)-mx2);if(d<bd){bd=d;b=p}}
    vl.setAttribute('x1',x(b.t));vl.setAttribute('x2',x(b.t));dot.setAttribute('cx',x(b.t));dot.setAttribute('cy',y(b.v));
    vl.style.opacity=1;dot.style.opacity=1;tip.style.opacity=1;
    tip.innerHTML=`<b>${fmt(Math.round(b.v))}</b><span>${esc(dstr(b.t))}</span>`;
    tip.style.left=Math.min(Math.max(x(b.t)/W*r.width,56),r.width-56)+'px';
  });
  svg.addEventListener('pointerleave',()=>{vl.style.opacity=0;dot.style.opacity=0;tip.style.opacity=0});
}

/* ---------- section builders ---------- */
const emptyState=(icon,title,text)=>`<div class="card es"><div class="ei">${ic(icon)}</div><b>${esc(title)}</b><p>${esc(text)}</p></div>`;
const uidOf=o=>pick(o,['userid','user_id','friendid','friend_id','friend.id','user.id','id']);
const cleanId=v=>v!=null&&/^\d+$/.test(String(v))?String(v):'';
const avatar=id=>id?[`${BASE}/api/users/${id}/avatar-picture?width=256&height=320&fit=cover`,`${BASE}/api/users/${id}/avatar-picture`,`${BASE}/api/users/${id}/avatar`,`${BASE}/api/users/${id}/thumbnail`]:[];
const NOLIST="The names couldn't be loaded from the site. The tracker already tried auto-discovering the API paths and scraping the page. Open /api/learn on this tracker to see what it found.";
async function goUser(id){if(!id||String(id)===String(UID))return;STACK.push({id:UID,name:CURN});$('uid').value=id;await load('nav');window.scrollTo({top:0,behavior:'smooth'})}
async function goBack(){const p=STACK.pop();if(!p)return;$('uid').value=p.id;await load();window.scrollTo({top:0,behavior:'smooth'})}
function viewAll(b){const r=$('rail'),g=$('fall');if(!r||!g)return;const open=g.style.display==='none';FOPEN=open;g.style.display=open?'grid':'none';r.style.display=open?'none':'flex';b.textContent=open?'Show less':'View all'}
function railGo(d){const r=$('rail');if(r)r.scrollBy({left:d*Math.max(240,r.clientWidth*.8),behavior:'smooth'})}
function personCard(o){
  const id=cleanId(uidOf(o)),name=nameOf(o)||(id?'User '+id:'Unknown');
  return `<div class="card person ${id?'clk':''}" data-n="${esc(name.toLowerCase())}" ${id?`onclick="goUser('${id}')"`:''}>${thumb(avatar(id),'user','',initial(name))}<b>${esc(name)}</b></div>`;
}
function friendTile(o){
  const id=cleanId(uidOf(o)),name=nameOf(o)||(id?'User '+id:'Unknown');
  const on=String(pick(o,['isonline','is_online','online'])||'')==='true';
  return `<div class="fr" ${id?`onclick="goUser('${id}')"`:''} title="Show ${esc(name)} stats"><div class="ph">${thumb(avatar(id),'user','sq',initial(name))}<span class="st ${on?'on':''}"></span></div><b>${esc(name)}</b></div>`;
}
function friendsHtml(l,tot){
  if(!l.length)return tot===0?emptyState('users','No friends yet','Nothing to show here.'):emptyState('users',tot?`${fmt(tot)} friends`:'Friends',tot?"The names weren't returned by the site. "+NOLIST:NOLIST);
  return `<div class="car"><div class="rail" id="rail" style="${FOPEN?'display:none':''}">${l.map(friendTile).join('')}</div></div><div class="people" id="fall" style="display:${FOPEN?'grid':'none'}">${l.map(personCard).join('')}</div>
    ${tot&&tot>l.length?`<p class="more">Showing ${fmt(l.length)} of ${fmt(tot)}</p>`:''}`;
}
function people(list,total,label,icon){
  if(!list.length)return total===0?emptyState(icon,`No ${label} yet`,'Nothing to show here.'):emptyState(icon,total?`${fmt(total)} ${label}`:esc(words(label)),NOLIST);
  return `<div class="people">${list.map(personCard).join('')}</div>
    ${total&&total>list.length?`<p class="more">Showing ${fmt(list.length)} of ${fmt(total)}</p>`:''}`;
}
const GROUP_URL=id=>`${BASE}/groups/${id}`; // change this if the site's group pages live somewhere else
function gimg(el){const l=(el.dataset.alt||'').split('|').filter(Boolean),n=+el.dataset.i||0;if(n<l.length){el.dataset.i=n+1;el.src=l[n]}else{el.parentNode.classList.add('err');el.remove()}}
function toggleG(b){const g=b.closest('section').querySelector('.ggrid');if(!g)return;const c=g.classList.toggle('collapsed');GOPEN=!c;b.textContent=c?`Show all (${b.dataset.n})`:'Show less'}
function groupsHtml(l,total){
  if(!l.length)return total===0?emptyState('users-group','Not in any groups','Nothing to show here.'):emptyState('users-group',total?`${fmt(total)} in total`:'Groups',NOLIST);
  const rows=l.map(o=>{
    const id=cleanId(pick(o,['groupid','group_id','group.id','id'])),name=nameOf(o)||(id?'#'+id:'Unnamed');
    const oid=pick(o,['ownerid','owner_id','owner.id','owner.userid','owner']),role=String(pick(o,['role','role.name','rolename'])||'').toLowerCase();
    const own=role==='owner'||(oid!==undefined&&String(oid)===String(UID));
    const on=pick(o,['owner.username','owner.name','owner.displayname','ownername','owner_name']);
    const imgs=[imgOf(o)||anyImg(o)].concat(id?['icon-picture?width=256&height=256&fit=cover','icon-picture','group-picture','picture','icon','thumbnail','icon.png'].map(x=>`${BASE}/api/groups/${id}/${x}`).concat([`${BASE}/assets/groups/${id}.png`,`${BASE}/assets/group-icons/${id}.png`,`${BASE}/assets/icons/${id}.png`,`${BASE}/assets/groups/${id}.jpg`,`${BASE}/assets/groups/${id}.webp`,`${BASE}/assets/groups/${id}/icon.png`]):[]).filter(Boolean);
    const rank=pick(o,['rank','rolerank','role.rank']),rn=pick(o,['rolename','role.name','role']);
    return {id,name,own,on:on&&isNaN(on)?on:'',imgs,desc:pick(o,['description','desc'])||'',mem:num(pick(o,['membercount','member_count','members.count','members'])),rank:ok(rank)?rank:(ok(rn)&&isNaN(rn)?rn:'')};
  }).sort((a,b)=>(b.mem??-1)-(a.mem??-1));
  return `<div class="ggrid ${rows.length>4&&!GOPEN?'collapsed':''}">${rows.map((g,i)=>{
    const tag=g.id?'a':'div',at=g.id?` href="${esc(GROUP_URL(g.id))}" target="_blank" rel="noopener"`:'';
    const th=thumb(g.imgs,'users-group');
    const sub=g.own?`Owner${g.on?' · @'+esc(g.on):''}`:(g.on?`Owner · @${esc(g.on)}`:'');
    return `<${tag} class="card gc ${g.id?'clk':''} ${i>=4?'xtra':''}" data-n="${esc(g.name.toLowerCase())}"${at}>${th}<b class="gn">${esc(g.name)}</b>${sub?`<div class="go">${sub}</div>`:''}${g.desc?`<p>${esc(g.desc)}</p>`:''}<div class="gm2">${g.mem!==undefined?`<b>${fmtB(g.mem)}</b> members`:''}${g.rank!==''?`${g.mem!==undefined?' · ':''}Rank ${esc(g.rank)}`:''}</div></${tag}>`}).join('')}</div>`;
}
function plain(list,total,o2){
  const {fb,none,kind}=o2;
  if(!list.length)return total===0?emptyState(fb,none,'Nothing to show here.'):emptyState(fb,total?`${fmt(total)} in total`:words(kind==='group'?'groups':'list'),NOLIST);
  return `<div class="grid">${list.map(o=>{
    const id=kind==='group'?pick(o,['groupid','group_id','group.id','id']):idOf(o),name=nameOf(o)||(id?'#'+id:'Unnamed'),img=imgOf(o);
    let sub='',meta='',val='',href='';
    if(kind==='group'){
      const oid=pick(o,['ownerid','owner_id','owner.id','owner.userid','owner']),role=String(pick(o,['role','role.name','rolename'])||'').toLowerCase();
      const mem=num(pick(o,['membercount','member_count','members.count','members'])),rank=pick(o,['rank','rolerank','role.rank']);
      const own=role==='owner'||(oid!==undefined&&String(oid)===String(UID));
      meta=[mem!==undefined?`<b>${fmtB(mem)}</b> members`:'',own?'<span class="own">Owner</span>':'',ok(rank)&&!own&&isNaN(rank)?esc(rank):''].filter(Boolean).join(' · ');
      if(cleanId(id))href=GROUP_URL(cleanId(id));
    }else if(kind==='inventory'){
      sub=id?`Asset ${id}`:pick(o,['type','category'])||'';
      for(const [k,l] of [['rap','RAP'],['price','PRICE'],['value','VALUE']]){const v=num(pick(o,[k]));if(v!==undefined){val=`<div class="val"><b>${fmtB(v)}</b><span>${l}</span></div>`;break}}
    }else sub=pick(o,['description','type','category'])||'';
    const tag=href?'a':'div',attrs=href?` href="${esc(href)}" target="_blank" rel="noopener"`:'';
    return `<${tag} class="card item ${href?'clk':''}" data-n="${esc(name.toLowerCase())}"${attrs}>${thumb(img,fb)}<div class="tx"><b>${esc(name)}</b>${sub?`<p class="${kind==='inventory'?'sm':''}">${esc(sub)}</p>`:''}${meta?`<div class="mt">${meta}</div>`:''}</div>${val}</${tag}>`}).join('')}</div>
    ${total&&total>list.length?`<p class="more">Showing ${fmt(list.length)} of ${fmt(total)}</p>`:''}`;
}
function badgeRows(list){
  return `<div class="list1">${list.map(b=>`<div class="card item" data-n="${esc(b.name.toLowerCase())}">${thumb(b.img,'medal','badge')}<div class="tx"><b>${esc(b.name)}</b>${b.sub?`<p>${esc(b.sub)}</p>`:''}${b.desc?`<p>${esc(b.desc)}</p>`:''}</div></div>`).join('')}</div>`;
}
function gamesHtml(games){
  if(!games.length)return emptyState('device-gamepad-2','No games yet','This user hasn\'t published any games.');
  return `<div class="grid">${games.map(g=>{
    const id=reg({title:g.name||'Untitled game',icon:'device-gamepad-2',sub:g.desc,sers:[{label:'Total visits',key:`agg.game.${g.id}.visits`,color:'#6366f1',cur:g.visits},{label:'Playing now',key:`agg.game.${g.id}.active`,color:'#22c55e',cur:g.active}]});
    return `<div class="card game clk" data-n="${esc((g.name||'').toLowerCase())}" onclick="openG('${id}')">${thumb([`${BASE}/assets/thumbnails/${g.id}.png${g.v?'?v='+g.v:''}`,`${BASE}/assets/thumbnails/${g.id}.png`,`${BASE}/assets/thumbnails/${g.id}.jpg`,`${BASE}/assets/thumbnails/${g.id}.webp`,`${BASE}/api/games/${g.id}/thumbnail`],'device-gamepad-2','cover')}
    <div class="bd"><b>${esc(g.name||'Untitled')}</b>${g.desc?`<p>${esc(g.desc)}</p>`:'<p></p>'}
    <div class="gm"><span>${ic('eye')}${fmtB(g.visits)} visits</span><span class="${g.active?'live':''}">${ic('activity')}${fmt(g.active)} playing</span></div></div></div>`}).join('')}</div>`;
}
function section(id,title,icon,count,body,searchable,ph,extra){
  return `<section id="sec-${id}"><div class="sh"><h2>${ic(icon)}${title}</h2>${count!=null?`<small>${fmtB(count)}</small>`:''}${searchable?`<input type="text" class="sf" placeholder="${esc(ph||'Search')}" oninput="filt(this)">`:''}${extra||''}</div>${body}</section>`;
}
function filt(el){const q=el.value.trim().toLowerCase();if(q)el.closest('section,.blk').querySelectorAll('.collapsed').forEach(x=>{x.classList.remove('collapsed');const b=x.closest('section').querySelector('button[data-n]');if(b)b.textContent='Show less'});el.closest('section,.blk').querySelectorAll('[data-n]').forEach(c=>c.style.display=c.dataset.n.includes(q)?'':'none')}

/* ---------- activity feed ---------- */
const NONSTAT=k=>/(^|_)(id|uuid|version|hash|token|url|avatar|image|img|thumbnail|slug|visits)s?$/i.test(k.split('.').pop());
const REDUN=(k,v)=>/(^|[._])(follow|friendship)_?status$/i.test(k)||/(online|presence)_?status$/i.test(k)||/(follower|following|friend)s?_?count$/i.test(k)||/created_?at$/i.test(k)||(/previous_?usernames?(_?count)?$/i.test(k)&&String(v)==='0');
const clean=l=>{l=l.replace(/^(User|Profile) /,'').replace(/ count$/,'');return l.charAt(0).toUpperCase()+l.slice(1)};
const STARS=[['QuintupleOne','11111'],['2Tone','2Tone'],['Gaming','Gaming'],['Kaazmaz','Kaaz'],['Keyhitt3','Key'],['Kingvaylo','Vaylo'],['MiningRoger','Roger'],['OllieVortex','OllieXD'],['OneSpruce','Spruce'],['Owenyahhhh','Owen'],['Shiv','Shiv'],['Superhappywars','Super'],['TheBlockMan','Block'],['Xotgamez','Xot'],['Yk.itslux','Lux'],['Kostas','Kostas']];
const norm=x=>String(x||'').toLowerCase().replace(/[^a-z0-9]/g,'');
function lev(a,b){const m=a.length,n=b.length;if(!m||!n)return Math.max(m,n);let p=Array.from({length:n+1},(_,j)=>j);for(let i=1;i<=m;i++){const c=[i];for(let j=1;j<=n;j++)c[j]=Math.min(p[j]+1,c[j-1]+1,p[j-1]+(a[i-1]===b[j-1]?0:1));p=c}return p[n]}
const sim=(a,b)=>{if(!a||!b)return 0;if(a===b)return 1;let sc=1-lev(a,b)/Math.max(a.length,b.length);if(Math.min(a.length,b.length)>=4&&(a.includes(b)||b.includes(a)))sc=Math.max(sc,.88);return sc};
function findStar(names,bio,isCC){
  const m=String(bio||'').match(/star\s*-?\s*code\W{0,4}([A-Za-z0-9]+)/i);
  if(m){const e=STARS.find(x=>norm(x[1])===norm(m[1]));if(e)return {c:e,why:'Mentioned in their bio',exact:true}}
  let best=null;
  for(const n of names.map(norm).filter(Boolean))for(const e of STARS){
    let sc=sim(n,norm(e[0]));if(isCC){sc=Math.max(sc,sim(n,norm(e[1]))*.95);const cd=norm(e[1]);if(cd.length>=3&&n.includes(cd))sc=Math.max(sc,.7)}
    if(!best||sc>best.sc)best={c:e,sc,exact:norm(e[0])===n};
  }
  if(best&&best.sc>=(isCC?.6:.9))return {c:best.c,why:best.exact?'':'Closest match to this username',exact:best.exact};
  return null;
}
const SAVED=new Set();
function saveStar(code,uid){const k=code+'|'+uid;if(SAVED.has(k))return;SAVED.add(k);(window.STARLINKS=window.STARLINKS||{})[code.toLowerCase()]={uid,name:CURN};fetch(`/api/starlink?id=${uid}&code=${encodeURIComponent(code)}`).catch(()=>{})}
function copyCode(c,b){try{navigator.clipboard.writeText(c)}catch(e){}const o=b.textContent;b.textContent='Copied';setTimeout(()=>b.textContent=o,1200)}
const SECN={friends:['Friends','users'],followers:['Followers','user-check'],following:['Following','user-plus'],badges:['Badges','award'],profilebadges:['Badges','award'],created:['Games','device-gamepad-2'],groups:['Groups','users-group'],clans:['Clans','shield'],inventory:['Inventory','package']};
function activity(h,s){
  const isCount=k=>/\.count$|^meta\.(friends|followers)$/.test(k);
  const NAMEK=/\.(username|name|displayname|display_name|title)$/i;
  const by=new Map();
  for(const x of h){
    if(x.key==='meta.description'||x.key==='meta.title'||x.key==='meta.last_online'||NOISY(x.key)||NONSTAT(x.key)&&!/^created\.\d+\.visits$/.test(x.key))continue;
    const sec=x.key.split('.')[0],gm=x.key.match(/^created\.(\d+)\./),gk=x.ts+'|'+sec+(gm?'|'+gm[1]:'');
    if(!by.has(gk))by.set(gk,{id:gk,ts:x.ts,sec,idx:gm?gm[1]:null,items:[]});
    by.get(gk).items.push(x);
  }
  const rowsOf=its=>its.slice(0,30).map(x=>`<div class="dr" title="${esc(x.key)}"><span class="dk">${esc(words(x.key.replace(/\./g,' ')))}</span><span class="dv">${esc(x.old??'—')} <em>→</em> <b>${esc(x.value)}</b></span></div>`).join('')+(its.length>30?`<div class="dr"><span class="dk">+${its.length-30} more</span></div>`:'');
  const rows=[];
  for(const g of by.values()){
    const [nm,icon]=SECN[g.sec]||[words(g.sec),'list'];
    const its=g.items,isList=x=>/\.\d+(\.|$)/.test(x.key);
    let summary='',ic2=icon,extra='',raw=its;
    if(g.sec==='created'){
      const gname=s['created.'+g.idx+'.name']||'A game',v=its.find(x=>/\.visits$/.test(x.key)&&num(x.old)!==undefined&&num(x.value)>num(x.old));
      if(v){summary=`<b>${esc(gname)}</b> got <span class="up">+${fmt(num(v.value)-num(v.old))}</span> visits`;ic2='eye';extra=`<div class="dr"><span class="dk">Total visits</span><span class="dv"><b>${fmt(num(v.value))}</b></span></div>`}
      else summary=`<b>${esc(gname)}</b> was updated`;
    }else{
      const cx=its.find(x=>isCount(x.key)&&num(x.old)!==undefined&&num(x.value)!==undefined&&num(x.value)!==num(x.old));
      if(cx){const d=num(cx.value)-num(cx.old);summary=`<b>${esc(nm)}</b> ${d>0?'increased':'decreased'} by <span class="${d>0?'up':'down'}">${fmt(Math.abs(d))}</span>`;extra=`<div class="dr"><span class="dk">${esc(nm)}</span><span class="dv">${fmt(num(cx.old))} <em>→</em> <b>${fmt(num(cx.value))}</b></span></div>`}
      else if(its.some(x=>x.key==='meta.name')){summary='Display name changed';ic2='user'}
      else if(its.some(isList)){summary=`<b>${esc(nm)}</b> list was updated`}
      else summary=its.length===1?`<b>${esc(words(its[0].key.replace(/\./g,' ')))}</b> changed`:`<b>${esc(nm)}</b> stats changed`;
      if(its.some(isList)){
        const nw=new Set(its.filter(x=>NAMEK.test(x.key)&&ok(x.value)).map(x=>x.value)),od=new Set(its.filter(x=>NAMEK.test(x.key)&&ok(x.old)).map(x=>x.old));
        const add=[...nw].filter(v=>!od.has(v)),rem=[...od].filter(v=>!nw.has(v));
        if(add.length)extra+=`<div class="dr"><span class="dk">Added</span><span class="dv">${esc(add.slice(0,12).join(', '))}${add.length>12?` +${add.length-12} more`:''}</span></div>`;
        if(rem.length)extra+=`<div class="dr"><span class="dk">Removed</span><span class="dv">${esc(rem.slice(0,12).join(', '))}${rem.length>12?` +${rem.length-12} more`:''}</span></div>`;
        if(add.length||rem.length)raw=its.filter(x=>!isList(x)&&!isCount(x.key));
      }else raw=its.filter(x=>!isCount(x.key)||!extra);
      if(g.sec==='created')raw=[];
    }
    if(g.sec==='created')raw=its.filter(x=>!/\.visits$/.test(x.key));
    rows.push({id:g.id,ts:g.ts,icon:ic2,html:summary,detail:`<div class="when">${esc(new Date(g.ts).toLocaleString())}</div>${extra}${rowsOf(raw)}`});
  }
  return rows.slice(0,30);
}
function toggleD(b){const d=b.parentNode.querySelector('.dt'),on=d.classList.toggle('open'),id=b.dataset.id;on?ACTOPEN.add(id):ACTOPEN.delete(id);b.textContent=on?'Show less':'Show more'}

/* ---------- page ---------- */
let LDT=null;
function loading(on,msg){
  clearTimeout(LDT);const ld=$('ld'),bar=$('bar');
  if(on){bar.style.width='';bar.classList.remove('on');void bar.offsetWidth;bar.classList.add('on');LDT=setTimeout(()=>{$('ldt').textContent=msg||'Loading…';ld.classList.add('on')},120)}
  else{ld.classList.remove('on');bar.style.width='100%';setTimeout(()=>{bar.classList.remove('on');bar.style.width=''},250)}
}
const ago=ts=>{const m=Math.max(0,Math.round((Date.now()-new Date(ts))/6e4));return m<1?'just now':m<60?m+' min ago':m<1440?Math.round(m/60)+' h ago':Math.round(m/1440)+' d ago'};
function search(){STACK=[];return load('search')}
function openUser(id){$('uid').value=id;return search()}
function starCards(){
  const rows=STARS.map((e,i)=>{const lk=(window.STARLINKS||{})[e[1].toLowerCase()];return {e,i,lk,id:lk?cleanId(lk.uid):''}});
  rows.sort((a,b)=>(!!b.id)-(!!a.id));
  const tile=({e,i,lk,id})=>{const nm=(lk&&lk.name)||e[0];
    return `<div class="rt ${id?'':'dim'}" onclick="openCreator(${i})" title="${esc(nm)}">${thumb(avatar(id),'user','sq',initial(nm))}<b>${esc(nm)}</b><span>${id?'':(window.STARBUSY?'Matching… · ':'')}Code: ${esc(e[1])}</span></div>`};
  let a=[];while(a.length<14)a=a.concat(rows);const t=a.map(tile).join('');
  return `<div id="starGrid"><div class="starcard"><div class="mqs"><div class="mqr">${t}${t}</div></div></div></div>`;
}
async function pollStars(){
  if(Object.keys(window.STARLINKS||{}).length>=STARS.length)return;
  window.STARBUSY=true;fetch('/api/resolve').catch(()=>{});
  for(let n=0;n<10;n++){
    await new Promise(r=>setTimeout(r,4000));
    if(!$('starGrid'))return;
    try{const r=await fetch('/api/home').then(r=>r.json());window.STARLINKS=r.stars||{};
      if(!r.resolving&&n>=1)window.STARBUSY=false;
      $('starGrid').outerHTML=starCards();
      if(!window.STARBUSY||Object.keys(window.STARLINKS).length>=STARS.length)return}catch(e){}
  }
  window.STARBUSY=false;if($('starGrid'))$('starGrid').outerHTML=starCards();
}
async function findUser(name){let r=null;try{r=await fetch('/api/find?name='+encodeURIComponent(name)).then(r=>r.json())}catch(e){}
  const l=(r&&r.results)||[];return l.find(u=>norm(u.name)===norm(name))||(l.length===1?l[0]:null)}
async function openCreator(i){
  const e=STARS[i],lk=(window.STARLINKS||{})[e[1].toLowerCase()];
  if(lk)return openUser(lk.uid);
  loading(true,`Looking for ${e[0]}…`);const hit=await findUser(e[0]);loading(false);
  let id=hit&&cleanId(hit.id);
  if(!id){id=cleanId((prompt(`Couldn't find ${e[0]} automatically. If you know their user ID, enter it and it will be remembered:`)||'').trim())}
  if(!id)return;
  await fetch(`/api/starlink?id=${id}&code=${encodeURIComponent(e[1])}`).catch(()=>{});
  (window.STARLINKS=window.STARLINKS||{})[e[1].toLowerCase()]={uid:id};
  return openUser(id);
}
async function homeGo(){
  const v=$('hq').value.trim();
  if(v&&!/^\d+$/.test(v)){
    const i=STARS.findIndex(e=>norm(e[0])===norm(v)||norm(e[1])===norm(v));if(i>=0)return openCreator(i);
    loading(true,`Looking for ${v}…`);const hit=await findUser(v);loading(false);
    if(hit)return openUser(hit.id);
    alert(`Couldn't find "${v}". Try their user ID instead.`);return;
  }
  $('uid').value=v;return search()
}
function marquee(faces){
  const seen=new Set(),u=[];for(const f of faces){const id=cleanId(f&&f.id);if(id&&!seen.has(id)){seen.add(id);u.push({id,name:f.name||''})}}
  const list=u.length?u:Array.from({length:12},()=>({id:'',name:''}));
  const A=list.length>=6?list.filter((_,i)=>i%2===0):list,B=list.length>=6?list.filter((_,i)=>i%2===1):list.slice().reverse();
  const row=(l,cls)=>{let a=[];while(a.length<24)a=a.concat(l);const tiles=a.map(f=>`<div class="mtl">${f.id?thumb(avatar(f.id),'user','sq',initial(f.name)):thumb('','user','','')}</div>`).join('');return `<div class="mqr ${cls}">${tiles}${tiles}</div>`};
  return `<div class="mqw" aria-hidden="true">${row(A,'')}${row(B,'rev')}</div>`;
}
function jump(id){const e=$('sec-'+id);if(e)e.scrollIntoView({behavior:'smooth',block:'start'})}
document.addEventListener('keydown',e=>{if(e.key==='/'&&!/INPUT|TEXTAREA/.test((document.activeElement||{}).tagName||'')){const h=$('hq');if(h){e.preventDefault();h.focus()}}});
function homeCards(list,sub,none){
  if(!list.length)return emptyState('clock',none[0],none[1]);
  return `<div class="people">${list.map(u=>{const id=cleanId(u.id),nm=u.name||('User '+id);
    return `<div class="card person clk" onclick="openUser('${id}')">${thumb(avatar(id),'user','',initial(nm))}<div style="min-width:0"><b style="display:block">${esc(nm)}</b><span class="hs">${esc(sub(u))}</span></div></div>`}).join('')}</div>`;
}
async function showHome(){
  clearInterval(timer);timer=null;UID='';STACK=[];closeG();$('uid').value='';
  loading(true,'Loading home…');
  let d={recent:[],popular:[],stars:{}};
  try{const r=await fetch('/api/home').then(r=>r.json());if(r&&Array.isArray(r.recent))d=r}catch(e){}
  window.STARLINKS=d.stars||{};
  const faces=[...Object.values(d.stars||{}).map(x=>({id:x.uid,name:x.name})),...d.recent,...d.popular,...(d.faces||[])];
  $('out').innerHTML=`<div class="home"><div class="heroW">${marquee(faces)}<div class="hero"><div class="logo">${ic('bolt')}</div><h1>Vortex Tracker</h1><p>Look up any player and watch their stats change over time.</p>
    <div class="hsb">${ic('search')}<input type="text" id="hq" placeholder="try a User ID, username or star code" onkeydown="if(event.key==='Enter')homeGo()"><kbd>/</kbd><button onclick="homeGo()">Track</button></div>
    <div class="chips"><button class="chip" onclick="jump('recent')">${ic('clock')}Recent</button><button class="chip" onclick="jump('popular')">${ic('flame')}Popular</button><button class="chip" onclick="jump('stars')">${ic('star')}Star codes</button></div></div></div>
    ${section('recent','Recently searched','clock',null,homeCards(d.recent,u=>ago(u.ts),['No searches yet','Players you look up will show up here.']))}
    ${section('popular','Popular users','flame',null,homeCards(d.popular,u=>`${u.count} search${u.count===1?'':'es'}`,['Nobody is popular yet','The most searched players will show up here.']))}
    ${section('stars','Star code creators','star',STARS.length,starCards(),false)}</div>`;
  loading(false);pollStars();
}
async function load(mode){
  const uid=(mode==='auto'?UID:$('uid').value).replace(/\D/g,'');
  if(!uid)return mode==='auto'?undefined:showHome();
  if(busy)return;busy=true;
  const silent=mode==='auto';
  if(!silent)loading(true,'Loading profile…');
  try{
    const a=await fetch('/api/track?id='+uid+(mode==='search'||mode==='nav'?'&q=1':'')).then(r=>r.json());if(a.error)throw new Error(a.error);
    const h=await fetch('/api/history?id='+uid).then(r=>r.json());if(h.error)throw new Error(h.error);
    const y=window.scrollY;render(uid,a,h);startAuto();if(silent)window.scrollTo(0,y);
  }catch(e){if(!silent)$('out').innerHTML=`<div class="card"><p class="empty"><b>Couldn't load stats</b><br>${esc(e.message)}</p></div>`}
  busy=false;if(!silent)loading(false);
}
function render(uid,a,h){
  if(uid!==UID){GOPEN=false;FOPEN=false;ACTOPEN=new Set()}UID=uid;GR={};closeG();
  const s=a.stats;
  const name=s['meta.name']||('User '+uid);CURN=name;
  const cKey=Object.keys(s).find(k=>/(^|\.)created_?at$/i.test(k)&&!/^created\./.test(k)&&ok(s[k]));
  if(!s['meta.joined']&&cKey){const d=new Date(s[cKey]);if(!isNaN(d))s['meta.joined']=d.toLocaleDateString('en-US',{month:'long',day:'numeric',year:'numeric'})}
  const bio=(s['meta.description']||'').replace(/^.*?Joined [A-Za-z]+ \d+, \d{4}\s*/,'').trim();
  const onKey=Object.keys(s).find(k=>/(^|\.)(is)?online$/i.test(k)&&(s[k]==='true'||s[k]==='false'))||Object.keys(s).find(k=>/online_?status$/i.test(k)&&/^(online|offline)$/i.test(s[k]));
  const online=onKey&&/^(true|online)$/i.test(s[onKey]);
  const deleted=Object.keys(s).some(k=>/(^|\.)is_?deleted$/i.test(k)&&s[k]==='true');
  const badges=getBadges(s),roles=getRoles(s,badges);
  const hasGames=s['created.count']!==undefined;
  const games=items(s,'created').map(o=>({id:o.id,name:o.name,desc:o.description,v:o.thumbnail_version,active:num(o.active)||0,visits:num(o.visits)||0}));
  const sum=k=>games.reduce((t,g)=>t+g[k],0);
  const gk=keys=>keys.find(k=>k&&num(s[k])!==undefined);
  const fk=n=>Object.keys(s).find(k=>new RegExp('(^|\\.)'+n+'_?count$','i').test(k)&&num(s[k])!==undefined);
  let followingKey=ckey(s,'following')||Object.keys(s).find(k=>/following_?(count)?$/i.test(k)&&num(s[k])!==undefined);
  const K={friends:gk(['meta.friends',ckey(s,'friends'),fk('friend')]),followers:gk(['meta.followers',ckey(s,'followers'),fk('follower')]),following:followingKey,badges:gk([ckey(s,'badges'),ckey(s,'profilebadges')]),groups:ckey(s,'groups'),clans:ckey(s,'clans'),inventory:ckey(s,'inventory'),games:hasGames?'created.count':undefined,pvisits:Object.keys(s).find(k=>/^[A-Za-z_]+\.visits$/.test(k)&&num(s[k])!==undefined)};
  const V=k=>K[k]?num(s[K[k]]):undefined;
  const cnt={friends:V('friends'),followers:V('followers'),following:V('following'),groups:V('groups'),clans:V('clans'),inventory:V('inventory'),badges:badges.length||V('badges')};
  const col={friends:'#a855f7',followers:'#ec4899',following:'#38bdf8',badges:'#f59e0b',groups:'#6366f1',clans:'#14b8a6',inventory:'#f97316',games:'#22c55e',pvisits:'#6366f1'};
  const graph=(key,label,icon,cur,sub)=>K[key]?reg({title:label,icon,sub:sub||'History of this number',sers:[{label,key:K[key],color:col[key],cur}]}):'';

  /* sidebar: profile */
  const roleIcons=roles.map(r=>`<span class="rb" data-tip="${esc(r.name)}" style="color:${r.color}">${ic(r.icon)}</span>`).join('')
    +badges.filter(b=>b.img).slice(0,6).map(b=>`<span class="rb" data-tip="${esc(b.name)}"><img src="${esc(b.img)}" alt="${esc(b.name)}" onerror="this.parentNode.style.display='none'"></span>`).join('');
  let member='';
  if(s['meta.joined']){const d=new Date(s['meta.joined']),days=isNaN(d)?null:Math.floor((Date.now()-d)/864e5);if(days!=null&&days>=0)member=days.toLocaleString()+' days'}
  const cell=(k,l)=>{const gid=graph(k,l,k==='friends'?'users':k==='followers'?'user-check':'user-plus',cnt[k]);return `<div class="cell" ${gid?`onclick="openG('${gid}')"`:''}>${delta(h,[K[k]])}<b>${fmtB(cnt[k])}</b><span>${l}</span></div>`};
  const NOTSTAT=new Set(['friends','followers','following','groups','clans','inventory','badges','profilebadges','created','games']);
  const used=new Set(['meta.name','meta.description','meta.joined','meta.friends','meta.followers']);
  Object.values(K).forEach(v=>v&&used.add(v));if(onKey)used.add(onKey);
  for(const k in s){let last=k.split('.').pop().toLowerCase().replace(/^is/,'').replace(/_/g,'');last=RALIAS[last]||last;
    if(/(^|\.)is_?deleted$/i.test(k)||(s[k]==='true'&&RDEF[last])||last==='role')used.add(k)}
  const loKey=Object.keys(s).find(k=>/last_?(online|seen|active|login)/i.test(k.split('.').pop())&&ok(s[k]));
  if(loKey)used.add(loKey);
  let lastChip='';
  if(loKey&&!online){const v=String(s[loKey]);let d=/^\d{13}$/.test(v)?new Date(+v):/^\d{10}$/.test(v)?new Date(+v*1000):new Date(v);
    lastChip=`<span class="chip" title="${esc(isNaN(d)?v:d.toLocaleString())}">${ic('clock')}Last online ${esc(isNaN(d)?v:ago(d))}</span>`}
  const fv=v=>{v=String(v);if(v==='true')return 'Yes';if(v==='false')return 'No';
    if(/^\d{4}-\d\d-\d\dT/.test(v)){const d=new Date(v);if(!isNaN(d))return d.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'})}
    if(/^-?\d+$/.test(v)&&v.length<16)return fmtB(+v);if(/^[a-z]+(_[a-z]+)+$/.test(v))return words(v);return v.length>48?v.slice(0,46)+'…':v};
  const others=Object.keys(s).filter(k=>!used.has(k)&&ok(s[k])&&s[k]!==uid&&!/\.\d+(\.|$)/.test(k)&&!(/\.count$/.test(k)&&NOTSTAT.has(k.split('.')[0]))&&!/^(meta\.joined|agg\.)/.test(k)&&!NONSTAT(k)&&s[k]!==name&&!/^(https?:)?\//.test(String(s[k]))&&!REDUN(k,s[k])&&String(s[k]).length<60)
    .map(k=>({k,l:clean(words(k.replace(/\./g,' '))),v:fv(s[k])})).sort((a,b)=>a.l<b.l?-1:1);
  const otherHtml=others.length?`<div class="card blk"><div class="bh">${ic('chart-bar')}Other stats<small style="margin-left:2px;font-size:12px;color:var(--pt);background:var(--pl);padding:2px 10px;border-radius:99px">${fmt(others.length)}</small>${others.length>8?`<input type="text" class="sf" style="width:130px!important" placeholder="Search" oninput="filt(this)">`:''}</div>
    <div class="os">${others.map(o=>`<div class="oc" data-n="${esc((o.l+' '+o.v).toLowerCase())}" title="${esc(o.k+': '+s[o.k])}"><span>${esc(o.l)}</span><b>${esc(o.v)}</b></div>`).join('')}</div></div>`:'';
  const nms=[name,...Object.keys(s).filter(k=>/(^|\.)(username|display_?name)$/i.test(k)&&!/\.\d+\./.test(k)&&ok(s[k])).map(k=>s[k])];
  const star=findStar(nms,bio,roles.some(r=>r.id==='contentcreator'));
  if(star&&star.exact)saveStar(star.c[1],uid);
  const starCard=star?`<div class="card blk"><div class="bh">${ic('star-filled')}Star code</div><div class="sc"><b>${esc(star.c[1])}</b><button class="soft mini" style="margin:0" onclick="copyCode('${esc(star.c[1])}',this)">Copy</button></div><p class="sn">Support ${esc(star.c[0])}${star.why?' · '+esc(star.why):''}</p></div>`:'';
  const rows=[['User ID',uid],s['meta.joined']?['Joined',s['meta.joined']]:null,member?['Member for',member]:null,onKey?['Status',online?'Online now':'Offline']:null].filter(Boolean);
  const side=`
    <div class="card prof"><div class="pr">
      <div class="av">${thumb(avatar(uid),'user','pf',initial(name))}${online?'<span class="dot"></span>':''}</div>
      <div class="who"><h1>${esc(name)}</h1>${roleIcons?`<div class="rl">${roleIcons}</div>`:''}<div class="idn">ID ${esc(uid)}</div></div></div>
      ${bio?`<p class="bio">${esc(bio)}</p>`:''}
      <div class="tags">${onKey?`<span class="chip ${online?'on':'off'}">${ic('activity')}${online?'Online now':'Offline'}</span>`:''}${deleted?`<span class="chip bad">${ic('alert-triangle')}Deleted account</span>`:''}${s['meta.joined']?`<span class="chip">${ic('calendar')}Joined ${esc(s['meta.joined'])}</span>`:''}${lastChip}</div></div>
    ${starCard}
    <div class="card strip">${cell('friends','Friends')}${cell('followers','Followers')}${cell('following','Following')}</div>
    <div class="card blk"><div class="bh">${ic('id')}Account</div><div class="rows">${rows.map(([l,v])=>`<div class="row"><span>${esc(l)}</span><b>${esc(v)}</b></div>`).join('')}</div></div>
    ${otherHtml}
    ${badges.length?`<div class="card blk"><div class="bh">${ic('award')}Badges<small style="margin-left:2px;font-size:12px;color:var(--pt);background:var(--pl);padding:2px 10px;border-radius:99px">${fmt(badges.length)}</small>${badges.length>5?`<input type="text" class="sf" style="width:130px!important" placeholder="Search" oninput="filt(this)">`:''}</div>${badgeRows(badges)}</div>`:''}`;

  /* main: stat cards (click for a graph) */
  const tile=(label,icon,val,key,deltaKeys,sub)=>{const gid=graph(key,label,icon,val,sub);
    return `<div class="card stat ${gid?'clk':''}" ${gid?`onclick="openG('${gid}')"`:''}>${delta(h,deltaKeys||[K[key]])}<div class="si">${ic(icon)}</div><div class="sv">${fmtB(val)}</div><div class="sl">${label}</div>${gid?`<span class="gi">${ic('chart-line')}</span>`:''}</div>`};
  const vid=hasGames?reg({title:'Game visits',icon:'eye',sub:'Across all games this user made',sers:[{label:'Total visits',key:'agg.visits',color:col.pvisits,cur:sum('visits')}]}):'';
  const pid=hasGames?reg({title:'Playing now',icon:'activity',sub:'Players currently in this user\'s games',sers:[{label:'Players online',key:'agg.active',color:'#22c55e',cur:sum('active')}]}):'';
  const gtile=(label,icon,val,gid)=>`<div class="card stat clk" onclick="openG('${gid}')"><div class="si">${ic(icon)}</div><div class="sv">${fmtB(val)}</div><div class="sl">${label}</div><span class="gi">${ic('chart-line')}</span></div>`;
  const tiles=[
    hasGames?tile('Games made','device-gamepad-2',games.length,'games'):'',
    cnt.groups!==undefined?tile('Groups','users-group',cnt.groups,'groups'):'',
    cnt.badges!==undefined?tile('Badges','award',cnt.badges,'badges'):'',
    hasGames?gtile('Game visits','eye',sum('visits'),vid):'',
    hasGames?gtile('Playing now','activity',sum('active'),pid):'',
    cnt.clans!==undefined?tile('Clans','shield',cnt.clans,'clans'):'',
    cnt.inventory!==undefined?tile('Inventory items','package',cnt.inventory,'inventory'):''
  ].filter(Boolean).join('');

  /* main: sections */
  const parts=[];
  const lf=items(s,'friends'),lg=items(s,'groups');
  const nav=lf.length>0?`<span class="nav"><button class="soft" onclick="viewAll(this)">${FOPEN?'Show less':'View all'}</button><button class="soft icon" onclick="railGo(-1)" title="Scroll left">${ic('arrow-left')}</button><button class="soft icon" onclick="railGo(1)" title="Scroll right">${ic('arrow-right')}</button></span>`:'';
  parts.push(section('friends','Friends','users',cnt.friends??(lf.length||null),friendsHtml(lf,cnt.friends),false,'',nav));
  parts.push(section('groups','Groups','users-group',cnt.groups??(lg.length||null),groupsHtml(lg,cnt.groups),lg.length>4,'Search groups',lg.length>4?`<button class="soft" data-n="${lg.length}" onclick="toggleG(this)">${GOPEN?'Show less':`Show all (${lg.length})`}</button>`:''));
  if(hasGames)parts.push(section('games','Games','device-gamepad-2',games.length,gamesHtml(games),games.length>5,'Search games'));
  const G=[['clans','Clans','shield','Not in a clan','group'],['inventory','Inventory','package','Inventory is empty','inventory']];
  for(const [sec,t,i,none,kind] of G){const l=items(s,sec),tot=cnt[sec];if(l.length||tot!==undefined)parts.push(section(sec,t,i,tot??l.length,plain(l,tot,{fb:i,none,kind}),l.length>5,'Search '+t.toLowerCase()))}
  for(const [sec,t,i] of [['followers','Followers','user-check'],['following','Following','user-plus']]){const l=items(s,sec),tot=cnt[sec];if(l.length||tot!==undefined)parts.push(section(sec,t,i,tot??l.length,people(l,tot,t.toLowerCase(),i),l.length>5,'Search '+t.toLowerCase()))}
  const act=activity(h,s);
  parts.push(section('activity','Activity','activity',act.length||null,act.length?`<div class="card tl">${act.map(x=>{const o=ACTOPEN.has(x.id);return `<div class="chg"><span class="ico">${ic(x.icon)}</span><div class="bd2">${x.html}<div><button class="soft mini" data-id="${esc(x.id)}" onclick="toggleD(this)">${o?'Show less':'Show more'}</button></div><div class="dt ${o?'open':''}">${x.detail}</div></div><time>${esc(dstr(x.ts))}</time></div>`}).join('')}</div>`
    :emptyState('activity','Nothing has changed yet','The tracker checks every minute. New friends, followers, badges and visits will show up here.')));

  $('out').innerHTML=`
  <div class="tb">${STACK.length?`<button class="soft" style="margin-right:auto" onclick="goBack()">${ic('arrow-left')}Back to ${esc(STACK[STACK.length-1].name)}</button>`:''}<div class="upd">${ic('clock')}Updated ${new Date(a.ts).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'})}</div>
    <button class="soft" onclick="load()">${ic('refresh')}Refresh now</button></div>
  <div class="layout"><aside class="side">${side}</aside>
  <main class="main">${tiles?`<div class="stats">${tiles}</div>`:''}${parts.join('')}
  ${a.endpoints.length?'':'<p class="note">Only basic profile info was found. To show more (friends, groups, inventory…), add the real API paths to ENDPOINTS in dashboard.py.</p>'}</main></div>`;
}
function startAuto(){if(!timer)timer=setInterval(()=>load('auto'),60000)}
$('uid').addEventListener('keydown',e=>{if(e.key==='Enter')search()});
showHome();
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, body, ctype, code=200):
        b = body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        uid = re.sub(r"\D", "", q.get("id", ["95204"])[0]) or "95204"
        if ADMIN_TOKEN and u.path in ("/api/probe", "/api/discover", "/api/learn", "/api/creators") and q.get("token", [""])[0] != ADMIN_TOKEN:
            return self.send("Add ?token=YOUR_ADMIN_TOKEN to use this page.", "text/plain", 403)
        try:
            if u.path == "/api/track":
                res = track(uid)
                if q.get("q"): note_search(uid)
                self.send(json.dumps(res), "application/json")
            elif u.path == "/img":
                res = fetch_img(q.get("u", [""])[0])
                if res:
                    self.send_response(200); self.send_header("Content-Type", res[0])
                    self.send_header("Content-Length", str(len(res[1]))); self.send_header("Cache-Control", "max-age=600")
                    self.end_headers(); self.wfile.write(res[1])
                else: self.send_error(404)
            elif u.path == "/api/discover": self.send(json.dumps(discover(), indent=2), "application/json")
            elif u.path == "/api/find": self.send(json.dumps(find_users(q.get("name", [""])[0][:60])), "application/json")
            elif u.path == "/api/starlink":
                link_star(uid, q.get("code", [""])[0]); self.send("{}", "application/json")
            elif u.path == "/api/resolve":
                threading.Thread(target=resolve_stars, args=(bool(q.get("force")),), daemon=True).start()
                self.send(json.dumps({"running": True}), "application/json")
            elif u.path == "/api/creators":
                self.send(json.dumps({"state": STAR_STATE, "learned": {k: v for k, v in LEARNED.items()}}, indent=2), "application/json")
            elif u.path == "/api/learn":
                self.send(json.dumps(learn_endpoints(force=True), indent=2), "application/json")
            elif u.path == "/api/home": self.send(json.dumps(home()), "application/json")
            elif u.path == "/api/history": self.send(json.dumps(history(uid)), "application/json")
            elif u.path == "/api/probe": self.send(json.dumps(probe(uid), indent=2), "application/json")
            elif u.path == "/api/series": self.send(json.dumps(series(uid, q.get("key", [""])[0])), "application/json")
            elif u.path == "/": self.send(PAGE, "text/html; charset=utf-8")
            else: self.send_error(404)
        except Exception as e:
            traceback.print_exc()
            self.send(json.dumps({"error": f"{type(e).__name__}: {e}"}), "application/json", 500)

if __name__ == "__main__":
    threading.Thread(target=resolve_stars, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    url = f"http://localhost:{PORT}"
    print(f"Vortex Tracker running at {url}  (Ctrl+C to stop)")
    if not DEPLOYED: threading.Timer(1, lambda: webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\nStopped.")
