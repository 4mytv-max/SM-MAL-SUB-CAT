#!/usr/bin/env python3
"""
SM MAL MERGED by Nandu10 — single service.

Catalog + meta at root, Malayalam subtitles under /sub.

Routes:
  /manifest.json                 -> SM MAL CATALOG v2 (catalog + meta)
  /catalog/<type>/<id>.json
  /meta/<type>/<id>.json
  /sub/manifest.json             -> SM MAL SUB (subtitles)
  /sub/subtitles/<type>/<id>.json
  /sub/srt/<src>/<key>.srt
  /subtitles/<type>/<id>.json    (legacy root subtitle routes, kept)
  /srt/<src>/<key>.srt
  /debug/msone/<rid>
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
from collections import Counter

from flask import Flask, jsonify, request, Response

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

POSTER = "https://image.tmdb.org/t/p/w500"
BG = "https://image.tmdb.org/t/p/w780"

UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")

# Official Msone subtitle addon (bypasses Cloudflare via download-manager URLs)
MSONE_OFFICIAL_API = "https://addon.malayalamsubtitles.org"

# TMDB API for IMDb -> title lookup (live subtitle search).
# Set TMDB_API_KEY in Render env vars. Live-fetch is skipped if unset.
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")

# OMDb API as fallback when TMDB fails (free 1000/day at omdbapi.com)
OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "")

# Live-fetch endpoints
MM_WP_API = "https://moviemirrorsubtitles.com/wp-json/wp/v2"
GOAT_HOME = "https://malayalamsubtitles.in/"
LIVE_URL_TTL = 7 * 24 * 3600   # found live SRT URLs rarely change

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


# IMDb ID overrides for titles missing them in mega_data (TMDB ID -> IMDb ID)
# These enable Torrentio/Comet to find streams
IMDB_OVERRIDES = {
    4327: "tt0096657",    # Mr. Bean (1990 series)
    1044: "tt0795176",    # Planet Earth (2006)
    68595: "tt5491994",   # Planet Earth II (2016)
    1430: "tt0081846",    # Cosmos: A Personal Voyage (1980)
    82953: "tt9130692",   # Dynasties (2018)
    95171: "tt10324164",  # Prehistoric Planet (2022)
    # --- Batch 2 (2026-10-08): 63 titles from systematic audit ---
    16591: "tt0996614",   # Galapagos (2006 tv)
    33189: "tt1508238",   # Apocalypse: The Second World War (2009 tv)
    41727: "tt2017109",   # Banshee (2013 tv)
    47089: "tt3215140",   # Nine: Nine Time Travels (2013 tv)
    61143: "tt3580170",   # God's Gift - 14 Days (2014 tv)
    64010: "tt5182866",   # Reply 1988 (2015 tv)
    65617: "tt6284006",   # Choco Bank (2016 tv)
    67391: "tt5966882",   # The K2 (2016 tv)
    67842: "tt6209442",   # Nightmare Teacher (2016 tv)
    68094: "tt6281238",   # The Teacher (2016 tv)
    69610: "tt6413646",   # Naked Fireman (2017 tv)
    70440: "tt6356086",   # Tunnel (2017 tv)
    71226: "tt10777760",  # One Sunny Day (2014 tv)
    71446: "tt6468322",   # Money Heist (2017 tv)
    71967: "tt6560040",   # The Forest (2017 tv)
    73544: "tt5743796",   # Warrior (2019 tv)
    73780: "tt7278424",   # Somehow 18 (2017 tv)
    74939: "tt7573686",   # Erased (2017 tv)
    76369: "tt7927936",   # FLAMES (2018 tv)
    79102: "tt7420880",   # Nude (2018 tv)
    82658: "tt9064400",   # Ms. Ma, Nemesis (2018 tv)
    86831: "tt9561862",   # Love, Death & Robots (2019 tv)
    92410: "tt5917374",   # Sex Chat with Pappu & Papa (2016 tv)
    94997: "tt11198330",  # House of the Dragon (2022 tv)
    110316: "tt10795658", # Alice in Borderland (2020 tv)
    113710: "tt13394544", # Lovestruck in the City (2020 tv)
    117376: "tt13433812", # Vincenzo (2021 tv)
    118932: "tt14900148", # Be My Boyfriend (2021 tv)
    125350: "tt14596414", # Mad for Each Other (2021 tv)
    126098: "tt14420552", # Maharani (2021 tv)
    133359: "tt14167390", # House of Secrets: The Burari Deaths (2021 tv)
    157239: "tt13623632", # Alien: Earth (2025 tv)
    197588: "tt13640670", # Man vs Bee (2022 tv)
    204597: "tt21099610", # Ishq Express (2022 tv)
    207333: "tt9892936",  # One Hundred Years of Solitude (2024 tv)
    212204: "tt27446493", # Twinkling Watermelon (2023 tv)
    224372: "tt27497448", # A Knight of the Seven Kingdoms (2026 tv)
    225171: "tt22202452", # Pluribus (2025 tv)
    129: "tt0245429",     # Spirited Away (2001 movie)
    83345: "tt10763262",  # My Romantic Some Recipe (2016 tv)
    95392: "tt11997412",  # Best Mistake (2019 tv)
    109237: "tt13026912", # Forbidden Love (2020 tv)
    117058: "tt13423446", # ONE FINE WEEK (2019 tv)
    1516738: "tt10676052",# Project Y / Fantastic Four (2025 movie)
    1369833: "tt23804378",# Three of Us (2022 movie)
    563987: "tt8458202",  # Pihu (2018 movie)
    534530: "tt7765910",  # Aravinda Sametha (2018 movie)
    479855: "tt7504256",  # Oye Ninne (2017 movie)
    818243: "tt3224288",  # Beyond the Clouds (2017 movie)
    467106: "tt6967980",  # Bareilly Ki Barfi (2017 movie)
    372226: "tt4991384",  # Visaranai (2016 movie)
    531526: "tt4373956",  # Clair Obscur (2016 movie)
    325138: "tt3495030",  # Dum Laga Ke Haisha (2015 movie)
    1455945: "tt2980794", # Highway (2014 movie)
    161064: "tt2417560",  # Filmistaan (2014 movie)
    212606: "tt2353767",  # A Thousand Times Good Night (2013 movie)
    72733: "tt1621642",   # Bangkok Traffic (Love) Story (2009 movie)
    84368: "tt0214915",   # Manichitrathazhu (1993 movie)
    959345: "tt14134794", # Dokgo Rewind (2018 movie)
    127763: "tt0379375",  # Matrubhoomi (2005 movie)
    695697: "tt13884658", # The 3rd Eye Murders (2020 movie)
    1780713: "tt0065571", # The Conformist (1970 movie)
    # --- Batch 3 (2026-10-08): additional from user request ---
    1104232: "tt27458539", # Vigilante (2023 tv, Korean drama)
    564610: "tt0179106",  # Bikini Seasons (1993 movie)
    760774: "tt13097932", # One Life (2023 movie)
    # --- Batch 4 (2026-10-08): from 12-title audit ---
    473216: "tt4603640",  # The Silence (2015, Marathi film)
    710859: "tt1041086",  # Goodbye Children Everywhere (BBC Timeshift S6E15)
}

# Name-based IMDb overrides for live rows where TMDB enrichment fails
# Format: "lowercase name (year)" -> (IMDb ID, TMDB poster_path)
NAME_OVERRIDES = {
    "one life (2023)": ("tt13097932", "/yvnIWt2j8VnDgwKJE2VMiFMa2Qo.jpg"),
}


def all_items():
    items = mega_load().get("items", [])
    # Apply IMDb ID overrides
    for it in items:
        tmdb = it.get("tmdb_id")
        if not it.get("imdb_id") and tmdb in IMDB_OVERRIDES:
            it["imdb_id"] = IMDB_OVERRIDES[tmdb]
            it["card_id"] = IMDB_OVERRIDES[tmdb]
    return items


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
        {"id": "smc_new_msone", "type": "movie",
         "name": "Msone New Releases", "kind": "live_msone"},
        {"id": "smc_new_goat", "type": "movie",
         "name": "Team GOAT New Releases", "kind": "live_goat"},
        {"id": "smc_new_mm", "type": "movie",
         "name": "Movie Mirror New Releases", "kind": "live_mm"},
        {"id": "smc_natsci_docs", "type": "series",
         "name": "Nature and Science Documentaries", "kind": "doc_natsci"},
        {"id": "smc_ko_new_m", "type": "movie",
         "name": "Korean New Movies", "kind": "ko_new", "media": "movie"},
        {"id": "smc_ko_new_s", "type": "series",
         "name": "Korean New Series", "kind": "ko_new", "media": "tv"},
        {"id": "smc_ko_all_m", "type": "movie",
         "name": "Korean Movies All Time", "kind": "ko_all", "media": "movie"},
        {"id": "smc_ko_all_s", "type": "series",
         "name": "Korean Series All Time", "kind": "ko_all", "media": "tv"},
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
    # Other documentaries (non-nature/science), movies + series
    other_docs = [it for it in all_items() if is_doc(it)
                  and doc_subject(it) not in ("nature", "science")]
    if other_docs:
        cats.append({"id": "smc_doc_other", "type": "movie",
                     "name": "Documentary Movies and Series",
                     "kind": "doc_other"})
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
    if kind == "live_msone":
        return _live_msone_releases()
    if kind == "live_goat":
        return _live_goat_releases()
    if kind == "live_mm":
        return _live_mm_releases()
    if kind == "doc_natsci":
        # nature & science documentary series (Discovery/NatGeo style)
        return [it for it in items
                if it["media"] == "tv" and is_doc(it)
                and doc_subject(it) in ("nature", "science")]
    if kind == "doc_other":
        # other documentaries (non-nature/science), movies + series
        return [it for it in items
                if is_doc(it)
                and doc_subject(it) not in ("nature", "science")]
    if kind == "media":
        items = [it for it in items if it["media"] == cat["media"]]
        return sorted(items, key=lambda x: (x.get("name") or "").lower())
    if kind == "latest":
        return [it for it in items if it["media"] == cat["media"]]
    if kind == "ko_new":
        # Korean new releases (newest first, pre-sorted)
        ko = [it for it in items
              if it["media"] == cat["media"] and it.get("lang") == "korean"]
        return ko[:50]
    if kind == "ko_all":
        # Korean all time (alphabetical)
        ko = [it for it in items
              if it["media"] == cat["media"] and it.get("lang") == "korean"]
        return sorted(ko, key=lambda x: (x.get("name") or "").lower())[:50]
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


# ---------------- series episodes (videos[]) ----------------
# Stremio/Nuvio need a "videos" array on series meta so stream addons
# (Torrentio/Comet) can resolve episode streams. Without it every
# series shows "Playback unavailable".
EP_TTL = 30 * 24 * 3600  # episode lists rarely change


def _ep_cache_load():
    try:
        with open(EP_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _ep_cache_save(c):
    tmp = EP_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f)
    os.replace(tmp, EP_CACHE)


def _tmdb_tv_id(it):
    """Numeric TMDB TV id for a series item, or None."""
    cid = it.get("card_id") or ""
    if cid.startswith("tmdb:") and cid[5:].isdigit():
        return cid[5:]
    if re.match(r"^tt\d+$", cid) and TMDB_API_KEY:
        try:
            data = _api_fetch_json(
                "https://api.themoviedb.org/3/find/%s?api_key=%s"
                "&external_source=imdb_id" % (cid, TMDB_API_KEY))
            tr = data.get("tv_results") or []
            if tr:
                return str(tr[0]["id"])
        except Exception:
            pass
    return None


def _series_videos(it):
    """Stremio videos[] for a series item. Cached 30d. None if unavailable."""
    if it.get("media") != "tv" or not TMDB_API_KEY:
        return None
    tmdb_id = _tmdb_tv_id(it)
    if not tmdb_id:
        return None
    # Prefer IMDb id in video ids (stream addons resolve tt..:s:e best)
    cid = it.get("card_id") or ""
    vid_prefix = cid if re.match(r"^tt\d+$", cid) else "tmdb:" + tmdb_id
    cache = _ep_cache_load()
    now = time.time()
    ck = "tv:" + tmdb_id
    e = cache.get(ck)
    if e and now - e.get("at", 0) < EP_TTL and e.get("videos"):
        return e["videos"]
    try:
        tv = _api_fetch_json(
            "https://api.themoviedb.org/3/tv/%s?api_key=%s"
            % (tmdb_id, TMDB_API_KEY))
        videos = []
        for s in tv.get("seasons") or []:
            sn = s.get("season_number")
            if not isinstance(sn, int) or sn < 1:
                continue  # skip "Specials"
            sd = _api_fetch_json(
                "https://api.themoviedb.org/3/tv/%s/season/%d?api_key=%s"
                % (tmdb_id, sn, TMDB_API_KEY))
            for ep in sd.get("episodes") or []:
                en = ep.get("episode_number")
                if not isinstance(en, int):
                    continue
                videos.append({
                    "id": "%s:%d:%d" % (vid_prefix, sn, en),
                    "title": "S%d E%d - %s" % (sn, en, ep.get("name") or ""),
                    "season": sn,
                    "episode": en,
                    "released": ep.get("air_date") or "",
                })
        if videos:
            cache[ck] = {"at": now, "videos": videos}
            _ep_cache_save(cache)
            return videos
    except Exception:
        pass
    return None


def _to_meta(it, with_videos=False):
    disp = it["name"] + (f" / {it['name_ml']}" if it.get("name_ml") else "")
    labels = [SITE_LABEL[s] for s in it.get("sources", []) if s in SITE_LABEL]
    urls = "\n".join(it.get("post_urls", {}).values())
    desc = (it.get("overview") or "")
    desc += ("\n\n\U0001F4DD Malayalam subtitles: " + ", ".join(labels)
             if labels else "")
    if urls:
        desc += "\n" + urls
    # poster: TMDB path first, then direct URL (GOAT homepage)
    poster = None
    if it.get("poster_path"):
        poster = f"{POSTER}{it['poster_path']}"
    elif it.get("poster_url"):
        poster = it["poster_url"]
    meta = {
        "id": it["card_id"],
        "type": "movie" if it["media"] == "movie" else "series",
        "name": disp,
        "poster": poster,
        "background": f"{BG}{it['backdrop_path']}" if it.get("backdrop_path") else None,
        "description": desc.strip(),
        "releaseInfo": str(it.get("year") or ""),
        "genres": [TMDB_GENRES[g] for g in (it.get("genre_ids") or [])
                   if g in TMDB_GENRES],
    }
    if with_videos and it.get("media") == "tv":
        vids = _series_videos(it)
        if vids:
            meta["videos"] = vids
    return meta


# ---------------- live new releases ----------------
# Fetched fresh from each site (cached 1h). Powers the top 3 rows:
# "New releases on MSONE / Team GOAT / Movie Mirror".
NEWREL_TTL = 3600  # 1 hour cache
_newrel_cache = {}
_newrel_lock = threading.Lock()

MSONE_RSS = "https://malayalamsubtitles.org/feed/"


def _tmdb_search_enrich(title_en, year):
    """Search TMDB for title+year. Returns dict with poster_path,
    overview, genre_ids, imdb_id, media, or None."""
    if not TMDB_API_KEY or not title_en:
        return None
    try:
        q = urllib.parse.quote(title_en)
        # try with year first, then without year as fallback
        for mtype in ("movie", "tv"):
            for try_year in ([year] if year else []) + [None]:
                if try_year:
                    yparam = (f"&year={try_year}" if mtype == "movie"
                              else f"&first_air_date_year={try_year}")
                else:
                    yparam = ""
                api = (f"https://api.themoviedb.org/3/search/{mtype}"
                       f"?api_key={TMDB_API_KEY}&query={q}{yparam}&page=1")
                data = _api_fetch_json(api, timeout=10)
                results = data.get("results") or []
                if results:
                    r = results[0]
                    # get imdb id via details
                    imdb_id = None
                    try:
                        det = _api_fetch_json(
                            f"https://api.themoviedb.org/3/{mtype}/{r['id']}"
                            f"?api_key={TMDB_API_KEY}"
                            f"&append_to_response=external_ids",
                            timeout=10)
                        imdb_id = (det.get("external_ids") or {}).get(
                            "imdb_id")
                    except Exception:
                        pass
                    return {
                        "poster_path": r.get("poster_path"),
                        "backdrop_path": r.get("backdrop_path"),
                        "overview": r.get("overview"),
                        "genre_ids": r.get("genre_ids") or [],
                        "imdb_id": imdb_id,
                        "media": "tv" if mtype == "tv" else "movie",
                    }
        return None
    except Exception:
        return None


def _omdb_enrich(title_en, year):
    """OMDb API fallback when TMDB fails. Returns dict with imdb_id,
    poster_path, overview, or None."""
    if not OMDB_API_KEY or not title_en:
        return None
    try:
        q = urllib.parse.quote(title_en)
        yparam = f"&y={year}" if year else ""
        api = (f"https://www.omdbapi.com/?apikey={OMDB_API_KEY}"
               f"&t={q}{yparam}&type=movie")
        data = _api_fetch_json(api, timeout=10)
        if data.get("Response") == "True":
            imdb_id = data.get("imdbID")
            poster = data.get("Poster")
            # OMDb returns "N/A" for missing poster
            if poster == "N/A":
                poster = None
            return {
                "imdb_id": imdb_id,
                "poster_path": poster,  # full URL from OMDb
                "poster_is_url": True,  # flag: not TMDB path
                "overview": data.get("Plot") if data.get("Plot") != "N/A" else "",
                "genre_ids": [],
                "media": "movie",
            }
        return None
    except Exception:
        return None


def _parse_msone_title(raw):
    """'Nine Puzzles / നയൻ പസിൽസ് (2025)' -> (en, ml, year)."""
    raw = html.unescape(raw or "").strip()
    year = None
    ym = re.search(r"\((19|20)\d{2}\)?\s*$", raw)
    if ym:
        year = re.search(r"(19|20)\d{2}", ym.group(0)).group(0)
        raw = raw[:ym.start()].strip()
    parts = [p.strip() for p in raw.split("/", 1)]
    en = parts[0] if parts else raw
    ml = parts[1] if len(parts) > 1 else None
    return en, ml, year


def _live_msone_releases():
    """10 latest Msone releases from RSS feed (live).
    Falls back to saved data if live fetch fails."""
    with _newrel_lock:
        e = _newrel_cache.get("msone")
        now = time.time()
        if e and now - e["at"] < NEWREL_TTL:
            return e["items"]
    items = []
    try:
        raw = _sub_fetch(MSONE_RSS, timeout=15, retries=2)
        xml = raw.decode("utf-8", errors="replace")
        for m in re.finditer(
                r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>",
                xml, re.S):
            title_raw = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1),
                               flags=re.S)
            link = m.group(2).strip()
            en, ml, year = _parse_msone_title(title_raw)
            slug = link.rstrip("/").split("/")[-1]
            if not en:
                continue
            enrich = _tmdb_search_enrich(en, year)
            # OMDb fallback when TMDB fails
            if not enrich or not enrich.get("imdb_id"):
                omdb = _omdb_enrich(en, year)
                if omdb and omdb.get("imdb_id"):
                    enrich = omdb
            # Check name-based overrides if still no luck
            if (not enrich or not enrich.get("poster_path")) and en and year:
                name_key = f"{en.lower()} ({year})"
                if name_key in NAME_OVERRIDES:
                    # Use override - create minimal enrich dict with poster
                    ov_imdb, ov_poster = NAME_OVERRIDES[name_key]
                    enrich = {
                        "imdb_id": ov_imdb,
                        "media": "movie",
                        "poster_path": ov_poster,
                        "overview": "",
                        "genre_ids": [],
                    }
            # skip items without posters (can't show in catalog)
            # unless we have an IMDb override (we can show without perfect poster)
            if not enrich or (not enrich.get("poster_path") and not enrich.get("imdb_id")):
                continue
            it = {
                "card_id": f"smcs:new:msone:{slug}",
                "name": en,
                "name_ml": ml,
                "media": (enrich or {}).get("media", "movie"),
                "poster_path": enrich.get("poster_path") if not enrich.get("poster_is_url") else None,
                "poster_url": enrich.get("poster_path") if enrich.get("poster_is_url") else None,
                "backdrop_path": enrich.get("backdrop_path"),
                "overview": enrich.get("overview") or "",
                "year": year,
                "genre_ids": enrich.get("genre_ids") or [],
                "sources": ["msone"],
                "post_urls": {"msone": link},
            }
            if enrich.get("imdb_id"):
                it["card_id"] = enrich["imdb_id"]
            items.append(it)
            # Don't break at 10 - process all RSS items to find overrides like One Life
            # We'll trim to 10 at the end, prioritizing items with IMDb IDs
        # end for loop - process all items
    except Exception:
        pass
    # Sort: items with IMDb IDs first, then by original order, take top 12
    items.sort(key=lambda x: (0 if x["card_id"].startswith("tt") else 1))
    items = items[:12]
    # fallback to saved data if live fetch gave nothing usable
    if not items:
        try:
            p = os.path.join(DATA_DIR, "mzone_data.json")
            with open(p, encoding="utf-8") as f:
                mz = json.load(f)
            for it in (mz.get("sections", {}).get("New Releases", [])[:10]):
                if not it.get("poster_path"):
                    continue
                # Use IMDb ID if available, else check overrides by TMDB ID, else fallback
                tmdb_id = it.get("id")
                card_id = it.get("imdb_id")
                if not card_id and tmdb_id:
                    try:
                        card_id = IMDB_OVERRIDES.get(int(tmdb_id))
                    except (ValueError, TypeError):
                        pass
                if not card_id:
                    card_id = f"smcs:new:msone:{tmdb_id}"
                items.append({
                    "card_id": card_id,
                    "name": it.get("name") or "",
                    "name_ml": it.get("name_ml"),
                    "media": "tv" if it.get("media") == "tv" else "movie",
                    "poster_path": it.get("poster_path"),
                    "backdrop_path": it.get("backdrop_path"),
                    "overview": it.get("overview") or "",
                    "year": it.get("year"),
                    "genre_ids": it.get("genre_ids") or [],
                    "sources": ["msone"],
                    "post_urls": {"msone": it.get("post_url") or ""},
                })
        except Exception:
            pass
    # FORCE-ADD One Life if not present (user requires it in Msone New Releases)
    one_life_ids = [it.get("card_id") for it in items]
    if "tt13097932" not in one_life_ids:
        items.insert(0, {
            "card_id": "tt13097932",
            "name": "One Life",
            "name_ml": "വൺ ലൈഫ്",
            "media": "movie",
            "poster_path": "/yvnIWt2j8VnDgwKJE2VMiFMa2Qo.jpg",
            "poster_url": None,
            "backdrop_path": None,
            "overview": "",
            "year": "2023",
            "genre_ids": [],
            "sources": ["msone"],
            "post_urls": {"msone": "https://malayalamsubtitles.org/languages/english/one-life-2023/"},
        })
        items = items[:12]
    with _newrel_lock:
        _newrel_cache["msone"] = {"at": time.time(), "items": items}
    return items


def _live_goat_releases():
    """Latest Team GOAT releases from homepage (live, newest first)."""
    with _newrel_lock:
        e = _newrel_cache.get("goat")
        now = time.time()
        if e and now - e["at"] < NEWREL_TTL:
            return e["items"]
    items = []
    try:
        raw = _sub_fetch(GOAT_HOME, timeout=15, retries=2)
        home_html = raw.decode("utf-8", errors="replace")
        pat = re.compile(
            r'<a href="(/release/[^"]+)">'
            r'<img src="([^"]+)"[^>]*>.*?'
            r'<p class="movie-name name">([^<]{3,120})</p>', re.S | re.I)
        seen = set()
        for m in pat.finditer(home_html):
            slug_path, img_src, t = (m.group(1),
                                     m.group(2), html.unescape(m.group(3)))
            if slug_path in seen:
                continue
            seen.add(slug_path)
            # direct poster from homepage (no TMDB needed)
            poster_url = urllib.parse.urljoin(GOAT_HOME, img_src)
            # "COCKTAIL 2 – കോക്ക്ടെയ്ൽ 2 (2026)" -> en/ml/year
            tm = re.search(r"\((19|20)\d{2}\)\s*$", t)
            year = tm.group(0).strip("()") if tm else None
            t2 = t[:tm.start()].strip() if tm else t.strip()
            parts = [p.strip() for p in re.split(r"\s+[–-]\s+", t2, 1)]
            en = parts[0].title() if parts else t2
            ml = parts[1] if len(parts) > 1 else None
            slug = slug_path.strip("/").split("/")[-1]
            enrich = _tmdb_search_enrich(en, year)
            it = {
                "card_id": f"smcs:new:goat:{slug}",
                "name": en,
                "name_ml": ml,
                "media": (enrich or {}).get("media", "movie"),
                "poster_path": (enrich or {}).get("poster_path"),
                "poster_url": poster_url,  # direct from homepage
                "backdrop_path": (enrich or {}).get("backdrop_path"),
                "overview": (enrich or {}).get("overview") or "",
                "year": year,
                "genre_ids": (enrich or {}).get("genre_ids") or [],
                "sources": ["goat"],
                "post_urls": {"goat": urllib.parse.urljoin(GOAT_HOME,
                                                           slug_path)},
            }
            if enrich and enrich.get("imdb_id"):
                it["card_id"] = enrich["imdb_id"]
            elif enrich and enrich.get("tmdb_id"):
                # Check overrides by TMDB ID
                try:
                    override = IMDB_OVERRIDES.get(int(enrich["tmdb_id"]))
                    if override:
                        it["card_id"] = override
                except (ValueError, TypeError):
                    pass
            items.append(it)
            if len(items) >= 15:
                break
    except Exception:
        pass
    with _newrel_lock:
        _newrel_cache["goat"] = {"at": time.time(), "items": items}
    return items


def _live_mm_releases():
    """Latest Movie Mirror releases from WP API (live, newest first)."""
    with _newrel_lock:
        e = _newrel_cache.get("mm")
        now = time.time()
        if e and now - e["at"] < NEWREL_TTL:
            return e["items"]
    items = []
    try:
        data = _api_fetch_json(
            f"{MM_WP_API}/posts?per_page=15&orderby=date&order=desc"
            f"&_fields=id,link,title,date", timeout=15)
        for p in data:
            title_raw = html.unescape(
                (p.get("title") or {}).get("rendered", ""))
            link = p.get("link", "")
            # "നോബഡി 2 ( Nobody 2 ) 2025" -> en/ml/year
            ym = re.search(r"\b(19|20)\d{2}\b", title_raw)
            year = ym.group(0) if ym else None
            # prefer english in parens
            pm = re.search(r"\(\s*([^)]+?)\s*\)", title_raw)
            en = pm.group(1).strip() if pm else title_raw
            en = re.sub(r"\b(19|20)\d{2}\b", "", en).strip(" ()")
            ml = None
            if pm:
                ml = title_raw[:pm.start()].strip(" ()")
            slug = link.rstrip("/").split("/")[-1]
            if not en:
                continue
            enrich = _tmdb_search_enrich(en, year)
            it = {
                "card_id": f"smcs:new:mm:{slug}",
                "name": en,
                "name_ml": ml,
                "media": (enrich or {}).get("media", "movie"),
                "poster_path": (enrich or {}).get("poster_path"),
                "backdrop_path": (enrich or {}).get("backdrop_path"),
                "overview": (enrich or {}).get("overview") or "",
                "year": year,
                "genre_ids": (enrich or {}).get("genre_ids") or [],
                "sources": ["mm"],
                "post_urls": {"mm": link},
            }
            if enrich and enrich.get("imdb_id"):
                it["card_id"] = enrich["imdb_id"]
            elif enrich and enrich.get("tmdb_id"):
                try:
                    override = IMDB_OVERRIDES.get(int(enrich["tmdb_id"]))
                    if override:
                        it["card_id"] = override
                except (ValueError, TypeError):
                    pass
            items.append(it)
    except Exception:
        pass
    with _newrel_lock:
        _newrel_cache["mm"] = {"at": time.time(), "items": items}
    return items


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
EP_CACHE = os.path.join(SUB_CACHE_DIR, "episodes.json")
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
            entry = {
                "id": s.get("id", ""),
                "url": surl,
                "lang": s.get("lang", "mal"),
            }
            # Preserve label if present (TV apps need it to display)
            if s.get("label"):
                entry["label"] = s["label"]
            out.append(entry)
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


# ---------------- addon 1: SM MAL CAT+SUB ----------------
@app.route("/manifest.json")
def manifest():
    base = request.url_root.rstrip("/")
    return jsonify({
        "id": "com.smmal.catsub.v2",
        "version": "2.3.1",
        "name": "SM MAL CATALOG v2",
        "description": "Malayalam movies, series & documentaries catalog "
                       "(Msone + Movie Mirror + Team GOAT combined) WITH "
                       "Malayalam subtitles from all three sites. Live search: "
                       "new subtitles appear automatically.",
        "logo": f"{base}/static/logo.png",
        "types": ["movie", "series"],
        "idPrefixes": ["tt", "tmdb:", "smcs:"],
        "resources": ["catalog", "meta"],
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
    return jsonify({"metas": metas[skip:skip + 100]})


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
        # look up live new-release items (msone/goat/mm rows) by any id
        for fn in (_live_msone_releases, _live_goat_releases,
                  _live_mm_releases):
            try:
                for cand in fn():
                    if cand.get("card_id") == mid:
                        it = cand
                        break
                if it:
                    break
            except Exception:
                pass
    if not it:
        return jsonify({"meta": {}})
    meta = _to_meta(it, with_videos=True)
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
            "url": f"{base}{prefix}/srt/{src}/{key}.srt",
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
    return Response(data, content_type="text/plain; charset=utf-8",
                    headers={"Access-Control-Allow-Origin": "*"})


@app.route("/subtitles/<vtype>/<rid>.json")
def subtitles(vtype, rid):
    resp = jsonify({"subtitles": _subtitle_entries("", vtype, rid)})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


@app.route("/srt/<src>/<key>.srt")
def srt(src, key):
    return _serve_srt(src, key)


# ---------------- /sub: standalone subtitle addon ----------------
@app.route("/sub/manifest.json")
def sub_manifest():
    resp = jsonify({
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
        "behaviorHints": {"configurable": False, "p2p": False},
    })
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


@app.route("/sub/subtitles/<vtype>/<rid>.json")
def sub_subtitles(vtype, rid):
    resp = jsonify({"subtitles": _subtitle_entries("/sub", vtype, rid)})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


@app.route("/sub/srt/<src>/<key>.srt")
def sub_srt(src, key):
    return _serve_srt(src, key)


@app.route("/")
def index():
    base = request.host_url.rstrip("/")
    return Response(
        "<h2>SM MAL MERGED by Nandu10 \u2705</h2>"
        "<p>Catalog addon (Stremio / Nuvio):<br>"
        f"<code>{base}/manifest.json</code></p>"
        "<p>Subtitle addon (Stremio / Nuvio):<br>"
        f"<code>{base}/sub/manifest.json</code></p>",
        mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
