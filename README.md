# My Internet Feed

A personal watch list of selected YouTube and Rumble channels. Open the page, play a video on YouTube or Rumble, and mark items as seen on this device.

Live site: https://statego2.github.io/My-Internet-Feed/

## Layout

- `docs/` — built site, served by GitHub Pages. `build.py` writes `index.html`, `data.json`, and `tv/index.html` here.
- `site/` — an earlier copy of the same build, not updated by the refresh workflow
- `build.py` — rebuilds the site from public YouTube RSS and one Rumble channel
- `sos_mapping.csv` — channel list (`include=1`, group, `sos=1`, filter: `minlen:SECONDS` / `title:TEXT`)
- `template.html`, `template_tv.html`, `fonts-embed.css`, `brand/` — used by the builder
- `supabase-setup.sql` — optional cross-device sync setup

## Rebuild

```bash
python3 build.py
```

`python3 build.py` fetches fresh (about 2 minutes the first time, about 10 seconds later thanks to `data/cache.json`). `python3 build.py --offline` rebuilds the HTML from `data/last.json`, or from `docs/data.json` when that cache is absent.

Python 3.9+ is enough for YouTube. The one Rumble channel needs `pip install playwright` and a Chrome/Chromium browser, because Cloudflare blocks plain HTTP. If Rumble or any other single channel fails, previous videos for that channel are kept and flagged stale, using `data/last.json` or, on a fresh checkout, `docs/data.json`.

GitHub Actions (`.github/workflows/refresh.yml`) runs that build every 3 hours and on demand, then commits `docs/index.html`, `docs/data.json`, and `docs/tv/index.html` to `main` only when they changed. The phone page is rendered from `template.html`, so the sync controls stay in each refresh. The TV page is `docs/tv/` (`template_tv.html`): large cards, remote arrows, no sync.

## Sync (optional)

Seen and hidden items can sync across devices through a free Supabase project. No server of our own. Run `supabase-setup.sql` in the Supabase SQL Editor, then paste the Project URL, the anon or publishable key, and a long sync secret into the page settings (the same values on every device). The secret is stored only in the browser, never in this repository.

Without those settings, seen and hidden state stays in the browser (`localStorage`) on this device only.
