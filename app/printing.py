import io
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
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

def printer_reachable(host, protocol):
    port = 631 if protocol == 'ipp' else 9100
    try:
        with socket.create_connection((host, port), timeout=2):
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
        if tag in (0x36, 0x37, 0x41, 0x42, 0x44, 0x45, 0x47, 0x48, 0x49):
            found.setdefault(current, []).append(value.decode('utf-8', 'replace'))
    return found

def _ipp_attr(tag, name, value):
    encoded = value.encode('utf-8')
    return bytes([tag]) + len(name).to_bytes(2, 'big') + name.encode('ascii') + len(encoded).to_bytes(2, 'big') + encoded

def _ipp_request(host):
    uri = f'ipp://{host}/ipp/print'
    body = b'\x02\x00' + (0x000B).to_bytes(2, 'big') + (1).to_bytes(4, 'big') + b'\x01'
    body += _ipp_attr(0x47, 'attributes-charset', 'utf-8')
    body += _ipp_attr(0x48, 'attributes-natural-language', 'en')
    body += _ipp_attr(0x45, 'printer-uri', uri)
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

def ipp_identity(host):
    body = _ipp_request(host)
    http = (
        f'POST /ipp/print HTTP/1.1\r\nHost: {host}:631\r\n'
        f'Content-Type: application/ipp\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'
    ).encode('ascii') + body
    for payload in (http, body):
        try:
            with socket.create_connection((host, 631), timeout=3) as sock:
                sock.settimeout(3)
                sock.sendall(payload)
                parsed = parse_ipp(_read_socket(sock))
        except OSError:
            continue
        if parsed.get('printer-make-and-model') or parsed.get('printer-device-id'):
            return parsed
    return {}

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
                    return {'model': 'everywhere', 'protocol': 'ipp', 'label': identity_label, 'driver': 'Described by the printer'}
                raise PrintError('This printer needs port 9100, and that port did not answer')
            return {'model': best['name'], 'protocol': 'socket', 'label': identity_label, 'driver': 'Brother driver, darkest setting'}
        if discovery.get('socket'):
            return {'model': best['name'], 'protocol': 'socket', 'label': identity_label, 'driver': 'Matched driver'}
    if discovery.get('ipp'):
        return {'model': 'everywhere', 'protocol': 'ipp', 'label': identity_label, 'driver': 'Described by the printer'}
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

def configure_queue(queue, address, discovery, drivers):
    choice = choose_driver(discovery, drivers)
    if not MODEL_RE.fullmatch(choice['model']):
        raise PrintError('The selected driver name was not usable')
    uri = f'socket://{address}:9100' if choice['protocol'] == 'socket' else f'ipp://{address}/ipp/print'
    result = cmd('lpadmin', '-p', queue, '-E', '-v', uri, '-m', choice['model'], timeout=40)
    if result.returncode:
        raise PrintError((result.stderr or result.stdout or 'The printer could not be added')[:400])
    listed = cmd('lpoptions', '-p', queue, '-l', timeout=15)
    if listed.returncode == 0:
        for key, value in density_options(listed.stdout):
            cmd('lpadmin', '-p', queue, '-o', f'{key}={value}', timeout=15)
    cmd('cupsaccept', queue)
    cmd('cupsenable', queue)
    return choice

def _save_printer(queue, label, address, choice):
    with store.connection() as db:
        db.execute(
            'INSERT INTO printers(queue,label,host,protocol,identity,driver) VALUES (?,?,?,?,?,?) '
            'ON CONFLICT(queue) DO UPDATE SET label=excluded.label, host=excluded.host, '
            'protocol=excluded.protocol, identity=excluded.identity, driver=excluded.driver',
            (queue, label[:100], address, choice['protocol'], choice['label'][:120], choice['driver'][:80]),
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
        store.event('success', f'Printer {queue} renamed to {name}')
        return (row['identity'] or name)[:120]
    discovery = discover_printer(address)
    if not discovery.get('reachable'):
        raise PrintError('The printer did not answer. The saved printer was left unchanged.')
    choice = configure_queue(queue, address, discovery, load_drivers())
    _save_printer(queue, name, address, choice)
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
            choice = configure_queue(queue, address, discovery, drivers)
        except Exception as exc:
            store.event('warning', f'Printer {queue} was not updated: {exc}'[:240])
            continue
        _save_printer(queue, row['label'] or queue, address, choice)

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
    with store.connection() as db:
        if dedupe:
            existing = db.execute('SELECT id, status FROM jobs WHERE dedupe=?', (dedupe,)).fetchone()
            if existing:
                if existing['status'] in ('submitted', 'cancelled'):
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
            db.execute('UPDATE jobs SET filepath=?,pages=? WHERE id=?', (path, pages, job_id))
        return dispatch(job_id)
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

def dispatch(job_id):
    with store.connection() as db:
        record = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not record:
        raise PrintError('Job not found')
    if not record['filepath'] or not Path(record['filepath']).is_file():
        raise PrintError('The saved document is not available for retry')
    print_path = sheet_for_duplex(record['filepath'], record['sides'])
    command = ['lp', '-d', record['queue'], '-n', str(record['copies']), '-o', 'sides=' + record['sides'], print_path]
    try:
        result = cmd(*command, timeout=35)
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
    store.event('success', f'Job {job_id} submitted to CUPS: {record["filename"][:100]}')
    return job_id

def cancel_cups(cups_id):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,40}-\d{1,12}', cups_id):
        raise PrintError('Invalid CUPS job ID')
    result = cmd('cancel', cups_id)
    if result.returncode:
        raise PrintError(result.stderr or 'Cancel failed')
    with store.connection() as db:
        db.execute("UPDATE jobs SET status='cancelled' WHERE cups_id=?", (cups_id,))

def printer_snmp(host):
    community = os.getenv('SNMP_COMMUNITY', 'public')
    # Printer MIB total pages vary by model. This is a best-effort counter, not toner level.
    oid = '1.3.6.1.2.1.43.10.2.1.4.1.1'
    if not community or not shutil.which('snmpget'):
        return None
    result = cmd('snmpget', '-v2c', '-c', community, '-t', '1', '-r', '0', '-Oqv', host, oid, timeout=4)
    if result.returncode:
        return None
    raw = (result.stdout or '').strip().strip('"')
    return raw if raw.isdigit() else None
