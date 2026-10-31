import io
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas
from . import store

Image.MAX_IMAGE_PIXELS = 40_000_000

ALLOWED = {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff', '.txt', '.docx', '.xlsx', '.odt', '.ods'}
SIDES = {'one-sided', 'two-sided-long-edge', 'two-sided-short-edge'}
QUEUE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,40}$')
MODEL_RE = re.compile(r'^[A-Za-z0-9_.:/-]{1,180}$')
OPTION_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,40}$')
OPTION_VALUE_RE = re.compile(r'^[A-Za-z0-9_+-]{1,20}$')

class PrintError(Exception):
    pass

STOP = threading.Event()
_QUEUE_LOCKS = {}
_QUEUE_LOCKS_GUARD = threading.Lock()
_NOTICE_AT = {}
LOW_SUPPLY = 10

def cmd(*args, timeout=25):
    env = os.environ.copy()
    env['LC_ALL'] = 'C'
    env['LANG'] = 'C'
    try:
        return subprocess.run(args, check=False, text=True, capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError as exc:
        raise PrintError(f'{args[0]} is not installed or not on PATH') from exc
    except subprocess.TimeoutExpired as exc:
        raise PrintError(f'{args[0]} timed out') from exc
    except OSError as exc:
        raise PrintError(str(exc)) from exc

def interpret_lpstat(result):
    lines = [ln for ln in (result.stdout or '').splitlines() if ln.strip()][:40]
    err = (result.stderr or '').strip()
    if result.returncode == 0:
        return {'ok': True, 'lines': lines, 'error': ''}
    low = err.lower()
    if 'no destinations added' in low or 'no active jobs' in low or 'no entries' in low:
        return {'ok': True, 'lines': [], 'error': ''}
    return {'ok': False, 'lines': [], 'error': (err or (result.stdout or '').strip() or 'lpstat failed')[:200]}

def printer_reachable(host, protocol, timeout=2):
    """A real connect to the printer port. Short enough to run before each document."""
    port = 631 if protocol == 'ipp' else 9100
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

def _ipv4(ip):
    try:
        address = str(ipaddress.ip_address(str(ip or '').strip()))
    except ValueError:
        raise PrintError('Enter a numeric printer IP address')
    if not isinstance(ipaddress.ip_address(address), ipaddress.IPv4Address):
        raise PrintError('IPv4 printer IP required')
    return address

def _norm(text):
    cleaned = re.sub(r'[^a-z0-9]+', ' ', (text or '').lower().replace('series', ' '))
    return ' '.join(cleaned.split())

def device_fields(device_id):
    fields = {}
    for part in str(device_id or '').split(';'):
        if ':' not in part:
            continue
        key, _, value = part.partition(':')
        key = key.strip().upper()
        value = value.strip()
        if key and value:
            fields[key] = value
    return fields

def parse_lpinfo(text):
    drivers = []
    current = None
    for line in (text or '').splitlines():
        stripped = line.strip()
        if stripped == '{':
            current = {}
            continue
        if stripped == '}' and current is not None:
            if current.get('name'):
                drivers.append(current)
            current = None
            continue
        if current is not None and '=' in stripped:
            key, _, value = stripped.partition('=')
            current[key.strip()] = value.strip()
            continue
        if current is None and stripped and not stripped.startswith('#'):
            name, _, desc = stripped.partition(' ')
            if MODEL_RE.fullmatch(name):
                drivers.append({'name': name, 'make-and-model': desc, 'device-id': ''})
    return drivers

def parse_ipp(payload):
    raw = payload or b''
    sep = raw.find(b'\r\n\r\n')
    if raw.startswith(b'HTTP/') and sep >= 0:
        raw = raw[sep + 4:]
    if len(raw) < 8:
        return {}
    index = 8
    found = {}
    current = ''
    while index < len(raw):
        tag = raw[index]
        index += 1
        if tag == 0x03:
            break
        if tag < 0x10:
            continue
        if index + 4 > len(raw):
            break
        name_len = int.from_bytes(raw[index:index + 2], 'big')
        index += 2
        name = raw[index:index + name_len].decode('utf-8', 'replace')
        index += name_len
        if index + 2 > len(raw):
            break
        value_len = int.from_bytes(raw[index:index + 2], 'big')
        index += 2
        value = raw[index:index + value_len]
        index += value_len
        if name:
            current = name
        if not current:
            continue
        if tag in (0x36, 0x37) and len(value) >= 4:
            lang_len = int.from_bytes(value[:2], 'big')
            text_at = 2 + lang_len
            if text_at + 2 <= len(value):
                text_len = int.from_bytes(value[text_at:text_at + 2], 'big')
                value = value[text_at + 2:text_at + 2 + text_len]
        if tag in (0x21, 0x23) and len(value) == 4:
            found.setdefault(current, []).append(int.from_bytes(value, 'big', signed=True))
        elif tag == 0x22 and len(value) == 1:
            found.setdefault(current, []).append(value[0] == 1)
        elif tag in (0x36, 0x37, 0x41, 0x42, 0x44, 0x45, 0x47, 0x48, 0x49):
            found.setdefault(current, []).append(value.decode('utf-8', 'replace'))
    return found

def _ipp_attr(tag, name, value):
    encoded = value.encode('utf-8')
    return bytes([tag]) + len(name).to_bytes(2, 'big') + name.encode('ascii') + len(encoded).to_bytes(2, 'big') + encoded

IPP_FIELDS = (
    'printer-make-and-model', 'printer-device-id', 'printer-state', 'printer-state-reasons',
    'printer-is-accepting-jobs', 'marker-names', 'marker-levels', 'marker-high-levels',
    'printer-firmware-string', 'printer-firmware-name', 'printer-firmware-version',
    'printer-icons',
)

def _ipp_request(host, detailed=False):
    uri = f'ipp://{host}/ipp/print'
    body = b'\x02\x00' + (0x000B).to_bytes(2, 'big') + (1).to_bytes(4, 'big') + b'\x01'
    body += _ipp_attr(0x47, 'attributes-charset', 'utf-8')
    body += _ipp_attr(0x48, 'attributes-natural-language', 'en')
    body += _ipp_attr(0x45, 'printer-uri', uri)
    if detailed and IPP_FIELDS:
        body += _ipp_attr(0x44, 'requested-attributes', IPP_FIELDS[0])
        for name in IPP_FIELDS[1:]:
            body += _ipp_attr(0x44, '', name)
    body += b'\x03'
    return body

def _read_socket(sock, limit=200000):
    chunks = []
    total = 0
    while total < limit:
        piece = sock.recv(8192)
        if not piece:
            break
        chunks.append(piece)
        total += len(piece)
    return b''.join(chunks)

def ipp_attributes(host):
    """Read the printer's own description. A detailed ask falls back to the plain one."""
    for detailed in (True, False):
        body = _ipp_request(host, detailed=detailed)
        http = (
            f'POST /ipp/print HTTP/1.1\r\nHost: {host}:631\r\n'
            f'Content-Type: application/ipp\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'
        ).encode('ascii') + body
        gave_up = False
        for payload in (http, body):
            if gave_up:
                break
            try:
                with socket.create_connection((host, 631), timeout=2) as sock:
                    sock.settimeout(2)
                    sock.sendall(payload)
                    parsed = parse_ipp(_read_socket(sock))
            except TimeoutError:
                gave_up = True
                continue
            except OSError:
                continue
            if parsed.get('printer-make-and-model') or parsed.get('printer-device-id') or parsed.get('printer-state'):
                return parsed
    return {}

def ipp_identity(host):
    return ipp_attributes(host)

def pjl_identity(host):
    query = b'\x1b%-12345X@PJL INFO ID\r\n\x1b%-12345X'
    try:
        with socket.create_connection((host, 9100), timeout=3) as sock:
            sock.settimeout(2)
            sock.sendall(query)
            data = b''
            while len(data) < 4000:
                piece = sock.recv(1024)
                if not piece:
                    break
                data += piece
    except OSError:
        return ''
    text = data.decode('latin-1', 'replace')
    match = re.search(r'INFO ID\s*\r?\n"([^"]+)"', text, re.I)
    if match:
        return match.group(1).strip()[:120]
    match = re.search(r'"([^"]{3,80})"', text)
    return match.group(1).strip()[:120] if match else ''

def snmp_identity(host):
    community = os.getenv('SNMP_COMMUNITY', 'public')
    if not community or not shutil.which('snmpget'):
        return ''
    for oid in ('1.3.6.1.2.1.25.3.2.1.3.1', '1.3.6.1.2.1.1.1.0'):
        result = cmd('snmpget', '-v2c', '-c', community, '-t', '1', '-r', '0', '-Oqv', host, oid, timeout=4)
        if result.returncode:
            continue
        text = (result.stdout or '').strip().strip('"')
        if text and 'nosuch' not in text.lower() and 'no such' not in text.lower():
            return text[:120]
    return ''

def discover_printer(host):
    ipp_open = printer_reachable(host, 'ipp')
    raw_open = printer_reachable(host, 'socket')
    found = {
        'reachable': bool(ipp_open or raw_open),
        'ipp': bool(ipp_open),
        'socket': bool(raw_open),
        'make': '',
        'model': '',
        'device_id': '',
        'identity': '',
    }
    if ipp_open:
        attrs = ipp_identity(host)
        device_id = (attrs.get('printer-device-id') or [''])[0]
        make_model = (attrs.get('printer-make-and-model') or [''])[0].strip()
        fields = device_fields(device_id)
        found['device_id'] = device_id[:300]
        found['make'] = fields.get('MFG', '')[:80]
        found['model'] = fields.get('MDL', '')[:80]
        found['identity'] = make_model[:120]
    if raw_open and not found['model']:
        pjl = pjl_identity(host)
        if pjl and not found['identity']:
            found['identity'] = pjl
            found['model'] = pjl
    if not found['identity']:
        found['identity'] = snmp_identity(host)
    if found['identity'] and not found['model']:
        found['model'] = found['identity']
    if not found['identity']:
        found['identity'] = ' '.join(part for part in (found['make'], found['model']) if part)[:120]
    return found

def choose_driver(discovery, drivers):
    """Match a discovered printer to an installed driver, or use the printer's own description."""
    model = _norm(discovery.get('model'))
    make = _norm(discovery.get('make'))
    identity = _norm(discovery.get('identity'))
    best = None
    best_score = 0
    for driver in drivers or []:
        name = driver.get('name') or ''
        if not MODEL_RE.fullmatch(name) or name == 'everywhere' or name.endswith('generic.ppd'):
            continue
        fields = device_fields(driver.get('device-id'))
        described = _norm(fields.get('MDL') or driver.get('make-and-model') or '')
        maker = _norm(fields.get('MFG') or '')
        score = 0
        if model and described and (model == described or model in described or described in model):
            score = 100
        elif identity and described and (identity in described or described in identity):
            score = 85
        else:
            tokens = [tok for tok in (model or identity).split() if any(ch.isdigit() for ch in tok) or len(tok) >= 5]
            if tokens and described:
                overlap = sum(1 for tok in tokens if tok in described.split())
                if overlap:
                    score = 60 + (10 * overlap)
        if score and make and maker and make == maker:
            score += 5
        if score > best_score:
            best = driver
            best_score = score
    identity_label = (discovery.get('identity') or 'Printer').strip()[:120]
    if best and best_score >= 80:
        if 'brlaser' in best['name']:
            if not discovery.get('socket'):
                if discovery.get('ipp'):
                    return {'model': 'everywhere', 'protocol': 'ipp', 'label': identity_label, 'driver': 'IPP Everywhere'}
                raise PrintError('This printer needs port 9100, and that port did not answer')
            return {'model': best['name'], 'protocol': 'socket', 'label': identity_label, 'driver': driver_title(best)}
        if discovery.get('socket'):
            return {'model': best['name'], 'protocol': 'socket', 'label': identity_label, 'driver': driver_title(best)}
    if discovery.get('ipp'):
        return {'model': 'everywhere', 'protocol': 'ipp', 'label': identity_label, 'driver': 'IPP Everywhere'}
    raise PrintError('The printer answered, but no driver matched it')

def load_drivers():
    info = cmd('lpinfo', '-m', '-l', timeout=25)
    if info.returncode and not (info.stdout or '').strip():
        info = cmd('lpinfo', '-m', timeout=25)
    if info.returncode and not (info.stdout or '').strip():
        raise PrintError((info.stderr or info.stdout or 'Could not list printer drivers')[:400])
    drivers = parse_lpinfo(info.stdout)
    if not drivers:
        raise PrintError('No printer drivers are installed')
    return drivers

def density_options(lpoptions_text):
    """Toner save off, and the darkest density step, when the PPD lists them."""
    chosen = []
    for line in (lpoptions_text or '').splitlines():
        if ':' not in line:
            continue
        left, _, right = line.partition(':')
        key = left.split('/', 1)[0].strip()
        if not OPTION_RE.fullmatch(key):
            continue
        label = left.lower()
        values = [item.lstrip('*') for item in right.split() if item.lstrip('*')]
        if 'economode' in key.lower() or 'toner save' in label:
            if 'False' in values:
                chosen.append((key, 'False'))
            continue
        if 'density' not in key.lower() and 'density' not in label:
            continue
        numbered = []
        for value in values:
            if not OPTION_VALUE_RE.fullmatch(value):
                continue
            try:
                numbered.append((int(value), value))
            except ValueError:
                continue
        if numbered:
            chosen.append((key, max(numbered)[1]))
    return chosen

def driver_title(driver):
    """The driver's own name, such as the PPD make and model."""
    title = (driver.get('make-and-model') or driver.get('name') or 'Driver').strip()
    return title[:120]

def parse_queue_options(text):
    """Choices advertised by a queue. The starred value is the one in use."""
    options = []
    for line in (text or '').splitlines():
        if ':' not in line:
            continue
        left, _, right = line.partition(':')
        key = left.split('/', 1)[0].strip()
        label = left.split('/', 1)[1].strip() if '/' in left else key
        if not OPTION_RE.fullmatch(key):
            continue
        current = ''
        values = []
        for token in right.split():
            chosen = token.startswith('*')
            value = token[1:] if chosen else token
            if not value or not OPTION_VALUE_RE.fullmatch(value) or value in values:
                continue
            values.append(value)
            if chosen:
                current = value
        if len(values) < 2:
            continue
        options.append({
            'key': key,
            'label': (label or key)[:60],
            'values': values[:12],
            'current': current or values[0],
        })
    def rank(option):
        low = f"{option['key']} {option['label']}".lower()
        if any(word in low for word in ('economode', 'toner save', 'density')):
            return 0
        if any(word in low for word in ('resolution', 'duplex', 'sides', 'pagesize', 'media')):
            return 1
        return 2
    options.sort(key=rank)
    return options[:16]

def queue_options(queue):
    if not QUEUE_RE.fullmatch(queue or ''):
        return []
    try:
        listed = cmd('lpoptions', '-p', queue, '-l', timeout=15)
    except PrintError:
        return []
    if listed.returncode:
        return []
    return parse_queue_options(listed.stdout)

def _set_options(queue, pairs):
    for key, value in pairs:
        if not OPTION_RE.fullmatch(key) or not OPTION_VALUE_RE.fullmatch(str(value)):
            continue
        result = cmd('lpadmin', '-p', queue, '-o', f'{key}={value}', timeout=15)
        if result.returncode:
            raise PrintError((result.stderr or result.stdout or f'{key} could not be saved')[:400])

def apply_queue_options(queue, choices):
    """Save the values the user picked. Unknown keys are ignored."""
    if not QUEUE_RE.fullmatch(queue or ''):
        raise PrintError('Invalid queue')
    known = {item['key']: item for item in queue_options(queue)}
    pairs = []
    for key, value in (choices or {}).items():
        option = known.get(key)
        if not option or str(value) not in option['values'] or str(value) == option['current']:
            continue
        pairs.append((key, str(value)))
    _set_options(queue, pairs)
    if pairs:
        store.event('success', f'Printer {queue} settings saved')
    return [key for key, _ in pairs]

def configure_queue(queue, address, discovery, drivers, keep_options=False):
    choice = choose_driver(discovery, drivers)
    if not MODEL_RE.fullmatch(choice['model']):
        raise PrintError('The selected driver name was not usable')
    saved = [(item['key'], item['current']) for item in queue_options(queue)] if keep_options else []
    uri = f'socket://{address}:9100' if choice['protocol'] == 'socket' else f'ipp://{address}/ipp/print'
    result = cmd('lpadmin', '-p', queue, '-E', '-v', uri, '-m', choice['model'], timeout=40)
    if result.returncode:
        raise PrintError((result.stderr or result.stdout or 'The printer could not be added')[:400])
    if saved:
        _set_options(queue, saved)
    else:
        listed = cmd('lpoptions', '-p', queue, '-l', timeout=15)
        if listed.returncode == 0:
            _set_options(queue, density_options(listed.stdout))
    cmd('cupsaccept', queue)
    cmd('cupsenable', queue)
    return choice

def _note_image(queue):
    try:
        from . import images
        images.attach(queue)
    except Exception:
        store.event('warning', 'Printer image could not be queued')

def _save_printer(queue, label, address, choice):
    with store.connection() as db:
        db.execute(
            'INSERT INTO printers(queue,label,host,protocol,identity,driver) VALUES (?,?,?,?,?,?) '
            'ON CONFLICT(queue) DO UPDATE SET label=excluded.label, host=excluded.host, '
            'protocol=excluded.protocol, identity=excluded.identity, driver=excluded.driver',
            (queue, label[:100], address, choice['protocol'], choice['label'][:120], choice['driver'][:120]),
        )

def add_printer(label, queue, ip, protocol='socket'):
    # Older forms posted a connection type. The printer is read and a driver is chosen instead.
    _ = protocol
    if not QUEUE_RE.fullmatch(queue):
        raise PrintError('Queue must begin with a letter and contain only letters, numbers, _ or -')
    address = _ipv4(ip)
    discovery = discover_printer(address)
    if not discovery.get('reachable'):
        raise PrintError('The printer did not answer. Check the address and that this host can reach it.')
    choice = configure_queue(queue, address, discovery, load_drivers())
    _save_printer(queue, label, address, choice)
    _note_image(queue)
    store.event('success', f'Printer {queue} configured on {address} ({choice["label"][:80]})')
    return choice['label']

def update_printer(queue, label, ip, reread=False):
    """Change a saved printer. A new address, or an explicit reread, selects the driver again."""
    if not QUEUE_RE.fullmatch(queue):
        raise PrintError('Invalid queue')
    address = _ipv4(ip)
    name = ' '.join(str(label or '').split())[:100]
    if not name:
        raise PrintError('Enter a display name')
    with store.connection() as db:
        row = db.execute('SELECT label, host, identity FROM printers WHERE queue=?', (queue,)).fetchone()
    if not row:
        raise PrintError('Printer not found')
    if address == row['host'] and not reread:
        with store.connection() as db:
            db.execute('UPDATE printers SET label=? WHERE queue=?', (name, queue))
        _note_image(queue)
        store.event('success', f'Printer {queue} renamed to {name}')
        return (row['identity'] or name)[:120]
    discovery = discover_printer(address)
    if not discovery.get('reachable'):
        raise PrintError('The printer did not answer. The saved printer was left unchanged.')
    choice = configure_queue(queue, address, discovery, load_drivers(), keep_options=(address == row['host']))
    _save_printer(queue, name, address, choice)
    _note_image(queue)
    store.event('success', f'Printer {queue} updated on {address} ({choice["label"][:80]})')
    return choice['label']

def retarget_saved_queues():
    """Re-read each saved printer and select its driver again."""
    with store.connection() as db:
        rows = db.execute('SELECT queue, label, host FROM printers').fetchall()
    if not rows:
        return
    try:
        drivers = load_drivers()
    except Exception as exc:
        store.event('warning', f'Printer drivers could not be listed: {exc}'[:240])
        return
    for row in rows:
        queue = row['queue'] or ''
        if not QUEUE_RE.fullmatch(queue):
            continue
        try:
            address = _ipv4(row['host'])
            discovery = discover_printer(address)
            if not discovery.get('reachable'):
                store.event('warning', f'Printer {queue} did not answer, so its driver was left as-is')
                continue
            choice = configure_queue(queue, address, discovery, drivers, keep_options=True)
        except Exception as exc:
            store.event('warning', f'Printer {queue} was not updated: {exc}'[:240])
            continue
        _save_printer(queue, row['label'] or queue, address, choice)
        _note_image(queue)

def remove_printer(queue):
    if not QUEUE_RE.fullmatch(queue):
        raise PrintError('Invalid queue')
    result = cmd('lpadmin', '-x', queue)
    if result.returncode:
        raise PrintError(result.stderr or 'Could not delete queue')
    with store.connection() as db:
        db.execute('DELETE FROM printers WHERE queue=?', (queue,))

def active_jobs():
    try:
        return interpret_lpstat(cmd('lpstat', '-o', timeout=10))
    except PrintError as exc:
        return {'ok': False, 'lines': [], 'error': str(exc)[:200]}

def cups_printers():
    try:
        return interpret_lpstat(cmd('lpstat', '-p', timeout=10))
    except PrintError as exc:
        return {'ok': False, 'lines': [], 'error': str(exc)[:200]}

def _render_txt(txt, dest):
    c = Canvas(str(dest), pagesize=letter)
    x, y = 48, 750
    for line in txt.splitlines()[:10000]:
        for chunk in [line[i:i + 88] for i in range(0, max(len(line), 1), 88)]:
            c.drawString(x, y, chunk.encode('latin-1', 'replace').decode('latin-1'))
            y -= 15
            if y < 45:
                c.showPage()
                y = 750
    c.save()

def _images_to_pdf(data, dest, max_pages):
    try:
        with Image.open(io.BytesIO(data)) as src:
            count = getattr(src, 'n_frames', 1)
            if count > max_pages:
                raise PrintError('Too many image pages')
            frames = []
            for index in range(count):
                src.seek(index)
                frames.append(src.convert('RGB').copy())
    except PrintError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise PrintError('Unable to read that image') from exc
    if not frames:
        raise PrintError('Unable to read that image')
    pdf = Canvas(str(dest), pagesize=letter)
    for index, frame in enumerate(frames):
        if index:
            pdf.showPage()
        width, height = frame.size
        scale = min(540 / max(width, 1), 720 / max(height, 1))
        draw_w, draw_h = width * scale, height * scale
        pdf.drawImage(ImageReader(frame), (612 - draw_w) / 2, (792 - draw_h) / 2, width=draw_w, height=draw_h)
    pdf.save()

def _safe_filename(filename):
    name = Path(str(filename or 'document')).name.strip()
    if name in ('', '.', '..'):
        name = 'document'
    if len(name) > 180:
        suffix = Path(name).suffix[-16:]
        name = name[:180 - len(suffix)] + suffix
    return name

def prepare_bytes(filename, data, max_pages):
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED:
        raise PrintError(f'Unsupported file type: {suffix or "unknown"}')
    if not data:
        raise PrintError('Empty attachment')
    folder = store.ROOT / 'files'
    folder.mkdir(exist_ok=True, parents=True)
    root = uuid.uuid4().hex
    original = folder / f'{root}{suffix}'
    original.write_bytes(data)
    dest = folder / f'{root}.pdf'
    try:
        if suffix == '.pdf':
            if not data.startswith(b'%PDF-'):
                raise PrintError('Invalid PDF file')
            dest = original
        elif suffix in ('.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff'):
            _images_to_pdf(data, dest, max_pages)
        elif suffix == '.txt':
            _render_txt(data.decode('utf-8', errors='replace'), dest)
        else:
            tmp_dir = store.ROOT / 'tmp'
            tmp_dir.mkdir(parents=True, exist_ok=True)
            profile = Path(tempfile.mkdtemp(prefix='lo-', dir=tmp_dir))
            try:
                out = cmd(
                    'libreoffice', '-env:UserInstallation=' + profile.resolve().as_uri(),
                    '--headless', '--convert-to', 'pdf', '--outdir', str(folder), str(original), timeout=55,
                )
                if out.returncode or not dest.exists():
                    raise PrintError('Office conversion failed: ' + (out.stderr or out.stdout)[:200])
            finally:
                shutil.rmtree(profile, ignore_errors=True)
        try:
            pages = len(PdfReader(str(dest), strict=False).pages)
        except Exception as ex:
            raise PrintError('Unable to inspect converted PDF') from ex
        if pages < 1 or pages > max_pages:
            raise PrintError(f'Document has {pages} pages; limit is {max_pages}')
        if original != dest:
            original.unlink(missing_ok=True)
        return str(dest), pages
    except Exception:
        original.unlink(missing_ok=True)
        if dest != original:
            dest.unlink(missing_ok=True)
        raise

def _supply_kind(name):
    low = (name or '').lower()
    if any(word in low for word in ('drum', 'opc', 'imaging', 'photoconductor')):
        return 'drum'
    if 'waste' in low:
        return ''
    if 'ink' in low:
        return 'ink'
    if any(word in low for word in ('toner', 'cartridge')):
        return 'toner'
    return ''


def supply_problem(stats):
    """A sentence when ink, toner, or drum is at 10% or below, paper is empty, or the printer is jammed."""
    if not stats:
        return None
    problems = []
    saw_mark = False
    for item in stats.get('supplies') or []:
        kind = _supply_kind(item.get('name') or '')
        percent = item.get('percent')
        if not kind or not isinstance(percent, int):
            continue
        if percent > LOW_SUPPLY:
            continue
        saw_mark = True
        label = ' '.join(str(item.get('name') or kind).split())
        problems.append(f'{label} is at {percent}%')
    reasons = [str(item) for item in (stats.get('reasons') or [])]
    if not saw_mark and 'Toner empty' in reasons:
        problems.append('The toner is empty')
    if any(item in reasons for item in ('Out of paper', 'Paper needed')):
        problems.append('The printer is out of paper')
    if 'Paper jam' in reasons:
        problems.append('The printer has a paper jam')
    if not problems:
        return None
    state = condition(stats, online=True)
    reason = '. '.join(problems)
    if not reason.endswith('.'):
        reason += '.'
    reason += ' The document was not printed.'
    return {
        'reason': reason,
        'problems': problems,
        'notice': state['paper'] != 'empty' and state['jam'] != 'jam',
    }


def supply_hold(queue):
    """Read the printer. None when it did not report a supply fault."""
    if not QUEUE_RE.fullmatch(queue or ''):
        return None
    with store.connection() as db:
        printer = db.execute(
            'SELECT host, protocol, label, identity FROM printers WHERE queue=?',
            (queue,),
        ).fetchone()
    if not printer or not printer_reachable(printer['host'], printer['protocol'], timeout=1):
        return None
    try:
        stats = printer_stats(printer['host'])
    except Exception:
        return None
    problem = supply_problem(stats)
    if not problem:
        return None
    problem['label'] = (printer['label'] or queue)[:100]
    problem['identity'] = printer['identity'] or printer['label'] or ''
    return problem


def _wrap_line(text, font, size, width):
    lines = []
    current = ''
    for word in str(text or '').split():
        trial = word if not current else f'{current} {word}'
        if stringWidth(trial, font, size) <= width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or ['']


def _notice_pdf(dest, hold):
    canvas = Canvas(str(dest), pagesize=letter)
    width, height = letter
    canvas.setFillColor(HexColor('#17191f'))
    canvas.rect(0, height - 96, width, 96, fill=1, stroke=0)
    canvas.setFillColor(white)
    canvas.setFont('Helvetica-Bold', 13)
    canvas.drawString(48, height - 42, 'PRINT HARBOR')
    canvas.setFont('Helvetica', 12)
    canvas.drawString(48, height - 66, 'This document was not printed')
    canvas.setFillColor(HexColor('#17191f'))
    y = height - 146
    canvas.setFont('Helvetica-Bold', 22)
    for line in _wrap_line(hold.get('reason') or 'The printer needs attention.', 'Helvetica-Bold', 22, width - 96)[:5]:
        canvas.drawString(48, y, line)
        y -= 30
    y -= 10
    canvas.setFont('Helvetica', 13)
    canvas.setFillColor(HexColor('#5c6570'))
    canvas.drawString(48, y, 'PRINTER')
    y -= 22
    canvas.setFillColor(HexColor('#17191f'))
    canvas.setFont('Helvetica-Bold', 16)
    canvas.drawString(48, y, (hold.get('label') or 'Printer')[:80])
    y -= 42
    canvas.setFont('Helvetica-Bold', 13)
    canvas.drawString(48, y, 'WHAT TO DO')
    y -= 28
    canvas.setFont('Helvetica', 13)
    steps = [
        'Fix the printer. Replace anything at or below 10%, add paper, or clear a jam.',
        'Send the document again.',
    ]
    try:
        from . import supplies
        cartridge = supplies.replacement_for(hold.get('identity') or '')
    except Exception:
        cartridge = None
    if cartridge and cartridge.get('name'):
        steps.insert(1, f"The cartridge for this model is {cartridge['name']}.")
    for index, step in enumerate(steps, start=1):
        canvas.setFont('Helvetica-Bold', 13)
        canvas.drawString(48, y, f'{index:02d}')
        canvas.setFont('Helvetica', 13)
        wrapped = _wrap_line(step, 'Helvetica', 13, width - 120)
        for line_index, line in enumerate(wrapped[:3]):
            canvas.drawString(88, y - (line_index * 18), line)
        y -= 18 * max(len(wrapped[:3]), 1) + 14
    canvas.setFillColor(HexColor('#5c6570'))
    canvas.setFont('Helvetica', 10)
    canvas.drawString(48, 48, 'Print Harbor')
    canvas.save()


def print_supply_notice(queue, hold):
    """Print one notice while the same fault continues, and only when a page can still come out."""
    if not hold or not hold.get('notice') or not QUEUE_RE.fullmatch(queue or ''):
        return False
    signature = hold.get('reason') or ''
    now = time.time()
    previous = _NOTICE_AT.get(queue)
    if previous and previous[0] == signature and now - previous[1] < 6 * 3600:
        return False
    folder = store.ROOT / 'tmp'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'notice-{uuid.uuid4().hex}.pdf'
    try:
        _notice_pdf(path, hold)
        result = cmd('lp', '-d', queue, '-n', '1', '-o', 'sides=one-sided', str(path), timeout=35)
    except Exception:
        store.event('warning', 'The supply notice could not be printed')
        return False
    finally:
        path.unlink(missing_ok=True)
    if result.returncode:
        store.event('warning', 'The supply notice could not be printed')
        return False
    _NOTICE_AT[queue] = (signature, now)
    store.event('info', f'Printed a supply notice for {queue}')
    return True


def record_rejection(filename, reason, queue='', source='email', sender='', dedupe=None, copies=1, sides='one-sided'):
    """Keep a received print in the job log when it is turned down before CUPS."""
    name = ' '.join(str(filename or 'Email').split())[:180] or 'Email'
    why = ' '.join(str(reason or 'Rejected').split())[:500]
    target = (queue or '').strip() or '—'
    try:
        copies = int(copies)
    except (TypeError, ValueError):
        copies = 1
    if not 1 <= copies <= 10:
        copies = 1
    if sides not in SIDES:
        sides = 'one-sided'
    with store.connection() as db:
        if dedupe:
            existing = db.execute('SELECT id FROM jobs WHERE dedupe=?', (dedupe,)).fetchone()
            if existing:
                return existing['id']
        cur = db.execute(
            '''INSERT INTO jobs(dedupe,source,sender,filename,queue,copies,sides,status,error)
               VALUES(?,?,?,?,?,?,?,'rejected',?)''',
            (dedupe, source, str(sender or '')[:200], name, target, copies, sides, why),
        )
        return cur.lastrowid

def submit_file(filename, data, queue, copies=1, sides='one-sided', source='upload', sender='', dedupe=None):
    cfg = store.settings()
    max_mb = max(1, min(int(cfg.get('max_mb', '12')), 30))
    safe_name = _safe_filename(filename)
    if len(data) > max_mb * 1024 * 1024:
        record_rejection(safe_name, f'File larger than {max_mb} MB limit', queue, source, sender, dedupe, copies, sides)
        raise PrintError(f'File larger than {max_mb} MB limit')
    try:
        copies = int(copies)
    except (TypeError, ValueError):
        copies = 0
    if sides not in SIDES or not 1 <= copies <= 10:
        record_rejection(safe_name, 'Invalid print options', queue, source, sender, dedupe, 1, sides)
        raise PrintError('Invalid print options')
    with store.connection() as db:
        configured = db.execute('SELECT 1 FROM printers WHERE queue=?', (queue,)).fetchone()
    if not configured:
        record_rejection(safe_name, 'Printer not configured', queue, source, sender, dedupe, copies, sides)
        raise PrintError('Printer not configured')
    hold = supply_hold(queue)
    if hold:
        record_rejection(safe_name, hold['reason'], queue, source, sender, dedupe, copies, sides)
        try:
            print_supply_notice(queue, hold)
        except Exception:
            store.event('warning', 'The supply notice could not be printed')
        raise PrintError(hold['reason'])
    with store.connection() as db:
        if dedupe:
            existing = db.execute('SELECT id, status FROM jobs WHERE dedupe=?', (dedupe,)).fetchone()
            if existing:
                if existing['status'] in ('submitted', 'cancelled', 'pending', 'sending'):
                    return existing['id']
                raise PrintError('This attachment was already processed; retry the failed job from Print jobs')
        cur = db.execute(
            '''INSERT INTO jobs(dedupe,source,sender,filename,queue,copies,sides,status)
               VALUES(?,?,?,?,?,?,?,?)''',
            (dedupe, source, sender, safe_name, queue, copies, sides, 'preparing'),
        )
        job_id = cur.lastrowid
    try:
        path, pages = prepare_bytes(safe_name, data, max(1, min(int(cfg.get('max_pages', '40')), 200)))
        with store.connection() as db:
            db.execute(
                "UPDATE jobs SET filepath=?,pages=?,status='pending',error='' WHERE id=?",
                (path, pages, job_id),
            )
        release_queues()
        return job_id
    except Exception as exc:
        with store.connection() as db:
            db.execute('UPDATE jobs SET status=?,error=? WHERE id=?', ('error', str(exc)[:500], job_id))
        raise

def sheet_for_duplex(path, sides):
    """Keep the next document off the back of this one.

    An odd-page duplex job ends on the front of a sheet. A following job can
    land on that back, so the file sent to CUPS gets one blank page.
    """
    if sides not in ('two-sided-long-edge', 'two-sided-short-edge'):
        return path
    reader = PdfReader(str(path), strict=False)
    count = len(reader.pages)
    if count < 1 or count % 2 == 0:
        return path
    writer = PdfWriter()
    writer.append(reader)
    box = reader.pages[-1].mediabox
    writer.add_blank_page(width=float(box.width), height=float(box.height))
    padded = Path(str(path) + '.' + uuid.uuid4().hex + '.sheet.pdf')
    with padded.open('wb') as handle:
        writer.write(handle)
    return str(padded)

def _queue_lock(queue):
    with _QUEUE_LOCKS_GUARD:
        lock = _QUEUE_LOCKS.get(queue)
        if lock is None:
            lock = threading.Lock()
            _QUEUE_LOCKS[queue] = lock
        return lock

def release_queues():
    """Send waiting documents, one printer at a time, and stop a queue when it is offline."""
    with store.connection() as db:
        rows = db.execute("SELECT DISTINCT queue FROM jobs WHERE status='pending' ORDER BY id").fetchall()
    for row in rows:
        lock = _queue_lock(row['queue'])
        if not lock.acquire(blocking=False):
            continue
        try:
            _release_queue(row['queue'])
        finally:
            lock.release()

def _release_queue(queue):
    while True:
        with store.connection() as db:
            printer = db.execute('SELECT host, protocol FROM printers WHERE queue=?', (queue,)).fetchone()
            job = db.execute(
                "SELECT id FROM jobs WHERE status='pending' AND queue=? ORDER BY id LIMIT 1",
                (queue,),
            ).fetchone()
        if not job:
            return
        if not printer:
            with store.connection() as db:
                db.execute(
                    "UPDATE jobs SET status='error', error=? WHERE id=? AND status='pending'",
                    ('Printer not configured', job['id']),
                )
            continue
        if not printer_reachable(printer['host'], printer['protocol'], timeout=1):
            return
        try:
            dispatch(job['id'])
        except PrintError:
            continue

def run_queue():
    while not STOP.is_set():
        try:
            release_queues()
        except Exception:
            store.event('warning', 'The print queue could not be checked')
        STOP.wait(0.4)

def requeue(job_id):
    with store.connection() as db:
        row = db.execute('SELECT status, filepath FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row or row['status'] != 'error':
            raise PrintError('Only failed jobs can be retried')
        if not row['filepath'] or not Path(row['filepath']).is_file():
            raise PrintError('The saved document is not available for retry')
        db.execute("UPDATE jobs SET status='pending', error='' WHERE id=? AND status='error'", (job_id,))
    release_queues()
    return job_id

def dispatch(job_id):
    with store.connection() as db:
        record = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not record:
            raise PrintError('Job not found')
        if not record['filepath'] or not Path(record['filepath']).is_file():
            db.execute(
                "UPDATE jobs SET status='error', error=? WHERE id=? AND status='pending'",
                ('The saved document is not available for retry', job_id),
            )
            raise PrintError('The saved document is not available for retry')
        claimed = db.execute(
            "UPDATE jobs SET status='sending', error='' WHERE id=? AND status='pending'",
            (job_id,),
        ).rowcount
    if not claimed:
        return None
    print_path = record['filepath']
    try:
        print_path = sheet_for_duplex(record['filepath'], record['sides'])
        command = ['lp', '-d', record['queue'], '-n', str(record['copies']), '-o', 'sides=' + record['sides'], print_path]
        result = cmd(*command, timeout=35)
    except Exception as exc:
        with store.connection() as db:
            db.execute('UPDATE jobs SET status=?,error=? WHERE id=?', ('error', str(exc)[:400], job_id))
        raise
    finally:
        if print_path != record['filepath']:
            Path(print_path).unlink(missing_ok=True)
    if result.returncode:
        error = (result.stderr or result.stdout or 'CUPS rejected job')[:400]
        with store.connection() as db:
            db.execute('UPDATE jobs SET status=?,error=? WHERE id=?', ('error', error, job_id))
        raise PrintError(error)
    match = re.search(r'request id is (\S+)', result.stdout or '')
    cups_id = match.group(1) if match else ''
    with store.connection() as db:
        db.execute('UPDATE jobs SET status=?,error=?,cups_id=? WHERE id=?', ('submitted', '', cups_id, job_id))
    store.event('success', f'Job {job_id} sent to {record["queue"]}: {record["filename"][:100]}')
    return job_id

def cancel_pending(job_id):
    with store.connection() as db:
        row = db.execute('SELECT status, cups_id FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise PrintError('Job not found')
        if row['status'] != 'pending' or row['cups_id']:
            raise PrintError('A document already sent to the printer cannot be cancelled')
        updated = db.execute(
            "UPDATE jobs SET status='cancelled', error='' WHERE id=? AND status='pending' AND cups_id=''",
            (job_id,),
        ).rowcount
    if not updated:
        raise PrintError('A document already sent to the printer cannot be cancelled')

IPP_STATES = {3: 'Idle', 4: 'Printing', 5: 'Stopped'}
SNMP_STATES = {3: 'Idle', 4: 'Printing', 5: 'Warming up'}
REASON_LABELS = (
    ('media-jam', 'Paper jam'),
    ('media-empty', 'Out of paper'),
    ('media-needed', 'Paper needed'),
    ('media-low', 'Paper low'),
    ('toner-empty', 'Toner empty'),
    ('toner-low', 'Toner low'),
    ('marker-supply-empty', 'Toner empty'),
    ('marker-supply-low', 'Toner low'),
    ('door-open', 'A door is open'),
    ('cover-open', 'A cover is open'),
    ('input-tray-missing', 'A tray is missing'),
    ('output-area-full', 'The output tray is full'),
    ('output-area-almost-full', 'The output tray is almost full'),
    ('paused', 'Paused'),
)
SNMP_OIDS = (
    '1.3.6.1.2.1.43.10.2.1.4.1.1',
    '1.3.6.1.2.1.43.5.1.1.17.1',
    '1.3.6.1.2.1.25.3.5.1.1.1',
) + tuple(
    f'1.3.6.1.2.1.43.11.1.1.{field}.1.{index}'
    for index in (1, 2, 3)
    for field in ('6', '8', '9')
)

def supply_percent(level, high):
    if not isinstance(level, int) or level < 0:
        return None
    if isinstance(high, int) and high > 0:
        return max(0, min(100, round(level * 100 / high)))
    if 0 <= level <= 100:
        return level
    return None

def _reason_label(keyword):
    key = str(keyword or '').split(',')[0].strip().lower()
    if not key or key == 'none':
        return ''
    for needle, label in REASON_LABELS:
        if needle in key:
            return label
    return ''

def readings_from_ipp(attrs):
    attrs = attrs or {}
    state_values = attrs.get('printer-state') or []
    state = IPP_STATES.get(state_values[0]) if state_values and isinstance(state_values[0], int) else ''
    reasons = []
    for keyword in attrs.get('printer-state-reasons') or []:
        label = _reason_label(keyword)
        if label and label not in reasons:
            reasons.append(label)
    accepting = attrs.get('printer-is-accepting-jobs') or []
    if accepting and accepting[0] is False and 'Not accepting jobs' not in reasons:
        reasons.append('Not accepting jobs')
    names = list(attrs.get('marker-names') or [])
    levels = list(attrs.get('marker-levels') or [])
    highs = list(attrs.get('marker-high-levels') or [])
    supplies = []
    for index, level in enumerate(levels):
        percent = supply_percent(level, highs[index] if index < len(highs) else 100)
        if percent is None:
            continue
        name = str(names[index]).strip() if index < len(names) and str(names[index]).strip() else 'Toner'
        supplies.append({'name': name[:40], 'percent': percent})
    fields = device_fields((attrs.get('printer-device-id') or [''])[0])
    serial = fields.get('SN') or fields.get('SERN') or ''
    firmware = ''
    for key in ('printer-firmware-string', 'printer-firmware-version', 'printer-firmware-name'):
        values = attrs.get(key) or []
        if values and str(values[0]).strip():
            firmware = str(values[0]).strip()[:80]
            break
    return {
        'state': state,
        'reasons': reasons[:4],
        'pages': None,
        'supplies': supplies[:6],
        'serial': str(serial).strip()[:40],
        'firmware': firmware,
    }

def _snmp_text(raw):
    text = str(raw or '').strip().strip('"')
    low = text.lower()
    if not text or 'no such' in low or 'nosuch' in low or low in ('null', 'nosuchobject', 'nosuchinstance'):
        return ''
    if re.fullmatch(r'(?:[0-9A-Fa-f]{2} )+[0-9A-Fa-f]{2}', text):
        try:
            text = bytes.fromhex(text).decode('utf-8', 'replace').strip()
        except ValueError:
            return ''
    return text

def _snmp_int(raw):
    text = _snmp_text(raw).split()[0] if _snmp_text(raw) else ''
    if text.lstrip('-').isdigit():
        return int(text)
    return None

def _snmp_lines(host):
    community = os.getenv('SNMP_COMMUNITY', 'public')
    if not community or not shutil.which('snmpget'):
        return None
    result = cmd('snmpget', '-v2c', '-c', community, '-t', '1', '-r', '0', '-Oqv', host, *SNMP_OIDS, timeout=4)
    lines = (result.stdout or '').splitlines()
    if len(lines) < len(SNMP_OIDS):
        return None
    return [line.strip() for line in lines[:len(SNMP_OIDS)]]

def readings_from_snmp(lines):
    blank = {'state': '', 'reasons': [], 'pages': None, 'supplies': [], 'serial': '', 'firmware': ''}
    if not lines or len(lines) < len(SNMP_OIDS):
        return blank
    pages = _snmp_int(lines[0])
    if pages is not None and pages < 0:
        pages = None
    serial = _snmp_text(lines[1])[:40]
    state = SNMP_STATES.get(_snmp_int(lines[2]) or 0, '')
    supplies = []
    for offset in range(3, len(SNMP_OIDS), 3):
        name = _snmp_text(lines[offset]) or 'Toner'
        percent = supply_percent(_snmp_int(lines[offset + 2]), _snmp_int(lines[offset + 1]))
        if percent is None:
            continue
        supplies.append({'name': name[:40], 'percent': percent})
    return {'state': state, 'reasons': [], 'pages': pages, 'supplies': supplies[:6], 'serial': serial, 'firmware': ''}

def condition(stats, online=False):
    """Toner, drum, paper, and jam as separate readings. Unknown when the printer did not say."""
    blank = {'toner': None, 'drum': None, 'paper': 'unknown', 'jam': 'unknown', 'mark': 'Toner', 'note': ''}
    if not online or not stats:
        return blank
    toner = None
    drum = None
    mark = 'Toner'
    for item in stats.get('supplies') or []:
        name = str(item.get('name') or '')
        low = name.lower()
        if any(word in low for word in ('drum', 'opc', 'imaging', 'photoconductor')):
            if drum is None:
                drum = item
            continue
        if 'ink' in low:
            mark = 'Ink'
        if toner is None and 'waste' not in low:
            toner = item
    reasons = [str(item) for item in (stats.get('reasons') or [])]
    if any(item in reasons for item in ('Out of paper', 'Paper needed')):
        paper = 'empty'
    elif 'Paper low' in reasons:
        paper = 'low'
    else:
        paper = 'ready'
    jam = 'jam' if 'Paper jam' in reasons else 'clear'
    notes = [item for item in reasons if item not in ('Out of paper', 'Paper needed', 'Paper low', 'Paper jam')]
    return {
        'toner': toner,
        'drum': drum,
        'paper': paper,
        'jam': jam,
        'mark': mark,
        'note': notes[0] if notes else '',
    }

def printer_stats(host):
    """Live snapshot. Empty when the printer does not answer IPP or SNMP."""
    try:
        ipp = readings_from_ipp(ipp_attributes(host))
        snmp = readings_from_snmp(_snmp_lines(host))
    except Exception:
        return None
    supplies = ipp['supplies'] or snmp['supplies']
    stats = {
        'state': ipp['state'] or snmp['state'],
        'reasons': ipp['reasons'],
        'pages': snmp['pages'],
        'pages_label': f"{snmp['pages']:,}" if isinstance(snmp['pages'], int) else '',
        'supplies': supplies,
        'serial': ipp['serial'] or snmp['serial'],
        'firmware': ipp['firmware'],
    }
    if not any((stats['state'], stats['pages_label'], stats['supplies'], stats['serial'], stats['firmware'], stats['reasons'])):
        return None
    return stats

def printer_snmp(host):
    lines = _snmp_lines(host)
    if not lines:
        return None
    pages = readings_from_snmp(lines)['pages']
    return str(pages) if isinstance(pages, int) else None
