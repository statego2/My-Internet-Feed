#!/usr/bin/env python3
"""My Internet Feed - builds the static site in ./docs/ (index.html + data.json + tv/index.html).

  python3 build.py                 # fetch everything fresh, rebuild docs/
  python3 build.py --offline       # rebuild HTML from data/last.json or docs/data.json
  python3 build.py --days 14 --per 6

Sources of truth:
  sos_mapping.csv   channel list (edit include/group/sos/filter columns)
  data/cache.json   video durations/short flags (immutable, speeds up reruns)
  data/last.json    last successful fetch (also used as a per-source fallback)
  docs/data.json    published snapshot, used when data/last.json has nothing for a failed source
Needs: Python 3.9+, stdlib only. Rumble needs `pip install playwright` + a Chrome/Chromium
(optional: if it fails, last known Rumble videos are kept and marked stale).
One channel failing does not drop the rest of the feed and does not fail the build.
"""
import argparse, base64, csv, json, os, re, shutil, sys, time, html, urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, 'docs')
DATA = os.path.join(HERE, 'data')
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'
TITLE = 'My Internet Feed'
GROUPS = [('politics', 'Πολιτικός σχολιασμός & γεωπολιτική'), ('tech', 'Tech'),
          ('faith', 'Πίστη, πολιτισμός & podcasts'), ('fitness', 'Γυμναστική')]
NS = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def http(url, redirect=True, timeout=25, retries=2):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9',
                                               'Cookie': 'CONSENT=YES+1; SOCS=CAI'})
    op = urllib.request.build_opener() if redirect else urllib.request.build_opener(_NoRedirect)
    for i in range(retries + 1):
        try:
            r = op.open(req, timeout=timeout)
            return r.status, r.read().decode('utf8', 'replace')
        except urllib.error.HTTPError as e:
            if not redirect and 300 <= e.code < 400:
                return e.code, ''
            if e.code in (404, 410):
                return e.code, ''
            err = e
        except Exception as e:
            err = e
        time.sleep(1 + i)
    return 0, ''


def load_json(p, default):
    try:
        return json.load(open(p, encoding='utf8'))
    except Exception:
        return default


def read_mapping():
    rows = list(csv.DictReader(open(os.path.join(HERE, 'sos_mapping.csv'), encoding='utf-8-sig')))
    out = []
    for r in rows:
        if r['include'].strip() != '1' or not r['id'].strip():
            continue
        out.append(dict(his=r['his_name'], title=r['channel_title'], id=r['id'].strip(), platform=r['platform'],
                        group=r['group'], sos=r['sos'].strip() == '1', filter=r['filter'].strip(),
                        url=r['url'], confidence=r['confidence']))
    return out


def parse_filter(f):
    minlen, title = 0, None
    for part in [p for p in f.split(';') if p]:
        if part.startswith('minlen:'):
            minlen = int(part[7:])
        elif part.startswith('title:'):
            title = part[6:].lower()
    return minlen, title


# ---------------- YouTube ----------------
def yt_details(vid, cache):
    c = cache.get(vid)
    if c and not c.get('live'):
        return c
    st, h = http('https://www.youtube.com/watch?v=' + vid)
    m = re.search(r'"lengthSeconds":"(\d+)"', h)
    if not m:
        return None
    ln = int(m.group(1))
    live = '"isLive":true' in h
    upcoming = '"isUpcoming":true' in h
    short = False
    if ln <= 185 and not live:
        s, _ = http('https://www.youtube.com/shorts/' + vid, redirect=False)
        short = (s == 200)
    c = dict(len=ln, live=live, upcoming=upcoming, short=short)
    cache[vid] = c
    return c


def fetch_yt(ch, days, per, cache):
    st, x = http('https://www.youtube.com/feeds/videos.xml?channel_id=' + ch['id'])
    if not x:
        return None, f"RSS HTTP {st}"
    try:
        root = ET.fromstring(x)
    except ET.ParseError:
        return None, f"RSS parse error HTTP {st}"
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    minlen, tfilter = parse_filter(ch['filter'])
    author = root.findtext('a:author/a:name', default=ch['title'], namespaces=NS)
    vids = []
    unresolved = 0
    for e in root.findall('a:entry', NS):
        vid = e.findtext('yt:videoId', namespaces=NS)
        title = e.findtext('a:title', namespaces=NS) or ''
        pub = datetime.fromisoformat(e.findtext('a:published', namespaces=NS))
        if pub < cutoff:
            continue
        if tfilter and tfilter not in title.lower():
            continue
        d = yt_details(vid, cache)
        # A bot wall returns HTTP 200 with no duration. That is a failure, not an empty channel.
        if not d:
            unresolved += 1
            continue
        if d['upcoming'] or d['short']:
            continue
        if d['len'] == 0 and not d['live']:
            continue
        if d['len'] < minlen and not d['live']:
            continue
        vids.append(dict(id='yt:' + vid, title=title, ch=ch['title'], chid=ch['id'], platform='youtube',
                         date=pub.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), dur=d['len'],
                         live=d['live'], url='https://www.youtube.com/watch?v=' + vid,
                         thumb=f'https://i.ytimg.com/vi/{vid}/mqdefault.jpg'))
        if len(vids) >= per:
            break
    if not vids and unresolved:
        return None, f"video details unavailable ({unresolved})"
    return vids, None


