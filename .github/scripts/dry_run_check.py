#!/usr/bin/env python3
"""DRY-RUN new-post check for the SM MAL catalog (GitHub Actions).

Fetches the three source listing endpoints, compares them against the
repo's data files, and prints how many new posts each site has.

Writes NOTHING to the repo. Exit 0 = all sites reachable,
exit 2 = one or more sites unreachable (still no writes).
"""
import json
import os
import re
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
GAP = 1.5  # polite gap between requests to a site

MM_SITEMAP = "https://moviemirrorsubtitles.com/post-sitemap.xml"
GOAT_SITE = "https://malayalamsubtitles.in"
MSOEN_RSS = "https://malayalamsubtitles.org/feed/"

_last = 0.0


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch(url, timeout=40, retries=2):
    """Fetch a URL via curl (robust TLS/timeouts). Returns (text, http_code)."""
    global _last
    last_err = None
    for attempt in range(retries + 1):
        wait = GAP - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = subprocess.run(
                ["curl", "-sL", "--max-time", str(timeout),
                 "-A", UA, "-o", "-", "-w", "\n%{http_code}", url],
                capture_output=True, timeout=timeout + 15)
            if r.returncode != 0:
                raise IOError(f"curl rc={r.returncode}")
            if not r.stdout:
                raise IOError("curl empty reply")
            out = r.stdout.decode("utf-8", errors="replace")
            body, _, code = out.rstrip().rpartition("\n")
            return body, code.strip()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 * (attempt + 1))
        finally:
            _last = time.time()
    raise IOError(f"fetch failed for {url}: {last_err}")


def snippet(text, n=300):
    """First n chars, single line, for diagnostics."""
    return re.sub(r"\s+", " ", text[:n])


def load_known(path, keys=("post_url",)):
    """Load known post URLs from a catalog data file (defensive shapes)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        log(f"WARN: cannot read {path}: {e}")
        return set()
    items = data.get("items") or data.get("archive") or []
    known = set()
    for it in items:
        for k in keys:
            u = it.get(k)
            if u:
                known.add(u.rstrip("/"))
    return known


def check_mm(known):
    t0 = time.time()
    sm, code = fetch(MM_SITEMAP)
    urls = re.findall(r"<loc>(?:<!\[CDATA\[)?([^<\]]+?)(?:\]\]>)?</loc>", sm)
    new = [u for u in urls if u.rstrip("/") not in known]
    log(f"Movie Mirror: http={code} sitemap has {len(urls)} posts, "
        f"{len(new)} not in catalog ({time.time()-t0:.0f}s)")
    if not urls:
        log(f"   DIAG sitemap head: {snippet(sm)}")
    for u in new[:10]:
        log(f"   NEW: {u}")
    return len(new)


def check_goat(known):
    t0 = time.time()
    found = set()
    for section in ("movies", "series"):
        page, code = fetch(f"{GOAT_SITE}/{section}/")
        if section == "movies":
            log(f"Team GOAT: http={code}")
        for m in re.finditer(r'href="(/release/[a-z0-9-]+)/?"', page):
            found.add(GOAT_SITE + m.group(1).rstrip("/") + "/")
    new = [u for u in found if u.rstrip("/") not in known]
    log(f"Team GOAT: first listing pages have {len(found)} posts, "
        f"{len(new)} not in catalog ({time.time()-t0:.0f}s)")
    for u in sorted(new)[:10]:
        log(f"   NEW: {u}")
    return len(new)


def check_msone(known):
    t0 = time.time()
    rss, code = fetch(MSOEN_RSS)
    links = re.findall(r"<item>.*?<link>([^<]+)</link>", rss, re.S)
    links = [l.strip() for l in links]
    new = [l for l in links if l.rstrip("/") not in known]
    log(f"Msone: http={code} RSS feed has {len(links)} items, "
        f"{len(new)} not in catalog ({time.time()-t0:.0f}s)")
    if not links:
        log(f"   DIAG feed head: {snippet(rss)}")
    for l in new[:10]:
        log(f"   NEW: {l}")
    return len(new)


def main():
    t0 = time.time()
    log("DRY-RUN new-post check starting (no writes will be made)")
    known_mm = load_known(os.path.join(BASE, "data", "mm_data.json"))
    known_goat = load_known(os.path.join(BASE, "data", "goat_data.json"))
    known_msone = load_known(os.path.join(BASE, "data", "mzone_data.json"))
    log(f"known in repo: MM={len(known_mm)} GOAT={len(known_goat)} "
        f"Msone={len(known_msone)}")

    failures = []
    results = {}
    for name, fn, known in (("mm", check_mm, known_mm),
                            ("goat", check_goat, known_goat),
                            ("msone", check_msone, known_msone)):
        try:
            results[name] = fn(known)
        except Exception as e:  # noqa: BLE001
            log(f"ERROR: {name} check failed: {e}")
            failures.append(name)

    log(f"DONE in {time.time()-t0:.0f}s — new posts: {results} "
        f"(dry-run, nothing written)")
    if failures:
        log(f"UNREACHABLE: {', '.join(failures)}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
