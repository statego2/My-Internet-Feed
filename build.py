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
def classify_yt(vid):
    """A Shorts URL must positively resolve to a reel or a normal watch URL.
    A block, consent screen, generic redirect or missing metadata is unknown."""
    req = urllib.request.Request('https://www.youtube.com/shorts/' + vid,
                                 headers={'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9',
                                          'Cookie': 'CONSENT=YES+1; SOCS=CAI'})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=8) as res:
            h = res.read().decode('utf8', 'replace')
            if re.search(r'"reelWatchEndpoint"\s*:\s*\{\s*"videoId"\s*:\s*"' + re.escape(vid) + '"', h):
                return True
            if re.search(r'<link[^>]+rel="canonical"[^>]+href="https://www.youtube.com/shorts/' + re.escape(vid) + r'["?]', h):
                return True
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            target = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, e.headers.get('Location', '')))
            if target.hostname in ('www.youtube.com', 'youtube.com') and target.path == '/watch' and urllib.parse.parse_qs(target.query).get('v') == [vid]:
                return False
    except Exception:
        pass
    return None


def channel_videos(ch):
    """Positive membership in YouTube's Videos tab (not the Shorts tab)."""
    st, h = http('https://www.youtube.com/channel/' + ch['id'] + '/videos', timeout=10, retries=0)
    if st != 200:
        return {}
    match = re.search(r'(?:var\s+ytInitialData\s*=|window\["ytInitialData"\]\s*=)\s*', h)
    if not match:
        return {}
    try:
        root, _ = json.JSONDecoder().raw_decode(h[match.end():])
        tabs = root['contents']['twoColumnBrowseResultsRenderer']['tabs']
    except (ValueError, KeyError, TypeError):
        return {}
    result = {}
    def walk(node):
        if isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            renderer = node.get('videoRenderer') or node.get('gridVideoRenderer')
            if isinstance(renderer, dict) and renderer.get('videoId'):
                vid = renderer['videoId']
                length = renderer.get('lengthText', {})
                label = length.get('simpleText') or ''.join(x.get('text', '') for x in length.get('runs', []))
                duration = None
                if re.fullmatch(r'\d+(?::\d{2}){1,2}', label):
                    duration = 0
                    for part in label.split(':'):
                        duration = duration * 60 + int(part)
                # Never interpret a reel/Shorts renderer as a normal video.
                nav = renderer.get('navigationEndpoint', {})
                if 'reelWatchEndpoint' not in nav:
                    result[vid] = duration
            for x in node.values():
                walk(x)
    for tab in tabs:
        tr = tab.get('tabRenderer', {})
        endpoint = tr.get('endpoint', {}).get('commandMetadata', {}).get('webCommandMetadata', {}).get('url', '')
        if tr.get('selected') and (endpoint.rstrip('/').endswith('/videos') or tr.get('title') == 'Videos'):
            walk(tr.get('content', {}))
    return result


def yt_details(vid, cache, regular=None):
    c = cache.get(vid)
    if c and c.get('classification_verified') and not c.get('live') and c.get('len') is not None:
        return c
    regular = regular or {}
    if vid in regular and regular[vid] is not None:
        c = dict(len=regular[vid], live=False, upcoming=False, short=False, classification_verified=True)
        cache[vid] = c
        return c
    st, h = http('https://www.youtube.com/watch?v=' + vid, timeout=8, retries=0)
    m = re.search(r'"lengthSeconds":"(\d+)"', h)
    ln = int(m.group(1)) if m else None
    live = '"isLive":true' in h
    upcoming = '"isUpcoming":true' in h
    short = False if vid in regular or live or (ln is not None and ln > 185) else classify_yt(vid)
    if short is None and c and c.get('classification_verified'):
        short = c['short']
    if short is None:
        return None
    c = dict(len=ln, live=live, upcoming=upcoming, short=short, classification_verified=True)
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
    unverified = 0
    regular = channel_videos(ch)
    for e in root.findall('a:entry', NS):
        vid = e.findtext('yt:videoId', namespaces=NS)
        title = e.findtext('a:title', namespaces=NS) or ''
        pub = datetime.fromisoformat(e.findtext('a:published', namespaces=NS))
        if pub < cutoff:
            continue
        if tfilter and tfilter not in title.lower():
            continue
        d = yt_details(vid, cache, regular)
        if d is None or not d.get('classification_verified'):
            unresolved += 1
            continue
        unknown = d['len'] is None
        if unknown:
            if minlen:
                unresolved += 1
                continue
            unverified += 1
        if d['upcoming'] or d['short']:
            continue
        if d['len'] == 0 and not d['live']:
            continue
        if not unknown and d['len'] < minlen and not d['live']:
            continue
        vids.append(dict(id='yt:' + vid, title=title, ch=ch['title'], chid=ch['id'], platform='youtube',
                         date=pub.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), dur=d['len'],
                         live=d['live'], metadata_pending=unknown, classification_verified=True, url='https://www.youtube.com/watch?v=' + vid,
                         thumb=f'https://i.ytimg.com/vi/{vid}/mqdefault.jpg'))
        if len(vids) >= per:
            break
    if unresolved:
        return vids, f"classification pending ({unresolved} withheld, {unverified} without duration)"
    return vids, (f"metadata pending ({unverified} unverified, {unresolved} filtered)" if unverified or unresolved else None)


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
        h = ''
        try:
            for path in ('/videos', ''):
                try:
                    pg.goto(f"https://rumble.com/c/{ch['id']}{path}", wait_until='domcontentloaded', timeout=15000)
                    pg.wait_for_selector('rum-card-video', timeout=8000)
                    h = pg.content()
                    if 'video-title=' in h:
                        break
                except Exception:
                    continue
        finally:
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


