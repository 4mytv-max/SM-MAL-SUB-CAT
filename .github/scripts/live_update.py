#!/usr/bin/env python3
"""Live catalog updater for GitHub Actions (SM-MAL-SUB-CAT).

Updates data/mm_data.json (Movie Mirror) and data/goat_data.json (Team GOAT)
with new posts from the source sites. Msone is NOT handled here (its site
blocks datacenter IPs; the assistant's cron keeps doing Msone).

STRICT: if any new post fails to parse/validate, that site's file is left
untouched and the script exits non-zero (no partial writes).

TMDB_API_KEY is read from the environment (GitHub Actions secret). Without
it, TMDB enrichment is skipped; site posters (og:image) still apply.

Self-contained: all parsing logic is inlined (no dependency on workspace
files) so it runs on a fresh GitHub runner.
"""
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

import requests

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SITE_GAP = 1.5
TMDB_GAP = 0.4

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")

OG_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
OG_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I)

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Connection": "close"})
_last_site = 0.0
_last_tmdb = 0.0


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def site_get(url, timeout=30, retries=3):
    """Fetch via curl subprocess (robust TLS/timeouts)."""
    global _last_site
    last_err = None
    for attempt in range(retries):
        wait = SITE_GAP - (time.time() - _last_site)
        if wait > 0:
            time.sleep(wait)
        try:
            r = subprocess.run(
                ["curl", "-sL", "--max-time", str(timeout), "-A", UA, url],
                capture_output=True, timeout=timeout + 10)
            if r.returncode != 0:
                raise IOError(f"curl rc={r.returncode}")
            if not r.stdout:
                raise IOError("curl empty reply")
            return r.stdout.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 * (attempt + 1))
        finally:
            _last_site = time.time()
    raise IOError(f"site_get failed after {retries}: {last_err}")


def tmdb_get(path, params=None, retries=3):
    global _last_tmdb
    params = dict(params or {})
    params["api_key"] = TMDB_API_KEY
    last_err = None
    for attempt in range(retries):
        wait = TMDB_GAP - (time.time() - _last_tmdb)
        if wait > 0:
            time.sleep(wait)
        try:
            r = _session.get("https://api.themoviedb.org/3" + path,
                             params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 * (attempt + 1))
        finally:
            _last_tmdb = time.time()
    raise IOError(f"tmdb_get failed after {retries}: {last_err}")


def tmdb_for(item, season_strip_re):
    """Resolve TMDB poster/backdrop/overview. Returns dict to merge."""
    out = {}
    imdb_id = item.get("imdb_id")
    if imdb_id and TMDB_API_KEY:
        try:
            res = tmdb_get(f"/find/{imdb_id}", {"external_source": "imdb_id"})
        except Exception as e:  # noqa: BLE001
            return {"_tmdb_error": str(e)[:80]}
        cands = []
        want_tv = item["type"] == "series"
        for r in res.get("tv_results", []):
            cands.append(("tv", r))
        for r in res.get("movie_results", []):
            cands.append(("movie", r))
        pick = None
        for media, r in cands:
            if (media == "tv") == want_tv:
                pick = (media, r)
                break
        if not pick and cands:
            pick = cands[0]
        if pick:
            media, r = pick
            out.update({
                "tmdb_id": r.get("id"),
                "media": media,
                "poster_path": r.get("poster_path"),
                "backdrop_path": r.get("backdrop_path"),
                "overview": r.get("overview") or "",
            })
            return out
    if TMDB_API_KEY and item.get("name_en"):
        want_tv = item["type"] == "series"
        path = "/search/tv" if want_tv else "/search/movie"
        query = item["name_en"]
        if want_tv:
            query = season_strip_re.sub("", query).strip(" -:") or query
        params = {"query": query, "language": "en-US"}
        if item.get("year") and item["year"].isdigit():
            params["year" if not want_tv else "first_air_date_year"] = item["year"]
        try:
            res = tmdb_get(path, params)
        except Exception as e:  # noqa: BLE001
            return {"_tmdb_error": str(e)[:80], "_fallback": True}
        results = res.get("results", [])
        if results:
            r = results[0]
            imdb_id = None
            try:
                ext = tmdb_get(
                    f"/{'tv' if want_tv else 'movie'}/{r['id']}/external_ids")
                imdb_id = ext.get("imdb_id")
            except Exception:  # noqa: BLE001
                pass
            out.update({
                "tmdb_id": r.get("id"),
                "imdb_id": imdb_id,
                "media": "tv" if want_tv else "movie",
                "poster_path": r.get("poster_path"),
                "backdrop_path": r.get("backdrop_path"),
                "overview": r.get("overview") or "",
                "_fallback": True,
            })
    return out


