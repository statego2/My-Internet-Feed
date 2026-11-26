# My Internet Feed

A personal watch list of selected YouTube and Rumble channels. Open the page, play a video on YouTube or Rumble, and mark items as seen on this device.

Live site: https://statego2.github.io/My-Internet-Feed/

## Layout

- `docs/` — built site, served by GitHub Pages
- `site/` — the same build output
- `build.py` — rebuilds the site from public YouTube RSS and one Rumble channel
- `sos_mapping.csv` — channel list, groups, and SOS flags
- `template.html`, `fonts-embed.css`, `brand/` — used by the builder

## Rebuild

```bash
python3 build.py
```

Python 3.9+ is enough for YouTube. The one Rumble channel needs `pip install playwright` and a Chrome/Chromium browser, because Cloudflare blocks plain HTTP. After a build, copy `site/` to `docs/` before publishing.

Seen and hidden state stays in the browser (`localStorage`) and is not synced between devices. There is no scheduled rebuild in this repository; refresh the list by running the builder and pushing an update.