# Resolve Rumble's official embed URL on the build machine, not on the TV.
def valid_rumble_embed(url):
    if not isinstance(url, str):
        return ''
    url = html.unescape(url).strip()
    p = urllib.parse.urlsplit(url)
    if p.scheme == 'https' and p.netloc == 'rumble.com' and not p.fragment and re.fullmatch(r'/embed/[A-Za-z0-9._-]+/?', p.path):
        return url
    return ''


def extract_rumble_embed(markup):
    # Prefer the video page's canonical embedUrl metadata; don't guess IDs.
    markup = html.unescape(markup or '').replace(r'\/', '/')
    for pattern in (
        r'["\x27]embedUrl["\x27]\s*:\s*["\x27]([^"\x27]+)',
        r'<iframe\b[^>]*\bsrc\s*=\s*["\x27](https://rumble\.com/embed/[^"\x27]+)',
    ):
        for match in re.finditer(pattern, markup, re.I):
            url = valid_rumble_embed(match.group(1))
            if url:
                return url
    return ''


def enrich_rumble_embeds(videos, published):
    previous = {v.get('id'): valid_rumble_embed(v.get('embed_url')) for v in published.get('videos', []) if v.get('platform') == 'rumble'}
    pending = []
    for v in videos:
        if v.get('platform') != 'rumble':
            continue
        url = valid_rumble_embed(v.get('embed_url')) or previous.get(v.get('id'))
        if url:
            v['embed_url'] = url
        else:
            pending.append(v)
    if not pending:
        return
    # The endpoint returns official iframe markup. Keep failures isolated.
    def resolve(v):
        endpoint = 'https://rumble.com/api/Media/oembed.json?' + urllib.parse.urlencode({'url': v['url']})
        _, body = http(endpoint, timeout=8, retries=0)
        try:
            result = json.loads(body)
            url = extract_rumble_embed(result.get('html', ''))
            if url:
                v['embed_url'] = url
        except Exception:
            pass
    with ThreadPoolExecutor(3) as ex:
        list(ex.map(resolve, pending))
    pending = [v for v in pending if not v.get('embed_url')]
    if not pending:
        return
    # Rumble may block HTTP requests. Try the same browser used by the feed.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            try:
                page = browser.new_page(user_agent=UA)
                for v in pending:
                    try:
                        page.goto(v['url'], wait_until='domcontentloaded', timeout=15000)
                        url = extract_rumble_embed(page.content())
                        if url:
                            v['embed_url'] = url
                    except Exception:
                        pass
            finally:
                browser.close()
    except Exception as e:
        print('Rumble embed lookup unavailable:', e)
    print('Rumble embeds resolved:', sum(bool(v.get('embed_url')) for v in videos if v.get('platform') == 'rumble'))


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
    videos, errors, stale, warnings = [], [], [], []
    # Preserve metadata of previously verified videos, even on a fresh CI runner.
    for item in published.get('videos', []):
        if item.get('platform') == 'youtube' and not item.get('live') and (item.get('classification_verified') or (item.get('dur') or 0) > 185):
            cache.setdefault(item['id'][3:], dict(len=item.get('dur'), live=False, upcoming=False, short=False, classification_verified=True))

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
            stale.append(ch['id'])
            if old:
                cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
                videos += [x for x in old if (x.get('platform') != 'youtube' or x.get('classification_verified') or (x.get('dur') or 0) > 185 or x.get('live')) and datetime.fromisoformat(x['date'].replace('Z', '+00:00')) >= cutoff]
            continue
        if err:
            warnings.append(f"{ch['title']}: {err}")
        videos += v
    enrich_rumble_embeds(videos, published)
    json.dump(cache, open(os.path.join(DATA, 'cache.json'), 'w'))
    gen = datetime.now(timezone.utc)
    data = dict(generated=gen.strftime('%Y-%m-%dT%H:%M:%SZ'), days=args.days,
                channels=[dict(id=c['id'], title=c['title'], group=c['group'], sos=c['sos'], platform=c['platform'],
                               url=c['url']) for c in chans],
                videos=videos, stale=stale, errors=errors, warnings=warnings)
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
    """Couch view for a webOS browser, with only the fields used by TV."""
    data = json.loads(blob)
    tv_data = {
        'groups': data.get('groups', []),
        'generated_label': data.get('generated_label', ''),
        'generated': data.get('generated', ''),
        'channels': [{k: c[k] for k in ('id', 'group', 'sos', 'title') if k in c}
                     for c in data.get('channels', [])],
        'videos': [],
    }
    for v in data.get('videos', []):
        item = {k: v[k] for k in ('id', 'title', 'ch', 'chid', 'platform', 'date', 'dur', 'live', 'url', 'thumb', 'embed_url') if k in v}
        if v.get('platform') == 'youtube' and re.fullmatch(r'yt:[A-Za-z0-9_-]{11}', v.get('id', '')):
            item.pop('url', None)
            if v.get('thumb') == 'https://i.ytimg.com/vi/' + v['id'][3:] + '/mqdefault.jpg':
                item.pop('thumb', None)
        tv_data['videos'].append(item)
    blob = json.dumps(tv_data, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    tv = os.path.join(SITE, 'tv')
    os.makedirs(tv, exist_ok=True)
    tpl = open(os.path.join(HERE, 'template_tv.html'), encoding='utf8').read()
    mark = open(os.path.join(HERE, 'brand', 'mark.svg'), encoding='utf8').read()
    mark = re.sub(r'<\?xml[^?]*\?>\s*', '', mark).strip()
    mark = mark.replace('<svg ', '<svg class="mark" ', 1)
    out = (tpl.replace('__TITLE__', TITLE)
              .replace('__MARK__', mark)
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
