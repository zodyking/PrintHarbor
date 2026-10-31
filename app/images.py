"""Model artwork for saved printers.

A make and model is downloaded once into DATA_DIR and reused. Lookup runs on a
background thread. Pages only read the local catalog.
"""
import hashlib
import ipaddress
import json
import os
import re
import shutil
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from PIL import Image, UnidentifiedImageError

from . import store

if Image.MAX_IMAGE_PIXELS is None:
    Image.MAX_IMAGE_PIXELS = 40_000_000

STOP = threading.Event()
KICK = threading.Event()
UA = 'PrintHarbor/1.0 (printer image cache)'
FINGERPRINT_URL = (
    'https://raw.githubusercontent.com/CANTI-BOT/UniFi-Icon-Browser/main/'
    'chrome-extension/data/fingerprint-database.json'
)
FINGERPRINT_PATH = (
    '/CANTI-BOT/UniFi-Icon-Browser/main/chrome-extension/data/fingerprint-database.json'
)
HOSTS = {'static.ui.com', 'commons.wikimedia.org', 'upload.wikimedia.org', 'thumb.wikimedia.org'}
CATEGORIES = ('mono', 'color', 'mfp', 'inkjet', 'label', 'receipt', 'large', 'generic')
STATIC = Path(__file__).resolve().parent / 'static' / 'printers'
QUEUE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,40}$')
ITEM_RE = re.compile(r'^[A-Za-z0-9_-]{1,40}$')
LABELS = {
    'unifi': 'UniFi',
    'wikimedia': 'Wikimedia Commons',
    'printer': 'This printer',
    'upload': 'Your image',
    'svg': 'Illustration',
    'miss': 'Illustration',
}
MAKERS = (
    ('hewlett packard', 'hp'),
    ('konica minolta', 'konica'),
    ('brother', 'brother'),
    ('canon', 'canon'),
    ('epson', 'epson'),
    ('xerox', 'xerox'),
    ('samsung', 'samsung'),
    ('lexmark', 'lexmark'),
    ('kyocera', 'kyocera'),
    ('ricoh', 'ricoh'),
    ('konica', 'konica'),
    ('sharp', 'sharp'),
    ('toshiba', 'toshiba'),
    ('panasonic', 'panasonic'),
    ('oki', 'oki'),
    ('okidata', 'oki'),
    ('dell', 'dell'),
    ('pantum', 'pantum'),
    ('dymo', 'dymo'),
    ('zebra', 'zebra'),
    ('fuji', 'fuji'),
    ('hp', 'hp'),
)
MAKER_KEYS = {key for _, key in MAKERS}
CODE_STOP = {
    'pro', 'plus', 'series', 'printer', 'printers', 'color', 'colour', 'laser', 'laserjet',
    'ink', 'inkjet', 'mono', 'wireless', 'network', 'all', 'one', 'eco', 'ecotank', 'pixma',
    'officejet', 'deskjet', 'workforce', 'expression', 'surecolor', 'designjet', 'imageclass',
    'workcentre', 'versalink', 'phaser', 'smart', 'tank', 'office',
}
_index = {'mtime': None, 'entries': []}


class ImageError(Exception):
    pass


def network_enabled():
    return os.environ.get('PRINTHARBOR_IMAGE_NETWORK', '1') != '0'


def _now():
    return datetime.now(timezone.utc)


def _stamp(moment=None):
    return (moment or _now()).strftime('%Y-%m-%dT%H:%M:%SZ')


