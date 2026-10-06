#!/usr/bin/env python3
"""
SM MAL CAT+SUB by Nandu10 — standalone service.

Catalog + meta + subtitles. Merges Msone + Movie Mirror + Team GOAT
catalogs (same merged data as the MEGA addon) AND serves Malayalam
subtitles from all three sites.

Routes:
  /manifest.json
  /catalog/<type>/<id>.json
  /meta/<type>/<id>.json
  /subtitles/<type>/<id>.json
  /srt/<src>/<key>.srt
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
import zipfile
from collections import Counter

from flask import Flask, jsonify, request, Response

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

POSTER = "https://image.tmdb.org/t/p/w500"
BG = "https://image.tmdb.org/t/p/w780"

UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")

TMDB_GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy",
    36: "History", 27: "Horror", 10402: "Music", 9648: "Mystery",
    10749: "Romance", 878: "Science Fiction", 10770: "TV Movie",
    53: "Thriller", 10752: "War", 37: "Western", 10759: "Action & Adventure",
    10762: "Kids", 10763: "News", 10764: "Reality", 10765: "Sci-Fi & Fantasy",
    10766: "Soap", 10767: "Talk", 10768: "War & Politics",
}

LANG_NAMES = {
    "english": "English", "korean": "Korean", "hindi": "Hindi",
    "japanese": "Japanese", "french": "French", "spanish": "Spanish",
    "mandarin": "Mandarin", "telugu": "Telugu", "tamil": "Tamil",
    "malayalam": "Malayalam", "cantonese": "Cantonese",
    "indonesian": "Indonesian", "thai": "Thai", "german": "German",
    "italian": "Italian", "russian": "Russian", "portuguese": "Portuguese",
    "dutch": "Dutch", "swedish": "Swedish", "danish": "Danish",
    "norwegian": "Norwegian", "finnish": "Finnish", "polish": "Polish",
    "turkish": "Turkish", "arabic": "Arabic", "persian": "Persian",
    "urdu": "Urdu", "bengali": "Bengali", "punjabi": "Punjabi",
    "marathi": "Marathi", "kannada": "Kannada", "gujarati": "Gujarati",
    "vietnamese": "Vietnamese", "tagalog": "Tagalog", "filipino": "Filipino",
    "malay": "Malay", "hebrew": "Hebrew", "greek": "Greek",
    "ukrainian": "Ukrainian", "chinese": "Chinese", "czech": "Czech",
    "serbian": "Serbian", "romanian": "Romanian", "hungarian": "Hungarian",
    "dzongkha": "Dzongkha",
}

SITE_LABEL = {"msone": "Msone", "moviemirror": "Movie Mirror",
              "teamgoat": "Team GOAT"}

# Documentary subject buckets: (slug, label, genre_ids, keywords)
DOC_SUBJECTS = [
    ("nature", "Nature & Wildlife", None,
     ["nature", "wildlife", "wild ", "planet", "earth", "ocean", "sea ",
      "seas", "animal", "dinosaur", "jungle", "forest", "bird", "penguin",
      "octopus", "chimpanzee", "lion", "tiger", "bear", "shark", "reef",
      "safari", "attenborough", "antarctica", "amazon", "savanna"]),
    ("history", "History", [36],
     ["history", "historical", "world war", "ancient", "empire",
      "civilization", "dynasty", "kingdom", "medieval"]),
    ("science", "Science & Space", None,
     ["science", "space", "nasa", "universe", "cosmos", "quantum", "physics",
      "technology", "robot", "climate", "astronaut", "moon landing",
      "genetic", "evolution"]),
    ("music", "Music & Arts", [10402],
     ["music", "concert", "band ", "singer", "rock", "jazz", "symphony",
      "opera", "hip hop", "dj "]),
    ("crime", "True Crime", [80],
     ["crime", "murder", "killer", "heist", "mafia", "drug",
      "serial killer", "scam", "fraud", "prison"]),
    ("sports", "Sports", None,
     ["sport", "football", "soccer", "olympic", "fifa", "cricket", "boxing",
      "race", "marathon", "tennis", "golf", "formula 1", "wrestling"]),
    ("biography", "Biography", None,
     ["biograph", "life story", "untold story", "the life of",
      "portrait of"]),
]
DOC_SUBJECT_LABEL = {s: l for s, l, _, _ in DOC_SUBJECTS}
DOC_SUBJECT_LABEL["more"] = "More Documentaries"


def doc_subject(it):
    gids = it.get("genre_ids") or []
    text = ((it.get("name") or "") + " " + (it.get("overview") or "")).lower()
    for slug, _label, gids_match, keywords in DOC_SUBJECTS:
        if gids_match and any(g in gids for g in gids_match):
            return slug
        if any(k in text for k in keywords):
            return slug
    return "more"


TILES = [
    {"tile": "movies", "file": "movies.png", "name": "Movies",
     "desc": "Every movie with Malayalam subtitles — "
             "Msone + Movie Mirror + Team GOAT combined."},
    {"tile": "series", "file": "series.png", "name": "Series",
     "desc": "Every series with Malayalam subtitles — "
             "Msone + Movie Mirror + Team GOAT combined."},
    {"tile": "docs", "file": "docs.png", "name": "Documentaries",
     "desc": "Nature, history, science and more — documentaries with "
             "Malayalam subtitles from all three sites."},
]


# ---------------- catalog data ----------------
_mega_data = None
_mega_defs = None
_mega_hidden_defs = None
_mega_metas = {}
_mega_by_id = None


def _all_cat_defs():
    """All catalogs including hidden (for API lookup)."""
    return smc_catalog_defs() + (_mega_hidden_defs or [])


def mega_load():
    global _mega_data
    if _mega_data is None:
        p = os.path.join(DATA_DIR, "mega_data.json")
        with open(p, encoding="utf-8") as f:
            _mega_data = json.load(f)
    return _mega_data


def all_items():
    return mega_load().get("items", [])


def is_doc(it):
    return 99 in (it.get("genre_ids") or [])


def lang_name(slug):
    return LANG_NAMES.get(slug, slug.replace("-", " ").replace("_", " ").title())


def smc_catalog_defs():
    """Ordered home-page rows. Cached."""
    global _mega_defs, _mega_hidden_defs
    if _mega_defs is not None:
        return _mega_defs
    items = all_items()
    movies = [it for it in items if it["media"] == "movie"]
    series = [it for it in items if it["media"] == "tv"]
    docs_m = [it for it in movies if is_doc(it)]
    docs_s = [it for it in series if is_doc(it)]

    cats = [
        {"id": "smc_new", "type": "movie", "name": "New Releases",
         "kind": "latest_all"},
        {"id": "smc_latest_movies", "type": "movie",
         "name": "Latest Movies", "kind": "latest", "media": "movie"},
        {"id": "smc_latest_series", "type": "series",
         "name": "Latest Series", "kind": "latest", "media": "tv"},
        {"id": "smc_latest_docs", "type": "movie",
         "name": "Latest Documentaries", "kind": "latest_docs"},
    ]
    # Genre rows grouped: for each genre, Movies then Series then Docs
    mg = Counter(g for it in movies for g in (it.get("genre_ids") or [])
                 if g != 99 and g in TMDB_GENRES)
    sg = Counter(g for it in series for g in (it.get("genre_ids") or [])
                 if g != 99 and g in TMDB_GENRES)
    all_genres = Counter()
    all_genres.update(mg)
    all_genres.update(sg)
    for gid, _ in all_genres.most_common():
        gname = TMDB_GENRES[gid]
        if mg.get(gid):
            cats.append({"id": f"smc_mgenre_{gid}", "type": "movie",
                         "name": f"{gname}: Movies",
                         "kind": "genre", "genre_id": gid, "media": "movie"})
        if sg.get(gid):
            cats.append({"id": f"smc_sgenre_{gid}", "type": "series",
                         "name": f"{gname}: Series",
                         "kind": "genre", "genre_id": gid, "media": "tv"})
    # Language rows grouped similarly
    ml = Counter(it["lang"] for it in movies if it.get("lang"))
    sl = Counter(it["lang"] for it in series if it.get("lang"))
    all_langs = Counter()
    all_langs.update(ml)
    all_langs.update(sl)
    for lang, cnt in all_langs.most_common():
        if cnt < 10:
            continue
        lname = lang_name(lang)
        if ml.get(lang, 0) >= 10:
            cats.append({"id": f"smc_mlang_{lang}", "type": "movie",
                         "name": f"{lname}: Movies",
                         "kind": "lang", "lang": lang, "media": "movie"})
        if sl.get(lang, 0) >= 10:
            cats.append({"id": f"smc_slang_{lang}", "type": "series",
                         "name": f"{lname}: Series",
                         "kind": "lang", "lang": lang, "media": "tv"})
    # ---- Documentaries ----
    ds = Counter(doc_subject(it) for it in docs_m)
    order = [s for s, _, _, _ in DOC_SUBJECTS] + ["more"]
    for slug in order:
        if ds.get(slug):
            cats.append({"id": f"smc_dsub_{slug}", "type": "movie",
                         "name": f"Documentaries: {DOC_SUBJECT_LABEL[slug]}",
                         "kind": "doc_subject", "subject": slug})
    dl = Counter(it["lang"] for it in docs_m if it.get("lang"))
    for lang, cnt in dl.most_common():
        if cnt < 5:
            continue
        cats.append({"id": f"smc_dlang_{lang}", "type": "movie",
                     "name": f"Documentaries: {lang_name(lang)}",
                     "kind": "doc_lang", "lang": lang})
    if docs_s:
        cats.append({"id": "smc_docs_series", "type": "series",
                     "name": "Documentaries: Doc Series",
                     "kind": "docs_series"})
    _mega_defs = cats
    _mega_hidden_defs = []
    return cats


def _items_for_cat(cat):
    items = all_items()
    kind = cat["kind"]
    if kind == "tiles":
        return []
    if kind == "latest_all":
        return items  # pre-sorted newest-first
    if kind == "media":
        items = [it for it in items if it["media"] == cat["media"]]
        return sorted(items, key=lambda x: (x.get("name") or "").lower())
    if kind == "latest":
        return [it for it in items if it["media"] == cat["media"]]
    if kind == "genre":
        return [it for it in items
                if it["media"] == cat["media"]
                and cat["genre_id"] in (it.get("genre_ids") or [])]
    if kind == "lang":
        return [it for it in items
                if it["media"] == cat["media"] and it.get("lang") == cat["lang"]]
    if kind == "latest_docs":
        return [it for it in items
                if it["media"] == "movie" and is_doc(it)]
    if kind == "docs_all":
        items = [it for it in items
                 if it["media"] == "movie" and is_doc(it)]
        return sorted(items, key=lambda x: (x.get("name") or "").lower())
    if kind == "doc_subject":
        return [it for it in items
                if it["media"] == "movie" and is_doc(it)
                and doc_subject(it) == cat["subject"]]
    if kind == "doc_lang":
        return [it for it in items
                if it["media"] == "movie" and is_doc(it)
                and it.get("lang") == cat["lang"]]
    if kind == "docs_series":
        return [it for it in items
                if it["media"] == "tv" and is_doc(it)]
    return []


def _to_meta(it):
    disp = it["name"] + (f" / {it['name_ml']}" if it.get("name_ml") else "")
    labels = [SITE_LABEL[s] for s in it.get("sources", []) if s in SITE_LABEL]
    urls = "\n".join(it.get("post_urls", {}).values())
    desc = (it.get("overview") or "")
    desc += ("\n\n\U0001F4DD Malayalam subtitles: " + ", ".join(labels)
             if labels else "")
    if urls:
        desc += "\n" + urls
    return {
        "id": it["card_id"],
        "type": "movie" if it["media"] == "movie" else "series",
        "name": disp,
        "poster": f"{POSTER}{it['poster_path']}" if it.get("poster_path") else None,
        "background": f"{BG}{it['backdrop_path']}" if it.get("backdrop_path") else None,
        "description": desc.strip(),
        "releaseInfo": str(it.get("year") or ""),
        "genres": [TMDB_GENRES[g] for g in (it.get("genre_ids") or [])
                   if g in TMDB_GENRES],
    }


def smc_metas(cid):
    if cid in _mega_metas:
        return _mega_metas[cid]
    cat = next(c for c in _all_cat_defs() if c["id"] == cid)
    metas = [_to_meta(it) for it in _items_for_cat(cat)]
    metas = [m for m in metas if m.get("poster")]
    _mega_metas[cid] = metas
    return metas


def tile_metas():
    base = request.url_root.rstrip("/")
    metas = []
    for t in TILES:
        tid = f"smcs:tile_{t['tile']}"
        metas.append({
            "id": tid,
            "type": "movie",
            "name": t["name"],
            "poster": f"{base}/static/tiles/{t['file']}",
            "background": f"{base}/static/tiles/{t['file']}",
            "description": t["desc"],
        })
    return metas


def mega_by_id():
    global _mega_by_id
    if _mega_by_id is None:
        idx = {}
        for it in all_items():
            idx[it["card_id"]] = it
            if it.get("imdb_id"):
                idx[it["imdb_id"]] = it
            if it.get("tmdb_id"):
                idx[f"tmdb:{it['tmdb_id']}"] = it
        _mega_by_id = idx
    return _mega_by_id


def _top_lines(items, n=8):
    return "\n".join(
        f"\u2022 {it['name']}"
        + (f" / {it['name_ml']}" if it.get("name_ml") else "")
        + (f" ({it['year']})" if it.get("year") else "")
        for it in items[:n])


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
MISS_TTL = 6 * 3600       # remember misses for 6h
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
        elif src == "goat":
            e = goat_by_tt().get(imdb_id)
            item = _pick_series(e, season) if e else None
            if item and item.get("sub_url"):
                file_url = item["sub_url"]
                referer = item.get("post_url")
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


def _has_subtitle(src, imdb_id, season=None):
    """Fast index-only check: does this source list a subtitle?"""
    if src == "mm":
        e = mm_by_tt().get(imdb_id)
    elif src == "goat":
        e = goat_by_tt().get(imdb_id)
    elif src == "msone":
        # Msone needs a live check (Cloudflare) — the file URL doubles
        # as the availability signal.
        return bool(_msone_file_url(imdb_id, season))
    else:
        return False
    if not e:
        return False
    item = _pick_series(e, season)
    return bool(item and item.get("sub_url"))


SUB_SOURCES = [
    ("mm", "Movie Mirror"),
    ("goat", "Team GOAT"),
    ("msone", "Msone"),
]


# ---------------- addon 1: SM MAL CAT+SUB ----------------
@app.route("/manifest.json")
def manifest():
    base = request.url_root.rstrip("/")
    return jsonify({
        "id": "com.smmal.catsub",
        "version": "1.0.0",
        "name": "SM MAL CAT+SUB",
        "description": "Malayalam movies, series & documentaries catalog "
                       "(Msone + Movie Mirror + Team GOAT combined) WITH "
                       "Malayalam subtitles from all three sites.",
        "logo": f"{base}/static/logo.png",
        "types": ["movie", "series"],
        "idPrefixes": ["tt", "tmdb:", "smcs:"],
        "resources": ["catalog", "meta", "subtitles"],
        "catalogs": [
            {"type": c["type"], "id": c["id"], "name": c["name"],
             "extra": [{"name": "skip", "isRequired": False},
                       {"name": "search", "isRequired": False}]}
            for c in smc_catalog_defs()
        ],
    })


@app.route("/catalog/<ctype>/<cid>.json")
@app.route("/catalog/<ctype>/<cid>/skip=<int:skip>.json")
@app.route("/catalog/<ctype>/<cid>/search=<path:search>.json")
def catalog(ctype, cid, skip=0, search=None):
    if search is None:
        search = request.args.get("search")
    cat = next((c for c in _all_cat_defs() if c["id"] == cid), None)
    if not cat:
        return jsonify({"metas": []}), 404
    try:
        metas = smc_metas(cid)
        if search:
            q = search.lower()
            metas = [m for m in metas
                     if q in (m.get("name") or "").lower()]
    except Exception as e:
        return jsonify({"metas": [], "error": str(e)}), 502
    if "skip" not in request.view_args:
        try:
            skip = int(request.args.get("skip", "0"))
        except ValueError:
            skip = 0
    return jsonify({"metas": metas[skip:skip + 20]})


@app.route("/meta/<mtype>/<mid>.json")
def meta(mtype, mid):
    base = request.url_root.rstrip("/")
    for t in TILES:
        if mid == f"smcs:tile_{t['tile']}":
            if t["tile"] == "movies":
                items = [it for it in all_items() if it["media"] == "movie"]
            elif t["tile"] == "series":
                items = [it for it in all_items() if it["media"] == "tv"]
            else:
                items = [it for it in all_items()
                         if it["media"] == "movie" and is_doc(it)]
            return jsonify({"meta": {
                "id": mid,
                "type": "movie",
                "name": t["name"],
                "poster": f"{base}/static/tiles/{t['file']}",
                "background": f"{base}/static/tiles/{t['file']}",
                "description": (f"{t['desc']}\n\n{len(items)} titles."
                                f"\n\nNewest:\n{_top_lines(items)}").strip(),
            }})
    cat = next((c for c in _all_cat_defs() if c["id"] == mid), None)
    if cat:
        items = _items_for_cat(cat)
        first = items[0] if items else None
        meta = {
            "id": cat["id"],
            "type": cat["type"],
            "name": cat["name"],
            "poster": (f"{POSTER}{first['poster_path']}"
                       if first and first.get("poster_path") else None),
            "background": (f"{BG}{first['backdrop_path']}"
                           if first and first.get("backdrop_path") else None),
            "description": (f"{len(items)} titles in {cat['name']}."
                            f"\n\nTop titles:\n{_top_lines(items)}").strip(),
        }
        return jsonify({"meta": {k: v for k, v in meta.items() if v}})
    it = mega_by_id().get(mid)
    if not it:
        return jsonify({"meta": {}})
    meta = _to_meta(it)
    meta = {k: v for k, v in meta.items() if v}
    return jsonify({"meta": meta})


def _parse_rid(vtype, rid):
    """Returns (imdb_id, season, episode) or None."""
    rid = urllib.parse.unquote(rid)
    parts = rid.split(":")
    imdb_id = parts[0]
    if not re.match(r"^tt\d+$", imdb_id):
        return None
    season = episode = None
    if vtype == "series" and len(parts) >= 3:
        try:
            season, episode = int(parts[1]), int(parts[2])
        except ValueError:
            return None
    return imdb_id, season, episode


def _subtitle_entries(prefix, vtype, rid):
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
    return jsonify({"subtitles": _subtitle_entries("", vtype, rid)})


@app.route("/srt/<src>/<key>.srt")
def srt(src, key):
    return _serve_srt(src, key)


@app.route("/")
def index():
    base = request.host_url.rstrip("/")
    return Response(
        "<h2>SM MAL CAT+SUB by Nandu10 \u2705</h2>"
        "<p>Install in Stremio / Nuvio:<br>"
        f"<code>{base}/manifest.json</code></p>",
        mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