# ---------------------------------------------------------------- MM ---
MM_SITE = "https://moviemirrorsubtitles.com"
MM_SITEMAP = MM_SITE + "/post-sitemap.xml"
MM_TITLE_RE = re.compile(r"<title>([^<]+)</title>")
MM_IMDB_RE = re.compile(r"imdb\.com/title/(tt\d+)")
MM_ARTICLE_SECTION_RE = re.compile(r'"articleSection"\s*:\s*"([^"]+)"')
MM_DL_RE = re.compile(r"custom_download=([^\"'&\s]+)")
MM_SITE_SUFFIX = " - മൂവി മിറർ പരിഭാഷകൾ"
MM_TITLE_PAREN_RE = re.compile(r"^(.*?)\s*\(\s*(.*?)\s*\)\s*(\d{4})?\s*$")
MM_TITLE_YEAR_RE = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")
MM_SEASON_STRIP_RE = re.compile(
    r"[:\-]?\s*(season\s*\d+|സീസൺ\s*\d+|\bs\d+\b)", re.I)


def mm_parse_title(raw):
    t = html.unescape(raw).strip()
    if t.endswith(MM_SITE_SUFFIX):
        t = t[: -len(MM_SITE_SUFFIX)].strip()
    name_ml, name_en, year = "", "", ""
    if " | " in t:
        ml_part, rest = t.split(" | ", 1)
        name_ml = ml_part.strip()
        m = MM_TITLE_YEAR_RE.match(rest.strip())
        if m:
            name_en, year = m.group(1).strip(), m.group(2)
        else:
            name_en = rest.strip()
    else:
        m = MM_TITLE_PAREN_RE.match(t)
        if m and m.group(2).strip():
            name_ml = m.group(1).strip()
            name_en = m.group(2).strip()
            year = m.group(3) or ""
        else:
            name_en = t
    if not name_ml:
        name_ml = name_en
    return name_ml, name_en, year


def mm_parse_post(url, page):
    name_ml, name_en, year = mm_parse_title(
        MM_TITLE_RE.search(page).group(1) if MM_TITLE_RE.search(page) else "")
    m = MM_IMDB_RE.search(page)
    imdb_id = m.group(1) if m else None
    cats = []
    m = MM_ARTICLE_SECTION_RE.search(page)
    if m:
        cats = [c.strip() for c in html.unescape(m.group(1)).split(",")
                if c.strip()]
    low = [c.lower() for c in cats]
    ptype = "series" if ("series" in low or "tv series" in low) else "movie"
    genres = [c for c in cats
              if c.lower() not in ("movies", "movie", "series", "tv series")]
    sub_url = None
    m = MM_DL_RE.search(page)
    if m:
        sub_url = urllib.parse.unquote(m.group(1))
        if sub_url.startswith("/"):
            sub_url = MM_SITE + sub_url
    return {"post_url": url, "name_ml": name_ml, "name_en": name_en,
            "year": year, "imdb_id": imdb_id, "type": ptype,
            "genres": genres, "sub_url": sub_url}


def mm_sub_url_matches(item):
    """Guard against wrong-post download links (Take Shelter incident)."""
    url = (item.get("sub_url") or "")
    name = (item.get("name_en") or "")
    if not url or not name:
        return False
    fname = urllib.parse.unquote(url).rsplit("/", 1)[-1].lower()
    words = [w for w in re.split(r"[^a-z0-9]+", name.lower()) if len(w) > 3]
    return any(w in fname for w in words)


