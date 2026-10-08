#!/usr/bin/env python3
"""
SM MAL SUB by Nandu10 — standalone service.

Malayalam subtitles from Msone + Movie Mirror + Team GOAT, one entry
per source. Subtitles only — video comes from your own sources.

Routes:
  /manifest.json
  /subtitles/<type>/<id>.json
  /srt/<src>/<key>.srt

Data (in ./data/):
  mm_data.json     Movie Mirror items (imdb_id -> sub_url)
  goat_data.json   Team GOAT items (imdb_id -> sub_url)
  mzone_data.json  Msone items (imdb_id -> post_url)
"""
import html
import io
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import zipfile

from flask import Flask, jsonify, request, Response

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")

# Official Msone subtitle addon (bypasses Cloudflare via download-manager URLs)
MSONE_OFFICIAL_API = "https://addon.malayalamsubtitles.org"

# TMDB API for IMDb -> title lookup (live subtitle search).
# Set TMDB_API_KEY in Render env vars. Live-fetch is skipped if unset.
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")

# Live-fetch endpoints
MM_WP_API = "https://moviemirrorsubtitles.com/wp-json/wp/v2"
GOAT_HOME = "https://malayalamsubtitles.in/"
LIVE_URL_TTL = 7 * 24 * 3600   # found live SRT URLs rarely change

# ---------------- subtitle data ----------------
_mm_data = None
_goat_data = None
_mzone_data = None
_mm_by_tt = None
_goat_by_tt = None
_mzone_by_tt = None


def _load_json(name):
    p = os.path.join(DATA_DIR, name)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def mm_items():
    global _mm_data
    if _mm_data is None:
        _mm_data = _load_json("mm_data.json").get("items", [])
    return _mm_data


def goat_items():
    global _goat_data
    if _goat_data is None:
        _goat_data = _load_json("goat_data.json").get("items", [])
    return _goat_data


def mzone_items():
    """All Msone items (sections + archive) with an imdb_id."""
    global _mzone_data
    if _mzone_data is None:
        d = _load_json("mzone_data.json")
        out = []
        seen = set()

        def collect(o):
            if isinstance(o, list):
                for it in o:
                    if isinstance(it, dict) and it.get("imdb_id"):
                        key = (it["imdb_id"], it.get("post_url"))
                        if key not in seen:
                            seen.add(key)
                            out.append(it)
            elif isinstance(o, dict):
                for v in o.values():
                    collect(v)

        collect(d.get("sections"))
        collect(d.get("archive"))
        _mzone_data = out
    return _mzone_data


def _index_by_tt(items):
    """imdb_id -> item or [items] (series can have several posts)."""
    idx = {}
    for it in items:
        tt = it.get("imdb_id")
        if not tt:
            continue
        if it.get("type") == "series" or it.get("media") == "tv":
            cur = idx.get(tt)
            if isinstance(cur, list):
                cur.append(it)
            elif isinstance(cur, dict):
                idx[tt] = [cur, it]
            else:
                idx[tt] = [it]
        else:
            idx.setdefault(tt, it)
    return idx


def mm_by_tt():
    global _mm_by_tt
    if _mm_by_tt is None:
        _mm_by_tt = _index_by_tt(mm_items())
    return _mm_by_tt


def goat_by_tt():
    global _goat_by_tt
    if _goat_by_tt is None:
        _goat_by_tt = _index_by_tt(goat_items())
    return _goat_by_tt


def mzone_by_tt():
    global _mzone_by_tt
    if _mzone_by_tt is None:
        _mzone_by_tt = _index_by_tt(mzone_items())
    return _mzone_by_tt


def _pick_series(items, season):
    """Pick the season post matching the requested season number."""
    if not isinstance(items, list):
        items = [items]
    if season is None:
        return items[0]
    s2 = "%02d" % season
    for it in items:
        blob = ((it.get("sub_url") or "") + " " + (it.get("post_url") or "")
                + " " + (it.get("name_en") or "") + " "
                + (it.get("name") or "")).lower()
        if (f"s{s2}" in blob or f"season {season}" in blob
                or f"season{s2}" in blob):
            return it
    return items[0]


# ---------------- subtitle fetch engine ----------------
SUB_CACHE_DIR = os.path.join(BASE_DIR, "sub_cache")
SUB_INDEX = os.path.join(SUB_CACHE_DIR, "index.json")
SUB_TTL = 7 * 24 * 3600   # subtitle bytes rarely change
MISS_TTL = 45 * 60        # remember misses for 45min (so fixes show faster)
SITE_GAP = 1.5            # politeness gap between site fetches

os.makedirs(SUB_CACHE_DIR, exist_ok=True)

_sub_lock = threading.Lock()
_sub_last_fetch = 0.0


def _sub_fetch(url, referer=None, timeout=30, retries=3):
    """Polite GET with browser UA via curl subprocess."""
    global _sub_last_fetch
    last_err = None
    for attempt in range(retries):
        with _sub_lock:
            wait = SITE_GAP - (time.time() - _sub_last_fetch)
            if wait > 0:
                time.sleep(wait)
            try:
                cmd = ["curl", "-sL", "--max-time", str(timeout),
                       "-A", UA, url]
                if referer:
                    cmd += ["-e", referer]
                r = subprocess.run(cmd, capture_output=True,
                                   timeout=timeout + 10)
                if r.returncode != 0 or not r.stdout:
                    raise IOError(f"curl rc={r.returncode}")
                return r.stdout
            except Exception as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
            finally:
                _sub_last_fetch = time.time()
    raise IOError(f"_sub_fetch failed after {retries}: {last_err}")