# ---------------- Rumble (best effort; Cloudflare => needs a real browser) ----------------
def fetch_rumble(ch, days, per):
    from playwright.sync_api import sync_playwright
    minlen, tfilter = parse_filter(ch['filter'])
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with sync_playwright() as p:
        kw = dict(headless=True, args=['--no-sandbox'])
        b = None
        for exe in ('/usr/bin/google-chrome', '/usr/bin/chromium', None):
            try:
                if exe and not os.path.exists(exe):
                    continue
                b = p.chromium.launch(executable_path=exe, **kw) if exe else p.chromium.launch(**kw)
                break
            except Exception:
                b = None
        if not b:
            raise RuntimeError('no browser')
        pg = b.new_page(user_agent=UA)
        pg.goto(f"https://rumble.com/c/{ch['id']}/videos", wait_until='domcontentloaded')
        pg.wait_for_timeout(6000)
        h = pg.content()
        b.close()
    items = []
    for m in re.finditer(r'<rum-card-video\b([^>]*)>', h):
        a = dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(1)))
        if 'video-title' not in a or not a.get('time'):
            continue
        items.append(a)
    if not items:
        raise RuntimeError('no videos parsed (blocked?)')
    out = []
    for a in items:
        pub = datetime.fromisoformat(a['time'])
        if pub < cutoff:
            continue
        dur = int(a.get('duration') or 0)
        title = html.unescape(a['video-title'])
        if dur < max(minlen, 1) or (tfilter and tfilter not in title.lower()):
            continue
        url = a['url'].split('?')[0]
        out.append(dict(id='rb:' + a['video-id'], title=title, ch=ch['title'], chid=ch['id'], platform='rumble',
                        date=pub.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), dur=dur, live=False,
                        url=url, thumb=a.get('src', '')))
    out.sort(key=lambda v: v['date'], reverse=True)
    return out[:per]


# ---------------- build ----------------
def published_snapshot():
    """Feed currently served from docs/. Present on a fresh CI checkout; data/ is not."""
    data = load_json(os.path.join(SITE, 'data.json'), None)
    return data if isinstance(data, dict) else {}


def load_last():
    """Prefer the local cache, then the published docs/data.json snapshot."""
    last = load_json(os.path.join(DATA, 'last.json'), None)
    if isinstance(last, dict) and last.get('videos'):
        return last
    return published_snapshot()


def videos_for(snapshot, chid):
    if not isinstance(snapshot, dict):
        return []
    vids = snapshot.get('videos')
    if not isinstance(vids, list):
        return []
    return [x for x in vids if isinstance(x, dict) and x.get('chid') == chid]


def collect(args):
    chans = read_mapping()
    cache = load_json(os.path.join(DATA, 'cache.json'), {})
    last = load_last()
    published = published_snapshot()
    videos, errors, stale = [], [], []

    def job(ch):
        try:
            if ch['platform'] == 'youtube':
                v, err = fetch_yt(ch, args.days, args.per, cache)
                return ch, v, err
            return ch, fetch_rumble(ch, args.days, args.per), None
        except Exception as e:
            return ch, None, str(e)

    yt = [c for c in chans if c['platform'] == 'youtube']
    rb = [c for c in chans if c['platform'] != 'youtube']
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(job, yt))
    for ch in rb:
        results.append(job(ch))
    for ch, v, err in results:
        if v is None:
            errors.append(f"{ch['title']}: {err}")
            # Keep this channel's last items. data/ is gitignored, so CI falls back to docs/data.json.
            old = videos_for(last, ch['id']) or videos_for(published, ch['id'])
            if old:
                videos += old
                stale.append(ch['id'])
            continue
        videos += v
    json.dump(cache, open(os.path.join(DATA, 'cache.json'), 'w'))
    gen = datetime.now(timezone.utc)
    data = dict(generated=gen.strftime('%Y-%m-%dT%H:%M:%SZ'), days=args.days,
                channels=[dict(id=c['id'], title=c['title'], group=c['group'], sos=c['sos'], platform=c['platform'],
                               url=c['url']) for c in chans],
                videos=videos, stale=stale, errors=errors)
    json.dump(data, open(os.path.join(DATA, 'last.json'), 'w'), ensure_ascii=False)
    return data