def update_mm():
    path = os.path.join(BASE, "data", "mm_data.json")
    data = json.load(open(path, encoding="utf-8"))
    known = {it["post_url"] for it in data["items"]}
    # IMDb dedup: skip reposts of the same title under a new URL
    # (Movie Mirror sometimes reposts the same IMDb item).
    known_imdb = {it.get("imdb_id") for it in data["items"] if it.get("imdb_id")}
    log(f"MM: known items: {len(known)}, known IMDb IDs: {len(known_imdb)}")
    sm = site_get(MM_SITEMAP)
    urls = re.findall(r"<loc>(?:<!\[CDATA\[)?([^<\]]+?)(?:\]\]>)?</loc>", sm)
    lastmods = re.findall(
        r"<lastmod>(?:<!\[CDATA\[)?([^<\]]+?)(?:\]\]>)?</lastmod>", sm)
    lm = dict(zip(urls, lastmods))
    log(f"MM: sitemap urls: {len(urls)}")
    new_urls = [u for u in urls if u not in known]
    log(f"MM: new urls: {len(new_urls)}")
    if not new_urls:
        log("MM: nothing new; mm_data.json untouched")
        return 0
    new_items, failed = [], []
    for u in new_urls:
        try:
            page = site_get(u)
            item = mm_parse_post(u, page)
            if not item.get("name_en"):
                raise ValueError("empty title parse")
            # IMDb dedup: skip if this title already exists under another URL.
            if item.get("imdb_id") and item["imdb_id"] in known_imdb:
                log(f"MM SKIP {u}: IMDb {item['imdb_id']} already in catalog "
                    f"({item.get('name_en')}) — repost, not new")
                continue
            if not mm_sub_url_matches(item):
                raise ValueError(
                    f"sub_url mismatch: {item.get('sub_url')!r} vs "
                    f"title {item.get('name_en')!r}")
            m = OG_RE.search(page) or OG_RE2.search(page)
            if m:
                item["poster_url"] = m.group(1)
            item["_lastmod"] = lm.get(u, "")
            item.update(tmdb_for(item, MM_SEASON_STRIP_RE))
            new_items.append(item)
            log(f"MM OK {item['name_en']} ({item.get('year')}) "
                f"tmdb={'yes' if item.get('tmdb_id') else 'no'}")
        except Exception as e:  # noqa: BLE001
            failed.append(u)
            log(f"MM FAIL {u}: {e!r}")
    if failed:
        log(f"MM: {len(failed)} failed; NOT writing mm_data.json")
        return 2
    order = {u: i for i, u in enumerate(urls)}
    items = new_items + data["items"]
    items.sort(key=lambda it: order.get(it["post_url"], 10 ** 9))
    data["items"] = items
    data["total"] = len(items)
    data["generated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    log(f"MM WROTE {path}: total={len(items)} (+{len(new_items)})")
    return 0


# --------------------------------------------------------------- GOAT ---
GOAT_SITE = "https://malayalamsubtitles.in"
GOAT_TITLE_RE = re.compile(r"<title>([^<]+)</title>")
GOAT_IMDB_RE = re.compile(r"imdb\.com/title/(tt\d+)")
GOAT_DL_RE = re.compile(r"https://wp\.malayalamsubtitles\.in/download/\d+")
GOAT_TABLE_ROW_RE = re.compile(
    r"<tr[^>]*>\s*<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", re.S)
GOAT_SITE_SUFFIX = " - Team GOAT Malayalam Subtitles"
GOAT_TITLE_SPLIT_RE = re.compile(
    r"^(.*?)\s*[–—-]\s*(.*?)\s*\((\d{4})\)\s*$")
GOAT_TITLE_YEAR_RE = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")
GOAT_SEASON_STRIP_RE = re.compile(
    r"[:\-]?\s*(season\s*\d+|സീസൺ\s*\d+|\bs\d+\b)", re.I)


def goat_parse_title(raw):
    t = html.unescape(raw).strip()
    if t.endswith(GOAT_SITE_SUFFIX):
        t = t[: -len(GOAT_SITE_SUFFIX)].strip()
    name_en, name_ml, year = "", "", ""
    m = GOAT_TITLE_SPLIT_RE.match(t)
    if m:
        name_en, name_ml, year = (m.group(1).strip(), m.group(2).strip(),
                                  m.group(3))
    else:
        m = GOAT_TITLE_YEAR_RE.match(t)
        if m:
            name_en, year = m.group(1).strip(), m.group(2)
        else:
            name_en = t
    if not name_ml:
        name_ml = name_en
    return name_en, name_ml, year


def goat_strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def goat_parse_post(url, page, ptype):
    name_en, name_ml, year = goat_parse_title(
        GOAT_TITLE_RE.search(page).group(1)
        if GOAT_TITLE_RE.search(page) else "")
    m = GOAT_IMDB_RE.search(page)
    imdb_id = m.group(1) if m else None
    genres, lang, director, translator = [], "", "", ""
    m = re.search(r"<table.*?</table>", page, re.S)
    if m:
        for th, td in GOAT_TABLE_ROW_RE.findall(m.group(0)):
            key = goat_strip_tags(th).rstrip(" :")
            val = goat_strip_tags(td)
            if key == "Genre":
                genres = [g.strip() for g in val.split(",") if g.strip()]
            elif key == "ഭാഷ":
                lang = val
            elif key == "സംവിധാനം":
                director = val
            elif key == "പരിഭാഷ":
                translator = val
    sub_url = None
    m = GOAT_DL_RE.search(page)
    if m:
        sub_url = m.group(0)
    poster = None
    m = re.search(r"/img/([A-Za-z0-9_-]+)-(?:1111|300)\.webp", page)
    if m:
        cand = m.group(0)
        if "goatblack" not in cand and "t9wzKR8Zt4" not in cand \
                and "redlogo" not in cand:
            poster = GOAT_SITE + cand
    return {"post_url": url, "name_en": name_en, "name_ml": name_ml,
            "year": year, "imdb_id": imdb_id, "type": ptype,
            "genres": genres, "language": lang, "director": director,
            "translator": translator, "sub_url": sub_url,
            "site_poster": poster}


def goat_collect_listing_urls():
    urls, seen = [], set()
    for section, ptype in (("movies", "movie"), ("series", "series")):
        page = 0
        while True:
            url = (f"{GOAT_SITE}/{section}/" if page == 0
                   else f"{GOAT_SITE}/{section}/{page}/")
            try:
                html_page = site_get(url)
            except Exception as e:  # noqa: BLE001
                log(f"GOAT listing {url} failed: {e}")
                break
            title = GOAT_TITLE_RE.search(html_page)
            title = title.group(1) if title else ""
            if "Home -" in title:
                break
            for m in re.finditer(r'href="(/release/[a-z0-9-]+)/?"', html_page):
                u = GOAT_SITE + m.group(1) + "/"
                if u not in seen:
                    seen.add(u)
                    urls.append((u, ptype))
            page += 1
    return urls


def update_goat():
    path = os.path.join(BASE, "data", "goat_data.json")
    data = json.load(open(path, encoding="utf-8"))
    items = data.get("items") or data.get("archive") or []
    known = {it["post_url"] for it in items}
    # IMDb dedup: skip reposts of the same title under a new URL.
    known_imdb = {it.get("imdb_id") for it in items if it.get("imdb_id")}
    log(f"GOAT: known items: {len(known)}, known IMDb IDs: {len(known_imdb)}")
    listing = goat_collect_listing_urls()
    log(f"GOAT: listing urls: {len(listing)}")
    if not listing:
        log("GOAT: listing walk returned nothing; goat_data.json untouched")
        return 2
    new_urls = [(u, pt) for (u, pt) in listing if u not in known]
    log(f"GOAT: new urls: {len(new_urls)}")
    if not new_urls:
        log("GOAT: nothing new; goat_data.json untouched")
        return 0
    new_items, failed = [], []
    for u, pt in new_urls:
        try:
            page = site_get(u)
            item = goat_parse_post(u, page, pt)
            if not item.get("name_en"):
                raise ValueError("empty title parse")
            # IMDb dedup: skip if this title already exists under another URL.
            if item.get("imdb_id") and item["imdb_id"] in known_imdb:
                log(f"GOAT SKIP {u}: IMDb {item['imdb_id']} already in catalog "
                    f"({item.get('name_en')}) — repost, not new")
                continue
            m = OG_RE.search(page) or OG_RE2.search(page)
            if m:
                item["poster_url"] = m.group(1)
            item.update(tmdb_for(item, GOAT_SEASON_STRIP_RE))
            new_items.append(item)
            log(f"GOAT OK {item['name_en']} ({item.get('year')}) "
                f"tmdb={'yes' if item.get('tmdb_id') else 'no'}")
        except Exception as e:  # noqa: BLE001
            failed.append(u)
            log(f"GOAT FAIL {u}: {e!r}")
    if failed:
        log(f"GOAT: {len(failed)} failed; NOT writing goat_data.json")
        return 2
    key = "items" if "items" in data else "archive"
    data[key] = new_items + data[key]
    data["total"] = len(data[key])
    data["generated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    log(f"GOAT WROTE {path}: total={len(data[key])} (+{len(new_items)})")
    return 0


def main():
    log("LIVE catalog update starting")
    log(f"TMDB key: {'present' if TMDB_API_KEY else 'MISSING - enrichment off'}")
    rc_mm = update_mm()
    rc_goat = update_goat()
    log(f"DONE mm={rc_mm} goat={rc_goat}")
    return 0 if (rc_mm == 0 and rc_goat == 0) else 2


if __name__ == "__main__":
    sys.exit(main())