def _sub_index_load():
    try:
        with open(SUB_INDEX, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _sub_index_save(idx):
    tmp = SUB_INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f)
    os.replace(tmp, SUB_INDEX)


def _to_utf8(raw):
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(enc).encode("utf-8")
        except (UnicodeDecodeError, ValueError):
            continue
    return raw.decode("utf-8", errors="replace").encode("utf-8")


def _pick_from_zip(data, season=None, episode=None):
    """Extract the right .srt from a zip. For series, match SXXEXX."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist()
                 if n.lower().endswith((".srt", ".ass", ".vtt"))
                 and not n.startswith("__MACOSX")]
        if not names:
            return None
        if season is not None and episode is not None:
            pat = re.compile(r"s%02d\s*e%02d" % (season, episode), re.I)
            for n in names:
                if pat.search(n):
                    return z.read(n)
            pat2 = re.compile(r"%dx%02d" % (season, episode), re.I)
            for n in names:
                if pat2.search(n):
                    return z.read(n)
            return None  # specific episode requested but not in zip
        names.sort(key=lambda n: (0 if n.lower().endswith(".srt") else 1, n))
        return z.read(names[0])
    return None


def _sub_key(src, rid):
    safe = re.sub(r"[^A-Za-z0-9]+", "_", rid).strip("_")
    return f"{src}_{safe}"


# Msone download-link patterns (post page -> .srt/.zip file)
_MSONE_DL_PATTERNS = [
    re.compile(r'href="([^"]+?\.srt[^"]*)"', re.I),
    re.compile(r"href='([^']+?\.srt[^']*)'", re.I),
    re.compile(r'href="([^"]+?\.zip[^"]*)"', re.I),
    re.compile(r"href='([^']+?\.zip[^']*)'", re.I),
    re.compile(r'href="([^"]+?\.ass[^"]*)"', re.I),
]
_MSONE_DL_TEXT_RE = re.compile(
    r'<a[^>]+href="([^"]+)"[^>]*>([^<]*(?:download|'
    r'\u0d21\u0d57\u0d7a\u0d4d\u200d\u0d32\u0d4b\u0d21\u0d4d|'
    r'\u0d38\u0d2c\u0d4d\u0d1f\u0d48\u0d31\u0d4d\u0d31\u0d3f\u0d32\u0d4d)'
    r'[^<]*)</a>', re.I)


def _msone_download_url(post_html, post_url):
    for pat in _MSONE_DL_PATTERNS:
        m = pat.search(post_html)
        if m:
            return urllib.parse.urljoin(post_url, html.unescape(m.group(1)))
    m = _MSONE_DL_TEXT_RE.search(post_html)
    if m:
        return urllib.parse.urljoin(post_url, html.unescape(m.group(1)))
    return None


def _resolve_subtitle(src, rid, imdb_id, season=None, episode=None):
    """Download (or load from cache) the subtitle for one source.

    Returns srt bytes or None. src in ('mm', 'goat', 'msone').
    """
    key = _sub_key(src, rid)
    idx = _sub_index_load()
    now = time.time()
    entry = idx.get(key)
    if entry:
        if entry.get("miss") and now - entry["at"] < MISS_TTL:
            return None
        path = os.path.join(SUB_CACHE_DIR, key + ".srt")
        if (not entry.get("miss") and now - entry["at"] < SUB_TTL
                and os.path.exists(path)):
            with open(path, "rb") as f:
                return f.read()
    # live lookup
    item = None
    file_url = None
    referer = None
    try:
        if src == "mm":
            e = mm_by_tt().get(imdb_id)
            item = _pick_series(e, season) if e else None
            if item and item.get("sub_url"):
                file_url = item["sub_url"]
                referer = item.get("post_url")
            else:
                got = _mm_live_file_url(imdb_id, season)
                if got:
                    file_url, referer = got
        elif src == "goat":
            e = goat_by_tt().get(imdb_id)
            item = _pick_series(e, season) if e else None
            if item and item.get("sub_url"):
                file_url = item["sub_url"]
                referer = item.get("post_url")
            else:
                got = _goat_live_file_url(imdb_id, season)
                if got:
                    file_url, referer = got
        elif src == "msone":
            got = _msone_file_url(imdb_id, season)
            if got:
                file_url, referer = got
        if not file_url:
            raise ValueError("no subtitle file url")
        raw = _sub_fetch(file_url, referer=referer)
        if file_url.lower().split("?")[0].endswith(".zip") or raw[:2] == b"PK":
            srt = _pick_from_zip(raw, season, episode)
            if not srt:
                raise ValueError("no matching srt in zip")
        else:
            srt = raw
        srt = _to_utf8(srt)
    except Exception:
        idx[key] = {"at": now, "miss": True}
        _sub_index_save(idx)
        return None
    with open(os.path.join(SUB_CACHE_DIR, key + ".srt"), "wb") as f:
        f.write(srt)
    idx[key] = {"at": now}
    _sub_index_save(idx)
    return srt


def _msone_file_url(imdb_id, season=None):
    """Returns the direct subtitle file URL for an Msone title, or None.

    Msone post pages are behind Cloudflare which often blocks automated
    fetches — so we check live (with caching) instead of trusting the
    index alone. Returns None quickly on 403/block.
    """
    idx = _sub_index_load()
    now = time.time()
    ck = f"msone_url:{imdb_id}:{season}"
    e = idx.get(ck)
    if e:
        if e.get("miss") and now - e["at"] < MISS_TTL:
            return None
        if e.get("url") and now - e["at"] < SUB_TTL:
            return e["url"], e.get("referer")
    entry = mzone_by_tt().get(imdb_id)
    item = _pick_series(entry, season) if entry else None
    if not item or not item.get("post_url"):
        idx[ck] = {"at": now, "miss": True}
        _sub_index_save(idx)
        return None
    post_url = item["post_url"]
    try:
        page = _sub_fetch(post_url, timeout=12, retries=1)
        page_html = page.decode("utf-8", errors="replace")
        if "Just a moment" in page_html or "cf-challenge" in page_html:
            raise ValueError("cloudflare challenge")
        file_url = _msone_download_url(page_html, post_url)
        if not file_url:
            raise ValueError("no download link")
    except Exception:
        idx[ck] = {"at": now, "miss": True}
        _sub_index_save(idx)
        return None
    idx[ck] = {"at": now, "url": file_url, "referer": post_url}
    _sub_index_save(idx)
    return file_url, post_url


def _api_fetch_json(url, timeout=12):
    """Quick JSON GET (for TMDB / WP APIs). No politeness gap."""
    cmd = ["curl", "-sL", "--max-time", str(timeout), "-A", UA, url]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    if r.returncode != 0 or not r.stdout:
        raise IOError("api fetch failed")
    return json.loads(r.stdout.decode("utf-8", errors="replace"))


def _norm_title(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tmdb_title(imdb_id):
    """Returns (title, year, media_type) for an IMDb ID via TMDB.

    Cached for 7 days. Returns (None, None, None) if no API key or
    lookup fails.
    """
    if not TMDB_API_KEY:
        return None, None, None
    idx = _sub_index_load()
    now = time.time()
    ck = f"tmdb:{imdb_id}"
    e = idx.get(ck)
    if e and not e.get("miss") and now - e["at"] < LIVE_URL_TTL:
        return e.get("title"), e.get("year"), e.get("mtype")
    if e and e.get("miss") and now - e["at"] < MISS_TTL:
        return None, None, None
    try:
        url = (f"https://api.themoviedb.org/3/find/{imdb_id}"
               f"?api_key={TMDB_API_KEY}&external_source=imdb_id")
        data = _api_fetch_json(url)
        title = year = mtype = None
        mr = data.get("movie_results") or []
        tr = data.get("tv_results") or []
        if mr:
            title = mr[0].get("title")
            rd = mr[0].get("release_date") or ""
            year = rd[:4] if len(rd) >= 4 else None
            mtype = "movie"
        elif tr:
            title = tr[0].get("name")
            rd = tr[0].get("first_air_date") or ""
            year = rd[:4] if len(rd) >= 4 else None
            mtype = "series"
        if not title:
            raise ValueError("no tmdb match")
        idx[ck] = {"at": now, "title": title, "year": year,
                   "mtype": mtype}
        _sub_index_save(idx)
        return title, year, mtype
    except Exception:
        idx[ck] = {"at": now, "miss": True}
        _sub_index_save(idx)
        return None, None, None


def _titles_match(want, have, year=None):
    """Fuzzy title match: all significant words of `want` in `have`,
    plus year match when both known."""
    w = _norm_title(want)
    h = _norm_title(have)
    if not w or not h:
        return False
    # year check
    if year:
        ym = re.search(r"\b(19|20)\d{2}\b", h)
        if ym and ym.group(0) != str(year):
            return False
    ww = [x for x in w.split() if len(x) > 2]
    if not ww:
        return w in h
    hit = sum(1 for x in ww if x in h)
    return hit / len(ww) >= 0.7


def _mm_live_file_url(imdb_id, season=None):
    """Live Movie Mirror subtitle lookup via WP REST API.

    Chain: TMDB title -> WP search -> post page -> ?custom_download=
    SRT URL. Returns (file_url, referer) or None. Results cached.
    """
    idx = _sub_index_load()
    now = time.time()
    ck = f"mm_live:{imdb_id}:{season}"
    e = idx.get(ck)
    if e:
        if e.get("miss") and now - e["at"] < MISS_TTL:
            return None
        if e.get("url") and now - e["at"] < LIVE_URL_TTL:
            return e["url"], e.get("referer")
    try:
        title, year, _mtype = _tmdb_title(imdb_id)
        if not title:
            raise ValueError("no tmdb title")
        q = urllib.parse.quote(title)
        data = _api_fetch_json(
            f"{MM_WP_API}/search?search={q}&per_page=10")
        post_url = None
        for r in data:
            rt = html.unescape(r.get("title") or "")
            if not _titles_match(title, rt, year):
                continue
            if season is not None:
                blob = _norm_title(rt)
                s2 = "%02d" % season
                if not (f"season {season}" in blob or f"s{s2}" in blob
                        or f"season{s2}" in blob):
                    continue
            # fetch full post to get canonical link
            p = _api_fetch_json(
                f"{MM_WP_API}/posts/{r.get('id')}")
            post_url = p.get("link")
            break
        if not post_url:
            raise ValueError("no wp match")
        page = _sub_fetch(post_url, timeout=15, retries=2)
        page_html = page.decode("utf-8", errors="replace")
        m = re.search(r"custom_download=(https?[^&\"']+)", page_html)
        if not m:
            raise ValueError("no download link")
        file_url = urllib.parse.unquote(m.group(1))
    except Exception:
        idx[ck] = {"at": now, "miss": True}
        _sub_index_save(idx)
        return None
    idx[ck] = {"at": now, "url": file_url, "referer": post_url}
    _sub_index_save(idx)
    return file_url, post_url


def _goat_live_file_url(imdb_id, season=None):
    """Live Team GOAT subtitle lookup.

    Chain: TMDB title -> homepage title list -> /release/ page ->
    wp.malayalamsubtitles.in/download/<id> SRT URL.
    Returns (file_url, referer) or None. Results cached.
    """
    idx = _sub_index_load()
    now = time.time()
    ck = f"goat_live:{imdb_id}:{season}"
    e = idx.get(ck)
    if e:
        if e.get("miss") and now - e["at"] < MISS_TTL:
            return None
        if e.get("url") and now - e["at"] < LIVE_URL_TTL:
            return e["url"], e.get("referer")
    try:
        title, year, _mtype = _tmdb_title(imdb_id)
        if not title:
            raise ValueError("no tmdb title")
        # homepage embeds all ~500 titles; cache it 1h
        hck = "goat_home_html"
        he = idx.get(hck)
        home_html = None
        if he and he.get("html") and now - he["at"] < 3600:
            home_html = he["html"]
        else:
            raw = _sub_fetch(GOAT_HOME, timeout=15, retries=2)
            home_html = raw.decode("utf-8", errors="replace")
            idx[hck] = {"at": now, "html": home_html[:600000]}
            _sub_index_save(idx)
        release_url = None
        pat = re.compile(
            r'<a href="(/release/[^"]+)"><p class="movie-name name">'
            r"([^<]{3,120})</p>", re.I)
        for m in pat.finditer(home_html):
            slug, t = m.group(1), html.unescape(m.group(2))
            if not _titles_match(title, t, year):
                continue
            if season is not None:
                blob = _norm_title(t)
                s2 = "%02d" % season
                if not (f"season {season}" in blob or f"s{s2}" in blob
                        or f"season{s2}" in blob):
                    continue
            release_url = urllib.parse.urljoin(GOAT_HOME, slug)
            break
        if not release_url:
            raise ValueError("no goat match")
        page = _sub_fetch(release_url, timeout=15, retries=2)
        page_html = page.decode("utf-8", errors="replace")
        dm = re.search(
            r"https://wp\.malayalamsubtitles\.in/download/\d+/?",
            page_html)
        if not dm:
            raise ValueError("no download link")
        file_url = dm.group(0)
    except Exception:
        idx[ck] = {"at": now, "miss": True}
        _sub_index_save(idx)
        return None
    idx[ck] = {"at": now, "url": file_url, "referer": release_url}
    _sub_index_save(idx)
    return file_url, release_url


def _has_subtitle(src, imdb_id, season=None):
    """Fast index-only check: does this source list a subtitle?

    Falls back to live site search (cached) when the saved index
    misses, so newly posted subtitles appear without waiting for
    the next data refresh.
    """
    if src == "mm":
        e = mm_by_tt().get(imdb_id)
        if e:
            item = _pick_series(e, season)
            if item and item.get("sub_url"):
                return True
        # live fallback
        return bool(_mm_live_file_url(imdb_id, season))
    elif src == "goat":
        e = goat_by_tt().get(imdb_id)
        if e:
            item = _pick_series(e, season)
            if item and item.get("sub_url"):
                return True
        # live fallback
        return bool(_goat_live_file_url(imdb_id, season))
    elif src == "msone":
        # Msone needs a live check (Cloudflare) — the file URL doubles
        # as the availability signal.
        return bool(_msone_file_url(imdb_id, season))
    else:
        return False


SUB_SOURCES = [
    ("mm", "Movie Mirror"),
    ("goat", "Team GOAT"),
    ("msone", "Msone"),
]


def _msone_official_entries(vtype, rid):
    """Fetch subtitles from the official Msone addon (pass-through URLs).

    The official addon uses download-manager URLs that bypass Cloudflare,
    so this works for Msone-only titles our local data can't fetch.
    URLs are signed and may expire — always fetch fresh, never cache.
    Tries curl first, then Python urllib as fallback (different TLS
    fingerprint in case Cloudflare blocks one client).
    """
    url = (f"{MSONE_OFFICIAL_API}/subtitles/{vtype}/"
           f"{urllib.parse.quote(rid)}.json")
    data = None
    # attempt 1: curl
    try:
        cmd = ["curl", "-sL", "--max-time", "20", "-A", UA, url]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode == 0 and r.stdout:
            data = json.loads(r.stdout.decode("utf-8", errors="replace"))
    except Exception:
        data = None
    # attempt 2: python urllib (different TLS fingerprint)
    if data is None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            data = None
    if not data:
        return []
    try:
        out = []
        for s in data.get("subtitles", []):
            surl = s.get("url")
            if not surl:
                continue
            out.append({
                "id": f"smsub:msone_off:{rid}:{s.get('id', '')}",
                "url": surl,
                "lang": s.get("lang", "mal"),
            })
        return out
    except Exception:
        return []


@app.route("/debug/msone/<rid>")
def debug_msone(rid):
    """Diagnostic: test official Msone addon connectivity from this server."""
    url = (f"{MSONE_OFFICIAL_API}/subtitles/movie/"
           f"{urllib.parse.quote(rid)}.json")
    info = {"url": url}
    # curl attempt
    try:
        cmd = ["curl", "-sL", "--max-time", "15", "-A", UA, "-w",
               "\n%{http_code}", url]
        r = subprocess.run(cmd, capture_output=True, timeout=25)
        out = r.stdout.decode("utf-8", errors="replace")
        info["curl_rc"] = r.returncode
        info["curl_http"] = out.strip().split("\n")[-1] if out else None
        info["curl_bytes"] = len(r.stdout)
        info["curl_stderr"] = r.stderr.decode("utf-8",
                                              errors="replace")[:200]
    except Exception as e:
        info["curl_error"] = str(e)[:200]
    # urllib attempt
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        info["urllib_http"] = resp.status
        info["urllib_bytes"] = len(raw)
    except Exception as e:
        info["urllib_error"] = str(e)[:200]
    return jsonify(info)




# ---------------- routes ----------------
@app.route("/manifest.json")
def manifest():
    return jsonify({
        "id": "com.smmal.subtitles",
        "version": "1.2.4",
        "name": "SM MAL SUB",
        "description": "Malayalam subtitles from Movie Mirror + Team GOAT "
                       "+ Msone (official addon). Live search: new subtitles "
                       "appear automatically. Subtitles only — video "
                       "comes from your own sources.",
        "resources": ["subtitles"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "catalogs": [],
    })


# IMDb ID overrides for titles missing them (TMDB ID -> IMDb ID)
IMDB_OVERRIDES = {
    "14": "tt0169547",
    "38": "tt0338013",
    "70": "tt0405159",
    "161": "tt0240772",
    "274": "tt0102926",
    "300": "tt0354899",
    "322": "tt0327056",
    "329": "tt0107290",
    "335": "tt0064116",
    "380": "tt0095953",
    "411": "tt0363771",
    "445": "tt0387898",
    "453": "tt0268978",
    "564": "tt0120616",
    "601": "tt0083866",
    "643": "tt0015648",
    "665": "tt0052618",
    "692": "tt0069089",
    "714": "tt0120347",
    "745": "tt0167404",
    "769": "tt0099685",
    "790": "tt0094525",
    "832": "tt0022100",
    "839": "tt0067023",
    "862": "tt0114709",
    "985": "tt0074486",
    "1044": "tt0795176",    # Planet Earth (2006)
    "1255": "tt0468492",
    "1396": "tt0903747",
    "1398": "tt0079944",
    "1399": "tt0944947",
    "1402": "tt1520211",
    "1411": "tt1839578",
    "1429": "tt2560140",
    "1430": "tt0081846",    # Cosmos: A Personal Voyage (1980)
    "1487": "tt0167190",
    "1585": "tt0038650",
    "1637": "tt0111257",
    "1705": "tt1119644",
    "1726": "tt0371746",
    "1771": "tt0458339",
    "1902": "tt0125659",
    "2016": "tt0444182",
    "2080": "tt0458525",
    "2251": "tt0250797",
    "2288": "tt0455275",
    "3763": "tt0058430",
    "3782": "tt0044741",
    "4327": "tt0096657",    # Mr. Bean (1990 series)
    "4476": "tt0110322",
    "4480": "tt0091288",
    "4547": "tt0258000",
    "4607": "tt0411008",
    "4613": "tt0185906",
    "5185": "tt0066249",
    "5511": "tt0062229",
    "5595": "tt1016301",
    "5925": "tt0057115",
    "7737": "tt0432021",
    "7973": "tt0825236",
    "7979": "tt0419887",
    "8681": "tt0936501",
    "8740": "tt0096163",
    "8848": "tt0200465",
    "9357": "tt0265459",
    "9367": "tt0104815",
    "9477": "tt0349683",
    "9509": "tt0328107",
    "9662": "tt0286244",
    "9806": "tt0317705",
    "10138": "tt1228705",
    "10191": "tt0892769",
    "10226": "tt0338095",
    "10451": "tt0111797",
    "10757": "tt0248126",
    "10974": "tt0091431",
    "10999": "tt0088944",
    "11000": "tt0115685",
    "11036": "tt0332280",
    "11072": "tt0071230",
    "11253": "tt0411477",
    "11518": "tt0213890",
    "11661": "tt0424205",
    "11830": "tt0092048",
    "11906": "tt0076786",
    "12222": "tt0451079",
    "12259": "tt0073707",
    "12539": "tt0395057",
    "13123": "tt1241195",
    "13528": "tt0036855",
    "13807": "tt0796212",
    "14163": "tt0871510",
    "14752": "tt0292490",
    "15003": "tt1183252",
    "17264": "tt0078872",
    "17431": "tt1182345",
    "18311": "tt0101258",
    "18384": "tt1360795",
    "18526": "tt1233461",
    "19885": "tt1475582",
    "20034": "tt1093369",
    "20662": "tt0955308",
    "21348": "tt0181627",
    "21575": "tt1235166",
    "22238": "tt0423310",
    "22954": "tt1057500",
    "23945": "tt0115836",
    "25597": "tt1002567",
    "26610": "tt0119375",
    "29917": "tt1258197",
    "30244": "tt0770214",
    "34105": "tt0159145",
    "35010": "tt1119199",
    "36204": "tt0995740",
    "36657": "tt0120903",
    "36668": "tt0376994",
    "38000": "tt1233473",
    "38011": "tt0475557",
    "38810": "tt1379182",
    "40842": "tt0079638",
    "41727": "tt2017109",
    "42528": "tt0066530",
    "42589": "tt2176165",
    "42699": "tt0092337",
    "43539": "tt1458175",
    "43947": "tt1242432",
    "43969": "tt0363303",
    "44069": "tt0260332",
    "44217": "tt2306299",
    "45016": "tt1278016",
    "46648": "tt2356777",
    "48508": "tt1287875",
    "49538": "tt1270798",
    "50162": "tt0225009",
    "50938": "tt1725995",
    "54186": "tt1590089",
    "60059": "tt3032476",
    "60574": "tt2442560",
    "60625": "tt2861424",
    "60948": "tt3148266",
    "61202": "tt1562872",
    "61222": "tt3398228",
    "61461": "tt0034493",
    "61664": "tt2431438",
    "61670": "tt4284216",
    "62439": "tt1848926",
    "62560": "tt4158110",
    "63174": "tt4052886",
    "63210": "tt0453115",
    "63247": "tt0475784",
    "63333": "tt4179452",
    "63926": "tt4508902",
    "64684": "tt3581932",
    "64840": "tt5332206",
    "65143": "tt4925000",
    "65754": "tt1568346",
    "66732": "tt4574334",
    "66816": "tt0038674",
    "66980": "tt4378376",
    "67070": "tt5687612",
    "67744": "tt5290382",
    "68595": "tt5491994",    # Planet Earth II (2016)
    "68721": "tt1300854",
    "69087": "tt6212854",
    "69740": "tt5071412",
    "70523": "tt5753856",
    "70593": "tt6611916",
    "70649": "tt6256484",
    "71411": "tt6692188",
    "71912": "tt5180504",
    "71914": "tt7462410",
    "72334": "tt1222815",
    "72750": "tt7016936",
    "72844": "tt6763664",
    "72984": "tt0093144",
    "73532": "tt1508675",
    "73544": "tt5743796",
    "73613": "tt6467482",
    "73780": "tt7278424",
    "74064": "tt7418578",
    "74430": "tt6118426",
    "74447": "tt5834256",
    "74577": "tt6257970",
    "75006": "tt1312171",
    "75200": "tt6883044",
    "76170": "tt1430132",
    "76459": "tt1368439",
    "76479": "tt1190634",
    "76659": "tt6466208",
    "77338": "tt1675434",
    "77461": "tt1772424",
    "77716": "tt1650056",
    "79240": "tt8362852",
    "79347": "tt7755494",
    "79352": "tt6077448",
    "79407": "tt8236544",
    "79788": "tt7049682",
    "80307": "tt7493974",
    "80707": "tt5909930",
    "80752": "tt7949218",
    "81049": "tt8463714",
    "81166": "tt8595766",
    "81355": "tt7137906",
    "82624": "tt1787127",
    "82856": "tt8111088",
    "82953": "tt9130692",    # Dynasties (2018)
    "83100": "tt9458304",
    "83221": "tt0413358",
    "83634": "tt8236556",
    "84105": "tt6473300",
    "84773": "tt7631058",
    "84958": "tt9140554",
    "85021": "tt7661390",
    "85271": "tt9140560",
    "85720": "tt2403776",
    "86831": "tt9561862",
    "86850": "tt9139220",
    "87108": "tt7366338",
    "87185": "tt9772814",
    "87313": "tt12879522",
    "87508": "tt9398466",
    "87739": "tt10048342",
    "88055": "tt8068860",
    "88463": "tt8750956",
    "88640": "tt3829868",
    "88803": "tt10233448",
    "89059": "tt0016804",
    "89113": "tt9432978",
    "89545": "tt9337588",
    "89604": "tt10192576",
    "90228": "tt10466872",
    "90260": "tt9446688",
    "90447": "tt10220588",
    "90634": "tt0222024",
    "90660": "tt9058134",
    "90802": "tt1751634",
    "90966": "tt10530900",
    "91363": "tt10168312",
    "91557": "tt9251798",
    "92911": "tt10656392",
    "93241": "tt10905902",
    "93352": "tt9544034",
    "93405": "tt10919420",
    "93705": "tt10485750",
    "93740": "tt0804484",
    "94605": "tt11126994",
    "94796": "tt10850932",
    "94997": "tt11198330",
    "95171": "tt10324164",    # Prehistoric Planet (2022)
    "96041": "tt11557904",
    "96129": "tt11318602",
    "96462": "tt12451520",
    "96648": "tt11612120",
    "96677": "tt2531336",
    "97365": "tt2053425",
    "98187": "tt10893694",
    "98827": "tt12516712",
    "99112": "tt11505790",
    "99478": "tt11953100",
    "99479": "tt12015466",
    "99494": "tt11691684",
    "99966": "tt14169960",
    "100088": "tt3581920",
    "100624": "tt8430234",
    "101352": "tt12004706",
    "102899": "tt0478970",
    "103759": "tt10651790",
    "103768": "tt12809988",
    "104811": "tt11827694",
    "106651": "tt12235718",
    "108285": "tt12937604",
    "108681": "tt7441984",
    "110249": "tt12701270",
    "110316": "tt10795658",
    "110356": "tt12940504",
    "110529": "tt18214248",
    "110533": "tt13400300",
    "111110": "tt11737520",
    "111188": "tt12392504",
    "112119": "tt12874950",
    "112836": "tt13696452",
    "113388": "tt2328503",
    "113622": "tt12477912",
    "113988": "tt13207736",
    "114410": "tt13616990",
    "115036": "tt13668894",
    "116135": "tt11311302",
    "117376": "tt13433812",
    "117465": "tt13911284",
    "119051": "tt13443470",
    "121856": "tt2094766",
    "122917": "tt2310332",
    "123349": "tt14460684",
    "123542": "tt14976292",
    "124364": "tt9813792",
    "126308": "tt2788316",
    "126485": "tt24640580",
    "126829": "tt14160660",
    "127585": "tt1877832",
    "127862": "tt14820482",
    "128206": "tt2181831",
    "129043": "tt14932842",
    "132316": "tt2176013",
    "132752": "tt14473896",
    "132846": "tt2181503",
    "133359": "tt14167390",
    "133678": "tt21875462",
    "135397": "tt0369610",
    "137872": "tt18970038",
    "139582": "tt2292625",
    "152584": "tt2278871",
    "152603": "tt1714915",
    "154326": "tt1710565",
    "157239": "tt13623632",
    "181886": "tt2316411",
    "186110": "tt0270321",
    "197588": "tt13640670",
    "198102": "tt19854762",
    "199818": "tt2236054",
    "200709": "tt20234568",
    "206586": "tt9859436",
    "207332": "tt17069148",
    "210704": "tt20600022",
    "211747": "tt14166656",
    "218230": "tt26225038",
    "219543": "tt26545355",
    "219651": "tt26653824",
    "221079": "tt26862142",
    "224372": "tt27497448",
    "225171": "tt22202452",
    "235260": "tt3210686",
    "239798": "tt0096747",
    "242582": "tt2872718",
    "249042": "tt31806037",
    "251577": "tt3089778",
    "259316": "tt3183660",
    "263115": "tt3315342",
    "265662": "tt0944961",
    "270476": "tt33332385",
    "271110": "tt3498820",
    "273248": "tt3460252",
    "278068": "tt0246833",
    "282058": "tt3837820",
    "283367": "tt35630036",
    "293646": "tt2006295",
    "293768": "tt1458169",
    "296206": "tt42127457",
    "299534": "tt4154796",
    "299537": "tt4154664",
    "313298": "tt32493765",
    "315635": "tt2250912",
    "323517": "tt3678782",
    "324552": "tt4425200",
    "338952": "tt4123430",
    "341182": "tt4313646",
    "347752": "tt2929652",
    "353464": "tt4934950",
    "363088": "tt5095030",
    "370870": "tt5121000",
    "372058": "tt5311514",
    "381284": "tt4846340",
    "382217": "tt6273736",
    "388333": "tt4169250",
    "392044": "tt3402236",
    "392572": "tt5165344",
    "393841": "tt5824826",
    "398924": "tt5091612",
    "399360": "tt4244998",
    "399579": "tt0437086",
    "400617": "tt5776858",
    "401545": "tt4964788",
    "403867": "tt5460276",
    "404579": "tt5465370",
    "407436": "tt2388771",
    "418235": "tt4807830",
    "418472": "tt6628102",
    "422566": "tt6210808",
    "426426": "tt6155172",
    "428493": "tt5635086",
    "432836": "tt5729348",
    "433327": "tt6054758",
    "435577": "tt3300980",
    "438070": "tt6814252",
    "438857": "tt6315750",
    "441889": "tt6108090",
    "444431": "tt6896536",
    "446894": "tt6182908",
    "448491": "tt6083230",
    "451955": "tt6367558",
    "451997": "tt6777370",
    "453276": "tt5923026",
    "453755": "tt6820256",
    "458156": "tt6146586",
    "458723": "tt6857112",
    "460713": "tt6580564",
    "461126": "tt7392212",
    "462718": "tt8176054",
    "468205": "tt6207878",
    "469651": "tt7213936",
    "479034": "tt7080138",
    "484423": "tt6613470",
    "486947": "tt6742252",
    "490132": "tt6966692",
    "491629": "tt7098658",
    "493655": "tt7763020",
    "494680": "tt6940696",
    "496527": "tt7700730",
    "499028": "tt8092252",
    "500723": "tt7797658",
    "512098": "tt6972140",
    "513434": "tt7914416",
    "517814": "tt8267604",
    "517839": "tt5501104",
    "525162": "tt6982254",
    "529216": "tt6908274",
    "529569": "tt8865562",
    "530254": "tt8574252",
    "533991": "tt8108202",
    "534780": "tt8108198",
    "536475": "tt8043456",
    "537996": "tt6412452",
    "538858": "tt8239946",
    "540189": "tt7758160",
    "540468": "tt8590896",
    "541487": "tt8333978",
    "544627": "tt8119680",
    "546230": "tt9063106",
    "547654": "tt7725596",
    "554600": "tt8291224",
    "555605": "tt7497366",
    "567973": "tt9412268",
    "571610": "tt8737614",
    "581361": "tt8130968",
    "588708": "tt9903716",
    "591278": "tt10090796",
    "592898": "tt8948790",
    "594028": "tt7109900",
    "610482": "tt10214826",
    "1032863": "tt22526100",
    "1339654": "tt33575372",
    "1386315": "tt34564059",
}


def _parse_rid(vtype, rid):
    """Returns (imdb_id, season, episode) or None."""
    rid = urllib.parse.unquote(rid)
    parts = rid.split(":")
    # Rejoin tmdb: prefix which split() broke apart
    if parts[0] == "tmdb" and len(parts) >= 2:
        imdb_id = "tmdb:" + parts[1]
        rest = parts[2:]
    else:
        imdb_id = parts[0]
        rest = parts[1:]
    # Handle TMDB IDs via override mapping
    if imdb_id.startswith("tmdb:"):
        tmdb_num = imdb_id[5:]
        if tmdb_num in IMDB_OVERRIDES:
            imdb_id = IMDB_OVERRIDES[tmdb_num]
        else:
            return None
    if not re.match(r"^tt\d+$", imdb_id):
        return None
    season = episode = None
    if vtype == "series" and len(rest) >= 2:
        try:
            season, episode = int(rest[0]), int(rest[1])
        except ValueError:
            return None
    return imdb_id, season, episode


def _subtitle_entries(vtype, rid):
    parsed = _parse_rid(vtype, rid)
    if not parsed:
        return []
    imdb_id, season, _episode = parsed
    base = request.url_root.rstrip("/")
    out = []
    idx = None
    for src, _label in SUB_SOURCES:
        if not _has_subtitle(src, imdb_id, season):
            continue
        key = _sub_key(src, rid)
        # remember rid -> key so /srt can resolve lazily on first hit
        if idx is None:
            idx = _sub_index_load()
        if ("rid:" + key) not in idx:
            idx["rid:" + key] = urllib.parse.unquote(rid)
            _sub_index_save(idx)
        out.append({
            "id": f"smsub:{src}:{rid}",
            "url": f"{base}/srt/{src}/{key}.srt",
            "lang": "mal",
        })
    # Official Msone addon (pass-through) — covers Msone-only titles
    # that Cloudflare blocks us from fetching directly.
    out.extend(_msone_official_entries(vtype, rid))
    return out


def _serve_srt(src, key):
    if src not in ("mm", "goat", "msone"):
        return jsonify({"error": "not found"}), 404
    if not re.match(r"^[A-Za-z0-9_]+$", key):
        return jsonify({"error": "not found"}), 404
    if not key.startswith(src + "_"):
        return jsonify({"error": "not found"}), 404
    path = os.path.join(SUB_CACHE_DIR, key + ".srt")
    if not os.path.exists(path):
        # first hit: resolve rid back from the mapping stored at
        # subtitles time, download, and cache
        rid = _sub_index_load().get("rid:" + key)
        if not rid:
            return jsonify({"error": "not found"}), 404
        parsed = _parse_rid("series", rid)
        if not parsed:
            return jsonify({"error": "not found"}), 404
        imdb_id, season, episode = parsed
        if not _resolve_subtitle(src, rid, imdb_id, season, episode):
            return jsonify({"error": "not found"}), 404
    with open(path, "rb") as f:
        data = f.read()
    return Response(data, mimetype="text/plain; charset=utf-8",
                    headers={"Access-Control-Allow-Origin": "*"})


@app.route("/subtitles/<vtype>/<rid>.json")
def subtitles(vtype, rid):
    return jsonify({"subtitles": _subtitle_entries(vtype, rid)})


@app.route("/srt/<src>/<key>.srt")
def srt(src, key):
    return _serve_srt(src, key)


@app.route("/")
def index():
    base = request.host_url.rstrip("/")
    return Response(
        "<h2>SM MAL SUB by Nandu10 \u2705</h2>"
        "<p>Malayalam subtitles from Msone + Movie Mirror + Team GOAT.</p>"
        "<p>Install in Stremio / Nuvio:<br>"
        f"<code>{base}/manifest.json</code></p>",
        mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
