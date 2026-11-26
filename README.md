# My Internet Feed

A personal watch list of selected YouTube and Rumble channels. Open the page, play a video on YouTube or Rumble, and mark items as seen on this device.

Live site: https://statego2.github.io/My-Internet-Feed/

## Layout

- `docs/` — built site, served by GitHub Pages. `build.py` writes `index.html` and `data.json` here.
- `site/` — an earlier copy of the same build, not updated by the refresh workflow
- `build.py` — rebuilds the site from public YouTube RSS and one Rumble channel
- `sos_mapping.csv` — channel list, groups, and SOS flags
- `template.html`, `fonts-embed.css`, `brand/` — used by the builder

## Rebuild

```bash
python3 build.py
```

Python 3.9+ is enough for YouTube. The one Rumble channel needs `pip install playwright` and a Chrome/Chromium browser, because Cloudflare blocks plain HTTP. If Rumble or any other single channel fails, that channel keeps its previous items from `data/last.json` or, on a fresh checkout, from `docs/data.json`.

GitHub Actions (`.github/workflows/refresh.yml`) runs that build every 3 hours and on demand, then commits `docs/index.html` and `docs/data.json` to `main` only when they changed.

Seen and hidden state stays in the browser (`localStorage`) and is not synced between devices.