def render(data):
    os.makedirs(SITE, exist_ok=True)
    gen = datetime.strptime(data['generated'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    gen_local = gen.astimezone(ZoneInfo('Europe/Athens'))
    data['generated_label'] = gen_local.strftime('%d/%m/%Y %H:%M') + ' (ώρα Ελλάδας)'
    data['title'] = TITLE
    data['groups'] = GROUPS
    blob = json.dumps(data, ensure_ascii=False).replace('</', '<\\/')
    tpl = open(os.path.join(HERE, 'template.html'), encoding='utf8').read()
    fonts = open(os.path.join(HERE, 'fonts-embed.css'), encoding='utf8').read() if os.path.exists(os.path.join(HERE, 'fonts-embed.css')) else ''
    brand = os.path.join(HERE, 'brand')
    mark_svg = open(os.path.join(brand, 'mark.svg'), encoding='utf8').read()
    mark_svg = re.sub(r'<\?xml[^?]*\?>\s*', '', mark_svg).strip()
    favicon = 'data:image/svg+xml;charset=utf-8,' + urllib.parse.quote(mark_svg, safe='/:=+-')
    apple_png = open(os.path.join(brand, 'apple-touch-icon.png'), 'rb').read()
    apple = 'data:image/png;base64,' + base64.b64encode(apple_png).decode('ascii')
    out = (tpl.replace('__TITLE__', TITLE)
              .replace('/*__DATA__*/null', blob)
              .replace('/*__FONTS__*/', fonts)
              .replace('__FAVICON_SVG__', favicon)
              .replace('__APPLE_TOUCH__', apple))
    open(os.path.join(SITE, 'index.html'), 'w', encoding='utf8').write(out)
    json.dump(data, open(os.path.join(SITE, 'data.json'), 'w'), ensure_ascii=False)
    open(os.path.join(SITE, '.nojekyll'), 'w').close()
    icons = os.path.join(SITE, 'icons')
    os.makedirs(icons, exist_ok=True)
    shutil.copy(os.path.join(brand, 'apple-touch-icon.png'), os.path.join(SITE, 'apple-touch-icon.png'))
    shutil.copy(os.path.join(brand, 'icon-192.png'), os.path.join(icons, 'icon-192.png'))
    shutil.copy(os.path.join(brand, 'icon-512.png'), os.path.join(icons, 'icon-512.png'))
    manifest = {
        'name': TITLE,
        'short_name': 'Feed',
        'lang': 'el',
        'start_url': './',
        'scope': './',
        'display': 'standalone',
        'background_color': '#161512',
        'theme_color': '#161512',
        'icons': [
            {'src': 'icons/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': 'icons/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
        ],
    }
    open(os.path.join(SITE, 'manifest.webmanifest'), 'w', encoding='utf8').write(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    render_tv(blob, fonts)


def render_tv(blob, fonts):
    """Couch view for a webOS browser. Same embedded feed as docs/index.html."""
    tv = os.path.join(SITE, 'tv')
    os.makedirs(tv, exist_ok=True)
    tpl = open(os.path.join(HERE, 'template_tv.html'), encoding='utf8').read()
    out = (tpl.replace('__TITLE__', TITLE)
              .replace('/*__DATA__*/null', blob)
              .replace('/*__FONTS__*/', fonts))
    open(os.path.join(tv, 'index.html'), 'w', encoding='utf8').write(out)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--offline', action='store_true')
    ap.add_argument('--days', type=int, default=14)
    ap.add_argument('--per', type=int, default=6)
    a = ap.parse_args()
    os.makedirs(DATA, exist_ok=True)
    if a.offline:
        d = load_last()
        if not d or not d.get('videos'):
            sys.exit('no data/last.json or docs/data.json; run without --offline')
    else:
        d = collect(a)
    render(d)
    print(f"channels={len(d['channels'])} videos={len(d['videos'])} stale={d['stale']} errors={d['errors']}")
    print('built', os.path.join(SITE, 'index.html'))
    print('built', os.path.join(SITE, 'tv', 'index.html'))
