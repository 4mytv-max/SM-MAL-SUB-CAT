# SM MAL CAT+SUB by Nandu10

Malayalam movies, series & documentaries catalog **with** Malayalam subtitles —
Msone + Movie Mirror + Team GOAT combined.

## Install

Stremio / Nuvio → Addons → paste:

```
https://<your-service>.onrender.com/manifest.json
```

## What it does

- **Catalog**: 4,401 titles (movies, series, documentaries) in 84 rows —
  New Releases, Latest, genre rows, language rows, documentary subjects.
- **Subtitles**: one Malayalam subtitle entry per site that carries the title
  (`smsub:mm:…` Movie Mirror, `smsub:goat:…` Team GOAT, `smsub:msone:…` Msone).
- Subtitle files download on first use and are cached on disk (`sub_cache/`).

Note: Msone post pages sit behind Cloudflare which often blocks automated
fetches — the Msone entry is listed only when reachable, so you never see
dead subtitle entries.

## Files

```
app.py
requirements.txt
data/mega_data.json    merged catalog
data/mm_data.json      Movie Mirror items (subtitle URLs)
data/goat_data.json    Team GOAT items (subtitle URLs)
data/mzone_data.json   Msone items (post URLs)
static/logo.png
static/tiles/
```

## Deploy (Render, free)

1. Push this folder to a GitHub repo.
2. Render → New → Web Service → connect the repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`
3. Open `https://<your-service>.onrender.com/` for the manifest URL.

No API keys or environment variables needed.