def _parsed(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _phrase(phrase):
    body = r'[- ]'.join(re.escape(part) for part in phrase.split(' '))
    return rf'(?<![a-z0-9]){body}(?![a-z0-9])'


def category_for(text):
    low = (text or '').lower()
    if any(word in low for word in ('receipt', 'thermal')):
        return 'receipt'
    if any(word in low for word in ('labelwriter', 'dymo', 'zebra', 'ql-', 'label printer')):
        return 'label'
    if any(word in low for word in ('plotter', 'designjet', 'surecolor', 'large format')):
        return 'large'
    if any(word in low for word in ('mfc', 'mfp', 'multifunction', 'all-in-one', 'all in one')):
        return 'mfp'
    if any(word in low for word in ('inkjet', 'pixma', 'ecotank', 'officejet', 'deskjet', 'stylus', 'workforce')):
        return 'inkjet'
    if 'color' in low or 'colour' in low:
        return 'color'
    if any(word in low for word in ('laser', 'laserjet', 'hl-')):
        return 'mono'
    return 'generic'


def identify(text):
    raw = (text or '').lower()
    raw = re.sub(r'\b(series|industries|corporation|corp|inc|ltd|company)\b', ' ', raw)
    maker = ''
    for phrase, key in MAKERS:
        pattern = _phrase(phrase)
        if re.search(pattern, raw.replace('-', ' ')):
            maker = key
            raw = re.sub(pattern, ' ', raw, count=1)
            break
    parts = re.findall(r'[a-z0-9]+(?:-[a-z0-9]+)*', raw)
    found_codes = []
    for index, token in enumerate(parts):
        compact = re.sub(r'[^a-z0-9]', '', token)
        prev = re.sub(r'[^a-z0-9]', '', parts[index - 1]) if index else ''
        glued = ''
        if prev.isalpha() and 1 <= len(prev) <= 4 and prev not in CODE_STOP and prev not in MAKER_KEYS:
            if re.search(r'[a-z]', compact) and re.search(r'\d', compact) and len(compact) >= 3:
                glued = prev + compact
            elif re.fullmatch(r'\d{3,6}', compact):
                glued = prev + compact
        if glued:
            shown = token if '-' in token else f'{parts[index - 1]}-{token}'
            found_codes.append((glued, shown))
        elif re.search(r'[a-z]', compact) and re.search(r'\d', compact) and len(compact) >= 4:
            found_codes.append((compact, token))
        elif re.fullmatch(r'\d{3,6}', compact):
            found_codes.append((compact, token))
    if found_codes:
        code, display = max(found_codes, key=lambda item: len(item[0]))
    else:
        code, display = '', ''
    key = f'{maker}|{code}' if maker and code else code
    return {
        'manufacturer': maker,
        'model': code,
        'code': code,
        'key': key,
        'display': display or code,
        'category': category_for(text or ''),
    }


def _entry(item):
    if item.get('code'):
        return item
    found = identify(item.get('name') or '')
    return {**item, **found}


def _makers_ok(left, right):
    if not left or not right:
        return True
    return left == right


def _near(left, right):
    if not left or not right or left == right:
        return False
    if left.startswith(right) or right.startswith(left):
        extra = left[len(right):] if left.startswith(right) else right[len(left):]
        return extra.isalpha() and 1 <= len(extra) <= 3
    return False


def match_entries(entries, ident):
    code = ident.get('code') or ''
    maker = ident.get('manufacturer') or ''
    if not code:
        return {'match': None, 'confidence': 0, 'candidates': []}
    exact = []
    near = []
    for raw in entries:
        item = _entry(raw)
        other = item.get('code') or ''
        other_maker = item.get('manufacturer') or ''
        if not other or not _makers_ok(maker, other_maker):
            continue
        if other == code:
            exact.append(item)
        elif _near(code, other):
            near.append(item)
    if len(exact) == 1:
        same = bool(maker and exact[0].get('manufacturer') == maker)
        return {'match': exact[0], 'confidence': 1.0 if same else 0.9, 'candidates': near[:6]}
    if len(exact) > 1:
        same = [item for item in exact if maker and item.get('manufacturer') == maker]
        if len(same) == 1:
            rest = [item for item in exact if item is not same[0]]
            return {'match': same[0], 'confidence': 1.0, 'candidates': rest[:6]}
        return {'match': None, 'confidence': 0, 'candidates': exact[:6]}
    return {'match': None, 'confidence': 0, 'candidates': near[:6]}


def _edits(left, right):
    if left == right:
        return 0
    if not left or not right:
        return max(len(left), len(right))
    prev = list(range(len(right) + 1))
    for i, char in enumerate(left, 1):
        current = [i]
        for j, other in enumerate(right, 1):
            current.append(min(current[-1] + 1, prev[j] + 1, prev[j - 1] + (char != other)))
        prev = current
    return prev[-1]


def _number_gap(left, right):
    left_nums = [int(item) for item in re.findall(r'\d+', left or '')]
    right_nums = [int(item) for item in re.findall(r'\d+', right or '')]
    if not left_nums or len(left_nums) != len(right_nums):
        return 10**6
    return sum(abs(a - b) for a, b in zip(left_nums, right_nums))


def nearest_entry(entries, ident):
    """Closest same-maker catalog row when the exact model is absent."""
    code = ident.get('code') or ''
    maker = ident.get('manufacturer') or ''
    if not code or not maker or len(code) < 4:
        return None
    best = None
    best_key = None
    for raw in entries:
        item = _entry(raw)
        other = item.get('code') or ''
        if not other or other == code or item.get('manufacturer') != maker or len(other) < 4:
            continue
        distance = _edits(code, other)
        if distance < 1 or distance > 2:
            continue
        prefix = 0
        for left_char, right_char in zip(code, other):
            if left_char != right_char:
                break
            prefix += 1
        if prefix < max(2, (min(len(code), len(other)) + 1) // 2):
            continue
        gap = _number_gap(code, other)
        if gap > 40:
            continue
        key = (distance, gap, other)
        if best_key is None or key < best_key:
            best_key = key
            best = item
    return best


def license_ok(name):
    low = (name or '').lower().replace('_', ' ').replace('-', ' ')
    if not low.strip():
        return False
    if re.search(r'\bnc\b|non ?commercial|\bnd\b|no deriv', low):
        return False
    return any(token in low for token in ('public domain', 'cc0', 'cc by', 'gfdl'))


def wiki_acceptable(title, license_name, code, manufacturer=''):
    compact = re.sub(r'[^a-z0-9]', '', (title or '').lower())
    if not code or code not in compact:
        return False
    maker = re.sub(r'[^a-z0-9]', '', (manufacturer or '').lower())
    if maker and maker not in compact:
        return False
    low = (title or '').lower()
    if any(word in low for word in ('logo', 'screenshot', 'icon', 'diagram', 'manual', 'packaging', 'box')):
        return False
    return license_ok(license_name)


def _plain(value):
    text = re.sub(r'<[^>]+>', ' ', str(value or ''))
    return ' '.join(text.split())[:180]


def wiki_pages(payload):
    pages = ((payload or {}).get('query') or {}).get('pages') or {}
    found = []
    if isinstance(pages, dict):
        values = pages.values()
    else:
        values = pages
    for page in values:
        if not isinstance(page, dict):
            continue
        info = (page.get('imageinfo') or [{}])[0] or {}
        meta = info.get('extmetadata') or {}

        def val(key):
            item = meta.get(key) or {}
            return item.get('value') if isinstance(item, dict) else str(item or '')

        found.append({
            'title': page.get('title') or '',
            'license': _plain(val('LicenseShortName')),
            'artist': _plain(val('Artist')),
            'url': info.get('thumburl') or info.get('url') or '',
            'page': info.get('descriptionurl') or '',
            'mime': info.get('mime') or '',
        })
    return found


def _remote_ok(url):
    parsed = urlsplit(url or '')
    if parsed.scheme != 'https' or not parsed.hostname:
        return False
    host = parsed.hostname
    if host == 'raw.githubusercontent.com':
        return parsed.path == FINGERPRINT_PATH
    if host == 'thumb.wikimedia.org':
        return parsed.path.startswith('/wikipedia/commons/')
    return host in HOSTS


def wiki_hits(pages, code, manufacturer=''):
    hits = []
    for page in pages:
        if not wiki_acceptable(page.get('title') or '', page.get('license') or '', code, manufacturer):
            continue
        if _remote_ok(page.get('url') or ''):
            hits.append(page)
    return hits


def pick_wiki(pages, code, manufacturer=''):
    hits = wiki_hits(pages, code, manufacturer)
    return hits[0] if hits else None


def fetch(url, timeout=8, limit=2_000_000):
    if not network_enabled():
        raise ImageError('Printer image downloads are turned off')
    if not _remote_ok(url):
        raise ImageError('Unsupported image address')
    request = Request(url, headers={'User-Agent': UA, 'Accept': 'image/*,application/json'})
    try:
        with urlopen(request, timeout=timeout) as response:
            if not _remote_ok(response.geturl()):
                raise ImageError('The image address changed host')
            body = response.read(limit + 1)
            ctype = (response.headers.get('Content-Type') or '').split(';', 1)[0].strip().lower()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ImageError('The image provider did not respond') from exc
    if len(body) > limit or not body:
        raise ImageError('The image provider returned an unexpected file')
    return body, ctype


def sanitize_svg(data):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return None
    if not text or len(text) > 200_000 or '<svg' not in text.lower():
        return None
    low = text.lower()
    if re.search(r'<\s*(script|foreignobject|iframe|embed|object|image|use|animate)\b', low):
        return None
    if re.search(r'\bon[a-z]+\s*=', low) or 'javascript:' in low:
        return None
    if re.search(r'(?:href|xlink:href)\s*=\s*["\']?\s*(?:https?:|//|data:)', low):
        return None
    return text.encode('utf-8')


def raster_ok(data):
    if not data or data[:1] in (b'<', b'{') or b'<html' in data[:400].lower() or b'<!doctype' in data[:400].lower():
        return False
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in ('PNG', 'JPEG', 'WEBP'):
                return False
            image.verify()
        return True
    except (UnidentifiedImageError, OSError, ValueError):
        return False


def image_ok(data, content_type=''):
    ctype = (content_type or '').lower()
    if 'html' in ctype or 'json' in ctype or 'text/' in ctype:
        return False
    if 'svg' in ctype:
        return sanitize_svg(data) is not None
    return raster_ok(data)


def fingerprint_file():
    return store.ROOT / 'fingerprint-database.json'


def catalog_stale():
    path = fingerprint_file()
    if not path.is_file():
        return True
    try:
        days = int(store.settings().get('images_catalog_days') or 14)
    except ValueError:
        days = 14
    age = _now().timestamp() - path.stat().st_mtime
    return age > max(days, 1) * 86400


def refresh_fingerprint(force=False):
    path = fingerprint_file()
    if path.is_file() and not force and not catalog_stale():
        return True
    try:
        body, _ctype = fetch(FINGERPRINT_URL, timeout=20, limit=8_000_000)
        data = json.loads(body)
        if not isinstance(data, (dict, list)) or not data:
            raise ImageError('The printer catalog was empty')
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.json.tmp')
        temp.write_bytes(body)
        temp.replace(path)
        _index['mtime'] = None
        return True
    except (ImageError, OSError, ValueError, json.JSONDecodeError):
        return path.is_file()


def load_index():
    path = fingerprint_file()
    if not path.is_file():
        return []
    mtime = path.stat().st_mtime
    if _index['mtime'] == mtime:
        return _index['entries']
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(raw, dict):
        pairs = raw.items()
    elif isinstance(raw, list):
        pairs = ((item.get('id'), item.get('name')) for item in raw if isinstance(item, dict))
    else:
        pairs = ()
    entries = []
    for fid, name in pairs:
        if fid is None or not name:
            continue
        found = identify(str(name))
        if not found['code']:
            continue
        entries.append({'id': str(fid), 'name': str(name), **found})
    _index['mtime'] = mtime
    _index['entries'] = entries
    return entries


def _images():
    folder = store.ROOT / 'images'
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _inside(path):
    try:
        root = _images().resolve()
        target = Path(path).resolve()
    except OSError:
        return False
    return target.is_file() and (target == root or root in target.parents)


def _row(sql, args=()):
    with store.connection() as db:
        found = db.execute(sql, args).fetchone()
    return dict(found) if found else None


def _catalog_by_key(key):
    if not key:
        return None
    return _row('SELECT * FROM image_catalog WHERE model_key=?', (key,))


def find_catalog(ident):
    code = ident.get('code') or ''
    if not code:
        return None
    direct = _catalog_by_key(ident.get('key') or '')
    if direct:
        return direct
    with store.connection() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM image_catalog WHERE model=?', (code,))]
    maker = ident.get('manufacturer') or ''
    usable = [row for row in rows if not maker or not row.get('manufacturer') or row.get('manufacturer') == maker]
    if not usable:
        return None
    usable.sort(key=lambda row: (row.get('manufacturer') == maker, bool(row.get('local_path'))), reverse=True)
    return usable[0]


def _save_catalog(fields):
    columns = (
        'model_key', 'manufacturer', 'model', 'source', 'fingerprint_id', 'local_path',
        'confidence', 'license', 'attribution', 'source_page', 'checked_at', 'negative_until',
        'category', 'candidates',
    )
    values = [fields.get(name, '' if name != 'confidence' else 0) for name in columns]
    assignments = ', '.join(f'{name}=excluded.{name}' for name in columns if name != 'model_key')
    with store.connection() as db:
        db.execute(
            f'INSERT INTO image_catalog({", ".join(columns)}) VALUES ({", ".join("?" * len(columns))}) '
            f'ON CONFLICT(model_key) DO UPDATE SET {assignments}',
            values,
        )
    return _catalog_by_key(fields['model_key'])


def _fresh(row):
    if not row:
        return False
    source = row.get('source') or ''
    if source in ('unifi', 'wikimedia', 'printer', 'upload'):
        return bool(row.get('local_path')) and _inside(row['local_path'])
    if source == 'svg':
        return True
    if source == 'miss':
        until = _parsed(row.get('negative_until') or '')
        return bool(until and until > _now())
    return False


def _candidate_dir():
    folder = _images() / 'candidates'
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _remember_candidate(kind, item_id, meta, data=None):
    if kind not in ('unifi', 'wiki') or not ITEM_RE.fullmatch(item_id or ''):
        return
    folder = _candidate_dir()
    (folder / f'{kind}-{item_id}.json').write_text(json.dumps(meta), encoding='utf-8')
    if data and raster_ok(data):
        (folder / f'{kind}-{item_id}.png').write_bytes(data)


def _public_candidates(items):
    cleaned = []
    for item in items[:6]:
        kind = item.get('kind') or ''
        item_id = str(item.get('id') or '')
        if kind not in ('unifi', 'wiki') or not ITEM_RE.fullmatch(item_id):
            continue
        cleaned.append({
            'kind': kind,
            'id': item_id,
            'title': (item.get('title') or 'Printer')[:120],
            'license': item.get('license') or '',
            'attribution': item.get('attribution') or '',
            'page': item.get('page') or '',
        })
    return cleaned


def _cache_file(key):
    digest = hashlib.sha256(key.encode()).hexdigest()[:24]
    return _images() / f'{digest}.png'


def _download_unifi(ident, match, candidates, confidence):
    item_id = str(match.get('id') or '')
    if not re.fullmatch(r'\d{1,12}', item_id):
        return None
    url = f'https://static.ui.com/fingerprint/0/{item_id}_257x257.png'
    try:
        body, ctype = fetch(url)
    except (ImageError, OSError, TimeoutError):
        return None
    if not image_ok(body, ctype):
        return None
    path = _cache_file(ident['key'])
    path.write_bytes(body)
    credit = 'Ubiquiti' if confidence >= 0.9 else (match.get('name') or 'Ubiquiti')
    listed = [{
        'kind': 'unifi', 'id': item_id, 'title': match.get('name') or ident['display'],
        'license': '', 'attribution': credit, 'page': '',
    }]
    _remember_candidate(listed[0]['kind'], item_id, {**listed[0], 'url': url}, body)
    for item in candidates:
        other = str(item.get('id') or '')
        if not re.fullmatch(r'\d{1,12}', other):
            continue
        other_url = f'https://static.ui.com/fingerprint/0/{other}_257x257.png'
        meta = {
            'kind': 'unifi', 'id': other, 'title': item.get('name') or other,
            'license': '', 'attribution': 'Ubiquiti', 'page': '', 'url': other_url,
        }
        listed.append(meta)
        _remember_candidate('unifi', other, meta)
    return _save_catalog({
        'model_key': ident['key'],
        'manufacturer': ident['manufacturer'],
        'model': ident['code'],
        'source': 'unifi',
        'fingerprint_id': item_id,
        'local_path': str(path),
        'confidence': confidence,
        'license': '',
        'attribution': credit,
        'source_page': '',
        'checked_at': _stamp(),
        'negative_until': '',
        'category': ident['category'],
        'candidates': json.dumps(_public_candidates(listed)),
    })


def _wiki_search(ident):
    shown = ident.get('display') or ident.get('code') or ''
    query = ' '.join(part for part in (f'intitle:{shown}' if shown else '', ident.get('manufacturer') or '', 'printer') if part)
    url = 'https://commons.wikimedia.org/w/api.php?' + urlencode({
        'action': 'query',
        'format': 'json',
        'generator': 'search',
        'gsrsearch': query,
        'gsrnamespace': '6',
        'gsrlimit': '6',
        'prop': 'imageinfo',
        'iiprop': 'url|mime|extmetadata',
        'iiurlwidth': '480',
    })
    try:
        body, _ctype = fetch(url, limit=1_000_000)
        payload = json.loads(body)
    except (ImageError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
        return None
    for page in wiki_hits(wiki_pages(payload), ident['code'], ident.get('manufacturer') or ''):
        try:
            data, ctype = fetch(page['url'])
        except (ImageError, OSError, TimeoutError):
            continue
        if image_ok(data, ctype or page.get('mime') or ''):
            return page, data
    return None


def _download_wiki(ident):
    found = _wiki_search(ident)
    if not found:
        return None
    page, data = found
    path = _cache_file(ident['key'])
    path.write_bytes(data)
    item_id = hashlib.sha256((page.get('title') or '').encode()).hexdigest()[:16]
    source_page = page.get('page') or ''
    if urlsplit(source_page).hostname != 'commons.wikimedia.org':
        title = (page.get('title') or '').replace(' ', '_')
        source_page = 'https://commons.wikimedia.org/wiki/' + quote(title, safe='/:_')
    meta = {
        'kind': 'wiki', 'id': item_id, 'title': page.get('title') or ident['display'],
        'license': page.get('license') or '', 'attribution': page.get('artist') or 'Wikimedia Commons',
        'page': source_page, 'url': page.get('url') or '',
    }
    _remember_candidate('wiki', item_id, meta, data)
    return _save_catalog({
        'model_key': ident['key'],
        'manufacturer': ident['manufacturer'],
        'model': ident['code'],
        'source': 'wikimedia',
        'fingerprint_id': '',
        'local_path': str(path),
        'confidence': 0.9,
        'license': meta['license'],
        'attribution': meta['attribution'],
        'source_page': source_page,
        'checked_at': _stamp(),
        'negative_until': '',
        'category': ident['category'],
        'candidates': json.dumps(_public_candidates([meta])),
    })


def _remember_fallback(ident, source, candidates, keep_existing=True):
    current = find_catalog(ident)
    if keep_existing and current and current.get('local_path') and _inside(current['local_path']):
        return current
    until = ''
    if source == 'miss':
        until = _stamp(_now() + timedelta(days=7))
    model_key = ident.get('key') or (current.get('model_key') if current else '') or ident.get('code') or ''
    return _save_catalog({
        'model_key': model_key,
        'manufacturer': ident['manufacturer'],
        'model': ident['code'],
        'source': source,
        'fingerprint_id': '',
        'local_path': '',
        'confidence': 0,
        'license': '',
        'attribution': '',
        'source_page': '',
        'checked_at': _stamp(),
        'negative_until': until,
        'category': ident['category'],
        'candidates': json.dumps(_public_candidates(candidates)),
    })


def resolve_model(ident, force=False):
    if not ident.get('code') or not ident.get('key'):
        return None
    current = find_catalog(ident)
    if current and _fresh(current) and not force:
        return current
    candidates = []
    saved = None
    try:
        if catalog_stale() or force:
            refresh_fingerprint(force=False)
        result = match_entries(load_index(), ident)
        candidates = [{
            'kind': 'unifi', 'id': str(item.get('id') or ''), 'title': item.get('name') or '',
            'license': '', 'attribution': 'Ubiquiti', 'page': '',
        } for item in result['candidates']]
        if result['match']:
            saved = _download_unifi(ident, result['match'], result['candidates'], result['confidence'])
        if not saved:
            saved = _download_wiki(ident)
        if not saved:
            near = nearest_entry(load_index(), ident)
            if near:
                saved = _download_unifi(ident, near, [], 0.6)
    except (ImageError, OSError, ValueError):
        saved = None
    if saved:
        return saved
    if current and current.get('local_path') and _inside(current['local_path']):
        return current
    return _remember_fallback(ident, 'miss', candidates, keep_existing=False)


def _printer(queue):
    if not QUEUE_RE.fullmatch(queue or ''):
        return None
    return _row('SELECT * FROM printers WHERE queue=?', (queue,))


def _driver_name(driver):
    return re.sub(r',\s*using\s+.*$', '', driver or '', flags=re.I).strip()


def identity_of(printer):
    identity = printer.get('identity') or ''
    driver = _driver_name(printer.get('driver') or '')
    label = printer.get('label') or ''
    blob = ' '.join(part for part in (identity, driver, label) if part)
    for text in (identity, driver):
        found = identify(text)
        if found['code']:
            found['category'] = category_for(blob or text)
            return found
    for text in (label, printer.get('queue') or ''):
        found = identify(text)
        if found['code']:
            found['category'] = category_for(blob or text)
            return found
    return {
        'manufacturer': '', 'model': '', 'code': '', 'key': '', 'display': '',
        'category': category_for(blob),
    }


def attach(queue):
    try:
        printer = _printer(queue)
        if not printer or (printer.get('image_mode') or 'auto') == 'manual':
            return
        ident = identity_of(printer)
        with store.connection() as db:
            db.execute(
                "UPDATE printers SET image_key=? WHERE queue=? AND COALESCE(image_mode,'auto')!='manual'",
                (ident.get('key') or '', queue),
            )
        KICK.set()
    except Exception:
        store.event('warning', 'Printer image could not be queued')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _icon_host(host):
    try:
        address = ipaddress.ip_address(str(host or '').strip())
    except ValueError:
        return ''
    if not isinstance(address, ipaddress.IPv4Address):
        return ''
    return str(address)


def _read_device_icon(host, url):
    parsed = urlsplit(url or '')
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
        return None
    if (parsed.hostname or '') != host or parsed.port not in (None, 80, 443, 631):
        return None
    opener = urllib.request.build_opener(_NoRedirect)
    request = Request(url, headers={'User-Agent': UA, 'Accept': 'image/png,image/jpeg'})
    try:
        with opener.open(request, timeout=4) as response:
            final = urlsplit(response.geturl())
            if final.scheme not in ('http', 'https') or (final.hostname or '') != host:
                return None
            body = response.read(2_000_001)
            ctype = (response.headers.get('Content-Type') or '').split(';', 1)[0].strip().lower()
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return None
    if not body or len(body) > 2_000_000 or not image_ok(body, ctype):
        return None
    return body


def _download_device_icon(printer, ident):
    host = _icon_host(printer.get('host') or '')
    if not host or not ident.get('key'):
        return None
    try:
        from . import printing
        attrs = printing.ipp_attributes(host)
    except Exception:
        return None
    urls = [item for item in (attrs.get('printer-icons') or []) if isinstance(item, str)]
    urls.sort(key=lambda item: 0 if item.lower().split('?', 1)[0].endswith('.png') else 1)
    for url in urls[:4]:
        body = _read_device_icon(host, url)
        if not body:
            continue
        path = _cache_file(ident['key'])
        path.write_bytes(body)
        return _save_catalog({
            'model_key': ident['key'],
            'manufacturer': ident['manufacturer'],
            'model': ident['code'],
            'source': 'printer',
            'fingerprint_id': '',
            'local_path': str(path),
            'confidence': 1,
            'license': '',
            'attribution': 'The printer',
            'source_page': '',
            'checked_at': _stamp(),
            'negative_until': '',
            'category': ident['category'],
            'candidates': '[]',
        })
    return None


def _hold_printer(row):
    return _save_catalog({
        'model_key': row['model_key'],
        'manufacturer': row.get('manufacturer') or '',
        'model': row.get('model') or '',
        'source': 'printer',
        'fingerprint_id': row.get('fingerprint_id') or '',
        'local_path': row.get('local_path') or '',
        'confidence': row.get('confidence') or 1,
        'license': row.get('license') or '',
        'attribution': row.get('attribution') or 'The printer',
        'source_page': row.get('source_page') or '',
        'checked_at': _stamp(),
        'negative_until': _stamp(_now() + timedelta(days=7)),
        'category': row.get('category') or 'generic',
        'candidates': row.get('candidates') or '[]',
    })


def _printer_held(row, now=None):
    if not row or row.get('source') != 'printer' or not _fresh(row):
        return False
    until = _parsed(row.get('negative_until') or '')
    return bool(until and until > (now or _now()))


def resolve_printer(queue):
    printer = _printer(queue)
    if not printer or (printer.get('image_mode') or 'auto') == 'manual':
        return None
    ident = identity_of(printer)
    if not ident.get('key'):
        return None
    _ICON_TRIED[queue] = _now()
    current = find_catalog(ident)
    if current and current.get('source') in ('unifi', 'wikimedia', 'upload') and _fresh(current):
        row = current
    elif _printer_held(current):
        row = current
    else:
        try:
            row = resolve_model(ident, force=True)
        except Exception:
            store.event('warning', 'Printer image lookup could not finish')
            row = None
        if row and row.get('source') in ('unifi', 'wikimedia', 'upload') and _fresh(row):
            pass
        elif row and row.get('source') == 'printer' and _fresh(row):
            row = _hold_printer(row)
        else:
            icon = _download_device_icon(printer, ident)
            if icon:
                row = _hold_printer(icon)
            elif not row or row.get('source') != 'miss':
                row = _remember_fallback(ident, 'miss', [])
    with store.connection() as db:
        db.execute(
            "UPDATE printers SET image_key=? WHERE queue=? AND COALESCE(image_mode,'auto')!='manual'",
            (ident['key'], queue),
        )
    return row


_ICON_TRIED = {}


def pending_queues():
    with store.connection() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM printers WHERE COALESCE(image_mode,'auto')!='manual' ORDER BY queue"
        )]
    due = []
    now = _now()
    for printer in rows:
        ident = identity_of(printer)
        if not ident.get('key'):
            continue
        row = find_catalog(ident)
        if row and row.get('source') in ('unifi', 'wikimedia', 'upload') and _fresh(row):
            continue
        if _printer_held(row, now):
            continue
        tried = _ICON_TRIED.get(printer['queue'])
        if tried and (now - tried).total_seconds() < 1800:
            continue
        due.append(printer['queue'])
    return due


def _clear_misses():
    with store.connection() as db:
        db.execute("DELETE FROM image_catalog WHERE source='miss'")


def run_loop():
    try:
        _clear_misses()
    except Exception:
        store.event('warning', 'Printer image lookup could not finish')
    while not STOP.is_set():
        try:
            while not STOP.is_set() and network_enabled():
                queues = pending_queues()
                if not queues:
                    break
                resolve_printer(queues[0])
        except Exception:
            store.event('warning', 'Printer image lookup could not finish')
        if STOP.is_set():
            break
        KICK.wait(2)
        KICK.clear()


def _json_list(value):
    try:
        data = json.loads(value or '[]')
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _page_ok(url):
    host = urlsplit(url or '').hostname
    return host in ('commons.wikimedia.org', 'static.ui.com')


def describe(printer):
    ident = identity_of(printer)
    mode = printer.get('image_mode') or 'auto'
    row = find_catalog(ident) if ident.get('code') else None
    candidates = _json_list((row or {}).get('candidates'))
    if mode == 'manual':
        source = printer.get('image_source') or 'upload'
        page = printer.get('image_page') or ''
        return {
            'source': source,
            'source_label': LABELS.get(source, 'Your image'),
            'confidence': 1,
            'confident': True,
            'pending': False,
            'attribution': printer.get('image_attribution') or '',
            'license': printer.get('image_license') or '',
            'page': page if _page_ok(page) else '',
            'category': printer.get('image_fingerprint') if source == 'svg' else ident['category'],
            'candidates': candidates,
        }
    if not row:
        pending = bool(ident.get('key'))
        return {
            'source': 'svg', 'source_label': 'Illustration', 'confidence': 0, 'confident': False,
            'pending': pending, 'attribution': '', 'license': '', 'page': '',
            'category': ident['category'], 'candidates': [],
        }
    source = row.get('source') or 'svg'
    pending = source == 'miss' and not _fresh(row)
    page = row.get('source_page') or ''
    confidence = float(row.get('confidence') or 0)
    return {
        'source': source,
        'source_label': LABELS.get(source, 'Illustration'),
        'confidence': confidence,
        'confident': confidence >= 0.9 and source in ('unifi', 'wikimedia', 'printer', 'upload'),
        'pending': pending,
        'attribution': row.get('attribution') or '',
        'license': row.get('license') or '',
        'page': page if _page_ok(page) else '',
        'category': row.get('category') or ident['category'],
        'candidates': candidates,
    }


def static_svg(category):
    name = category if category in CATEGORIES else 'generic'
    return STATIC / f'{name}.svg', 'image/svg+xml'


def media_type(path):
    return {
        '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.webp': 'image/webp', '.svg': 'image/svg+xml',
    }.get(Path(path).suffix.lower(), 'application/octet-stream')


def email_bytes(queue):
    """A small PNG of the saved printer photo, for the result email."""
    path, media = file_for(queue)
    if media == 'image/svg+xml' or not path or not Path(path).is_file():
        return None
    try:
        with Image.open(path) as src:
            frame = src.convert('RGB')
            frame.thumbnail((480, 320))
            buf = BytesIO()
            frame.save(buf, 'PNG')
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    data = buf.getvalue()
    if not data or len(data) > 400_000:
        return None
    return data, 'png'


def file_for(queue):
    printer = _printer(queue)
    if not printer:
        return static_svg('generic')
    if (printer.get('image_mode') or 'auto') == 'manual':
        if printer.get('image_source') == 'svg':
            return static_svg(printer.get('image_fingerprint') or 'generic')
        path = Path(printer.get('image_path') or '')
        if _inside(path):
            return path, media_type(path)
    ident = identity_of(printer)
    row = find_catalog(ident) if ident.get('code') else None
    if row and row.get('local_path') and _inside(row['local_path']):
        path = Path(row['local_path'])
        return path, media_type(path)
    category = (row or {}).get('category') or ident['category']
    return static_svg(category)


def ensure_candidate(kind, item_id):
    if kind not in ('unifi', 'wiki') or not ITEM_RE.fullmatch(item_id or ''):
        return None
    folder = _candidate_dir()
    image = folder / f'{kind}-{item_id}.png'
    if _inside(image):
        return image
    meta_path = folder / f'{kind}-{item_id}.json'
    if not meta_path.is_file():
        if kind == 'unifi' and re.fullmatch(r'\d{1,12}', item_id):
            meta = {
                'kind': 'unifi', 'id': item_id, 'title': item_id, 'license': '',
                'attribution': 'Ubiquiti', 'page': '',
                'url': f'https://static.ui.com/fingerprint/0/{item_id}_257x257.png',
            }
        else:
            return None
    else:
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return None
    url = meta.get('url') or ''
    if not _remote_ok(url):
        return None
    try:
        body, ctype = fetch(url)
    except (ImageError, OSError, TimeoutError):
        return None
    if not raster_ok(body) or not image_ok(body, ctype):
        return None
    image.write_bytes(body)
    return image


def _set_manual(queue, source, path='', license_name='', attribution='', page='', fingerprint=''):
    with store.connection() as db:
        db.execute(
            '''UPDATE printers SET image_mode='manual', image_source=?, image_path=?, image_license=?,
               image_attribution=?, image_page=?, image_fingerprint=? WHERE queue=?''',
            (source, path, license_name, attribution, page, fingerprint, queue),
        )


def _clear_manual_files(queue):
    for path in _images().glob(f'manual-{queue}.*'):
        if _inside(path):
            path.unlink()


def apply_choice(queue, choice):
    printer = _printer(queue)
    if not printer:
        raise ImageError('Printer not found')
    kind, _, item_id = (choice or '').partition(':')
    path = ensure_candidate(kind, item_id)
    if not path:
        raise ImageError('That image is not available')
    meta_path = _candidate_dir() / f'{kind}-{item_id}.json'
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8')) if meta_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        meta = {}
    _clear_manual_files(queue)
    dest = _images() / f'manual-{queue}{path.suffix.lower()}'
    dest.write_bytes(path.read_bytes())
    source = 'unifi' if kind == 'unifi' else 'wikimedia'
    _set_manual(
        queue, source, str(dest), meta.get('license') or '', meta.get('attribution') or '',
        meta.get('page') or '', item_id if kind == 'unifi' else '',
    )


def apply_illustration(queue, category):
    printer = _printer(queue)
    if not printer:
        raise ImageError('Printer not found')
    if category not in CATEGORIES:
        raise ImageError('Choose an illustration')
    _clear_manual_files(queue)
    _set_manual(queue, 'svg', '', '', '', '', category)


def save_upload(queue, filename, data):
    printer = _printer(queue)
    if not printer:
        raise ImageError('Printer not found')
    if not data or len(data) > 2_000_000:
        raise ImageError('Use a PNG, JPEG, WebP, or SVG under 2 MB')
    ext = Path(filename or '').suffix.lower()
    if ext == '.svg':
        cleaned = sanitize_svg(data)
        if not cleaned:
            raise ImageError('This SVG is not allowed')
        payload, suffix = cleaned, '.svg'
    elif ext in ('.png', '.jpg', '.jpeg', '.webp'):
        if not raster_ok(data):
            raise ImageError('This file is not a valid image')
        payload, suffix = data, '.jpg' if ext == '.jpeg' else ext
    else:
        raise ImageError('Use a PNG, JPEG, WebP, or SVG')
    folder = _images() / 'preview'
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f'{queue}{suffix}'
    for old in folder.glob(f'{queue}.*'):
        if _inside(old):
            old.unlink()
    dest.write_bytes(payload)
    return dest


def preview_file(queue):
    if not QUEUE_RE.fullmatch(queue or ''):
        return None
    folder = _images() / 'preview'
    if not folder.is_dir():
        return None
    found = [path for path in folder.glob(f'{queue}.*') if _inside(path)]
    return found[0] if found else None


def apply_preview(queue):
    path = preview_file(queue)
    if not path:
        raise ImageError('Preview an image first')
    _clear_manual_files(queue)
    dest = _images() / f'manual-{queue}{path.suffix.lower()}'
    shutil.copyfile(path, dest)
    _set_manual(queue, 'upload', str(dest), '', 'Uploaded image', '', '')
    return dest


def restore_auto(queue):
    printer = _printer(queue)
    if not printer:
        raise ImageError('Printer not found')
    _clear_manual_files(queue)
    with store.connection() as db:
        db.execute(
            '''UPDATE printers SET image_mode='auto', image_source='', image_path='', image_license='',
               image_attribution='', image_page='', image_fingerprint='' WHERE queue=?''',
            (queue,),
        )
    ident = identity_of(printer)
    if ident.get('key') and network_enabled():
        resolve_model(ident, force=False)
    KICK.set()


def search_again(queue):
    printer = _printer(queue)
    if not printer:
        raise ImageError('Printer not found')
    ident = identity_of(printer)
    if not ident.get('key'):
        raise ImageError('This printer has no model to look up')
    row = resolve_model(ident, force=True)
    if (printer.get('image_mode') or 'auto') != 'manual':
        with store.connection() as db:
            db.execute('UPDATE printers SET image_key=? WHERE queue=?', (ident['key'], queue))
    return row


def _shared_files():
    protected = set()
    with store.connection() as db:
        for row in db.execute("SELECT image_path FROM printers WHERE image_mode='manual'"):
            if row['image_path']:
                protected.add(str(Path(row['image_path']).resolve()))
    return protected


def clear_cache():
    protected = _shared_files()
    with store.connection() as db:
        db.execute('DELETE FROM image_catalog')
        db.execute(
            "UPDATE printers SET image_key='' WHERE COALESCE(image_mode,'auto')!='manual'"
        )
    folder = _images()
    files = [path for path in folder.rglob('*') if path.is_file()]
    for path in files:
        if path.name.startswith('manual-') or str(path.resolve()) in protected:
            continue
        path.unlink()
    KICK.set()


def rescan_all():
    clear_cache()
    for queue in pending_queues():
        if network_enabled():
            resolve_printer(queue)
            break
    KICK.set()


def update_settings(auto, unifi, wiki, cache, days):
    try:
        days_i = int(days)
    except (TypeError, ValueError):
        days_i = 0
    if days_i < 1 or days_i > 90:
        raise ImageError('Catalog refresh age must be between 1 and 90 days')
    store.set_setting('images_auto', '1' if auto else '0')
    store.set_setting('images_unifi', '1' if unifi else '0')
    store.set_setting('images_wikimedia', '1' if wiki else '0')
    store.set_setting('images_cache', '1' if cache else '0')
    store.set_setting('images_catalog_days', str(days_i))
    with store.connection() as db:
        db.execute("DELETE FROM image_catalog WHERE source IN ('miss', 'svg') OR local_path=''")
    KICK.set()
