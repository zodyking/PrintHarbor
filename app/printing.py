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
from pypdf import PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas
from . import store

Image.MAX_IMAGE_PIXELS = 40_000_000

ALLOWED = {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff', '.txt', '.docx', '.xlsx', '.odt', '.ods'}
SIDES = {'one-sided', 'two-sided-long-edge', 'two-sided-short-edge'}
QUEUE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,40}$')

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

def add_printer(label, queue, ip, protocol):
    if not QUEUE_RE.fullmatch(queue):
        raise PrintError('Queue must begin with a letter and contain only letters, numbers, _ or -')
    try:
        address = str(ipaddress.ip_address(ip))
    except ValueError:
        raise PrintError('Enter a numeric printer IP address')
    if not isinstance(ipaddress.ip_address(address), ipaddress.IPv4Address):
        raise PrintError('IPv4 printer IP required')
    if protocol not in ('ipp', 'socket'):
        raise PrintError('Unsupported connection method')
    uri = f'ipp://{address}/ipp/print' if protocol == 'ipp' else f'socket://{address}:9100'
    # Brother HL-L2380DW supports IPP/AirPrint and BR-Script3. CUPS may need a
    # device-specific PPD if its firmware does not expose IPP Everywhere.
    model = 'everywhere' if protocol == 'ipp' else 'drv:///sample.drv/generic.ppd'
    result = cmd('lpadmin', '-p', queue, '-E', '-v', uri, '-m', model, timeout=35)
    if result.returncode:
        raise PrintError((result.stderr or result.stdout or 'CUPS rejected printer')[:400])
    cmd('cupsaccept', queue)
    cmd('cupsenable', queue)
    with store.connection() as db:
        db.execute(
            'INSERT INTO printers(queue,label,host,protocol) VALUES (?,?,?,?) '
            'ON CONFLICT(queue) DO UPDATE SET label=excluded.label,host=excluded.host,protocol=excluded.protocol',
            (queue, label[:100], address, protocol),
        )
    store.event('success', f'Printer {queue} configured ({protocol} on {address})')

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

def submit_file(filename, data, queue, copies=1, sides='one-sided', source='upload', sender='', dedupe=None):
    cfg = store.settings()
    max_mb = max(1, min(int(cfg.get('max_mb', '12')), 30))
    if len(data) > max_mb * 1024 * 1024:
        raise PrintError(f'File larger than {max_mb} MB limit')
    copies = int(copies)
    if sides not in SIDES or not 1 <= copies <= 10:
        raise PrintError('Invalid print options')
    safe_name = _safe_filename(filename)
    with store.connection() as db:
        if not db.execute('SELECT 1 FROM printers WHERE queue=?', (queue,)).fetchone():
            raise PrintError('Printer not configured')
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

def dispatch(job_id):
    with store.connection() as db:
        record = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not record:
        raise PrintError('Job not found')
    if not record['filepath'] or not Path(record['filepath']).is_file():
        raise PrintError('The saved document is not available for retry')
    command = ['lp', '-d', record['queue'], '-n', str(record['copies']), '-o', 'sides=' + record['sides'], record['filepath']]
    result = cmd(*command, timeout=35)
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
