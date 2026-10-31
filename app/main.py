import os
import re
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from . import mailer, printing, store, supplies, tailnet

@asynccontextmanager
async def lifespan(app: FastAPI):
    store.initialize()
    try:
        printing.retarget_saved_queues()
    except Exception:
        store.event('warning', 'Saved printers could not be checked')
    mailer.STOP.clear()
    printing.STOP.clear()
    thread = threading.Thread(target=mailer.run_loop, daemon=True, name='imap-poller')
    queue_thread = threading.Thread(target=printing.run_queue, daemon=True, name='print-queue')
    thread.start()
    queue_thread.start()
    yield
    mailer.STOP.set()
    printing.STOP.set()
    thread.join(timeout=4)
    queue_thread.join(timeout=2)

secret = os.getenv('SESSION_SECRET', '')
if len(secret) < 32 or secret.startswith('REPLACE_'):
    raise RuntimeError('Set a unique SESSION_SECRET of at least 32 characters')

app = FastAPI(title='PrintHarbor', docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=secret, same_site='strict', https_only=False, max_age=86400)
base = Path(__file__).resolve().parent
app.mount('/static', StaticFiles(directory=str(base / 'static')), name='static')
templates = Jinja2Templates(directory=str(base / 'templates'))
EMPTY_CUPS = {'ok': True, 'lines': [], 'error': ''}

def require_auth(request):
    if not request.session.get('username'):
        raise HTTPException(status_code=401, detail='Login required')

def csrf(request, value):
    require_auth(request)
    if not value or value != request.session.get('csrf'):
        raise HTTPException(status_code=403, detail='Invalid request token')

def nextpage(page='overview', notice='', error=''):
    params = {'page': page}
    if notice:
        params['notice'] = str(notice)[:180]
    if error:
        params['error'] = str(error)[:180]
    return RedirectResponse('/?' + urlencode(params), status_code=303)

@app.exception_handler(401)
async def unauthorized(request, exc):
    return RedirectResponse('/login', status_code=303)

@app.get('/login', response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get('username'):
        return RedirectResponse('/', status_code=303)
    return templates.TemplateResponse(request, 'login.html', {'error': request.query_params.get('error', '')})

@app.post('/login')
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if not store.verify_user(username, password):
        return RedirectResponse('/login?error=Invalid+credentials', status_code=303)
    request.session.clear()
    request.session['username'] = username
    request.session['csrf'] = secrets.token_urlsafe(28)
    return RedirectResponse('/', status_code=303)

@app.post('/logout')
def logout(request: Request, token: str = Form(...)):
    csrf(request, token)
    request.session.clear()
    return RedirectResponse('/login', status_code=303)

def render_page(request, page='overview', notice='', error='', config=None, folders=None):
    if page not in ('overview', 'printers', 'jobs', 'email', 'tailscale'):
        page = 'overview'
    cfg = dict(config or store.settings())
    current = (cfg.get('imap_folder') or 'INBOX').strip() or 'INBOX'
    cfg['imap_folder'] = current
    shown = []
    for name in [current, *(folders or [])]:
        if name and name not in shown:
            shown.append(name)
    printers = store.records('printers')
    edit_name = (request.query_params.get('edit') or '').strip()
    editing = next((item for item in printers if item['queue'] == edit_name), None)
    if page in ('overview', 'printers'):
        for printer in printers:
            printer['online'] = printing.printer_reachable(printer['host'], printer['protocol'])
            printer['stats'] = printing.printer_stats(printer['host']) if printer['online'] else None
            printer['supply'] = supplies.replacement_for(printer.get('identity') or printer.get('label') or '')
    jobs = store.records('jobs', 100) if page in ('overview', 'jobs') else []
    counts = {
        'total': len(jobs),
        'errors': sum(j['status'] in ('error', 'rejected') for j in jobs),
        'submitted': sum(j['status'] == 'submitted' for j in jobs),
        'pending': sum(j['status'] == 'pending' for j in jobs),
        'printers': len(printers),
    }
    return templates.TemplateResponse(request, 'dashboard.html', {
        'page': page,
        'name': request.session.get('username'),
        'token': request.session['csrf'],
        'config': cfg,
        'folders': shown,
        'printers': printers,
        'editing': editing,
        'jobs': jobs,
        'counts': counts,
        'mail': mailer.snapshot(),
        'events': store.records('events', 12) if page in ('overview', 'email') else [],
        'tail': tailnet.status(),
        'active_jobs': printing.active_jobs() if page == 'jobs' else EMPTY_CUPS,
        'cups_report': printing.cups_printers() if page == 'printers' else EMPTY_CUPS,
        'notice': notice,
        'error': error,
    })

@app.get('/', response_class=HTMLResponse)
def index(request: Request, page: str = 'overview'):
    require_auth(request)
    return render_page(
        request, page,
        request.query_params.get('notice', ''),
        request.query_params.get('error', ''),
    )

@app.post('/printers/add')
def add_printer(request: Request, token: str = Form(...), label: str = Form(...), queue: str = Form(...), host: str = Form(...), protocol: str = Form('socket')):
    csrf(request, token)
    try:
        identity = printing.add_printer(label, queue, host, protocol)
        if not store.settings().get('default_queue'):
            store.set_setting('default_queue', queue)
        return nextpage('printers', f'Printer added: {identity}'[:180])
    except Exception as exc:
        return nextpage('printers', error=str(exc))

@app.post('/printers/edit')
def edit_printer(request: Request, token: str = Form(...), queue: str = Form(...), label: str = Form(...), host: str = Form(...), action: str = Form('save')):
    csrf(request, token)
    try:
        identity = printing.update_printer(queue, label, host, reread=(action == 'reread'))
        return nextpage('printers', f'Printer saved: {identity}'[:180])
    except Exception as exc:
        return nextpage('printers', error=str(exc))

@app.post('/printers/remove')
def remove_printer(request: Request, token: str = Form(...), queue: str = Form(...)):
    csrf(request, token)
    try:
        printing.remove_printer(queue)
        if store.settings().get('default_queue') == queue:
            store.set_setting('default_queue', '')
        return nextpage('printers', 'Printer removed')
    except Exception as exc:
        return nextpage('printers', error=str(exc))

@app.post('/jobs/upload')
def upload(request: Request, token: str = Form(...), printer: str = Form(...), sides: str = Form('one-sided'), copies: int = Form(1), file: UploadFile = File(...)):
    csrf(request, token)
    try:
        max_mb = max(1, min(int(store.settings().get('max_mb', '12')), 30))
        claimed = request.headers.get('content-length', '')
        if claimed.isdigit() and int(claimed) > max_mb * 1024 * 1024 + 1024 * 1024:
            printing.record_rejection(file.filename or 'document.pdf', f'File larger than {max_mb} MB limit', printer, 'upload', request.session.get('username') or '')
            return nextpage('overview', error=f'File larger than {max_mb} MB limit')
        contents = file.file.read(max_mb * 1024 * 1024 + 1)
        if len(contents) > max_mb * 1024 * 1024:
            printing.record_rejection(file.filename or 'document.pdf', f'File larger than {max_mb} MB limit', printer, 'upload', request.session.get('username') or '')
            return nextpage('overview', error=f'File larger than {max_mb} MB limit')
        printing.submit_file(file.filename or 'document.pdf', contents, printer, copies, sides)
        return nextpage('jobs', 'Document added to the printer queue')
    except Exception as exc:
        return nextpage('overview', error=str(exc))

@app.post('/jobs/retry')
def retry(request: Request, token: str = Form(...), job_id: int = Form(...)):
    csrf(request, token)
    try:
        with store.connection() as db:
            row = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row or row['status'] != 'error':
            raise printing.PrintError('Only failed jobs can be retried')
        printing.requeue(job_id)
        return nextpage('jobs', 'Job added back to the printer queue')
    except Exception as exc:
        return nextpage('jobs', error=str(exc))

@app.post('/jobs/cancel')
def cancel_job(request: Request, token: str = Form(...), job_id: int = Form(...)):
    csrf(request, token)
    try:
        printing.cancel_pending(job_id)
        return nextpage('jobs', 'Removed from the queue')
    except Exception as exc:
        return nextpage('jobs', error=str(exc))

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

def mail_values(
    mail_enabled, imap_host, imap_port, imap_user, imap_folder, allowed_senders, mail_recipient,
    default_queue, default_sides, print_email_body, poll_seconds, max_pages, max_mb, max_files,
    reply_enabled, smtp_from, smtp_host, smtp_port,
):
    folder = imap_folder.strip()
    sender = smtp_from.strip()
    host = imap_host.strip()
    smtp = smtp_host.strip() or 'smtp.gmail.com'
    if not (1 <= imap_port <= 65535 and 1 <= smtp_port <= 65535 and 2 <= poll_seconds <= 3600 and 1 <= max_pages <= 200 and 1 <= max_mb <= 30 and 1 <= max_files <= 10):
        raise printing.PrintError('Invalid configuration limits')
    if default_sides.strip() not in printing.SIDES:
        raise printing.PrintError('Invalid duplex option')
    if not host:
        raise printing.PrintError('IMAP server is required')
    if not folder or len(folder) > 200 or any(ch in folder for ch in '\r\n\x00'):
        raise printing.PrintError('Choose a mailbox folder')
    if sender and not EMAIL_RE.fullmatch(sender):
        raise printing.PrintError('From address must be an email address')
    return {
        'mail_enabled': '1' if mail_enabled == '1' else '0',
        'imap_host': host,
        'imap_port': str(imap_port),
        'imap_user': imap_user.strip(),
        'imap_folder': folder,
        'allowed_senders': allowed_senders.strip(),
        'mail_recipient': mail_recipient.strip(),
        'default_queue': default_queue.strip(),
        'default_sides': default_sides.strip(),
        'print_email_body': '1' if print_email_body == '1' else '0',
        'poll_seconds': str(poll_seconds),
        'max_pages': str(max_pages),
        'max_mb': str(max_mb),
        'max_files': str(max_files),
        'reply_enabled': '1' if reply_enabled == '1' else '0',
        'smtp_from': sender,
        'smtp_host': smtp,
        'smtp_port': str(smtp_port),
    }

@app.post('/settings/save')
def save_settings(
    request: Request, token: str = Form(...), mail_enabled: str = Form('0'),
    imap_host: str = Form('imap.gmail.com'), imap_port: int = Form(993), imap_user: str = Form(''),
    imap_password: str = Form(''), imap_folder: str = Form('INBOX'), allowed_senders: str = Form(''),
    mail_recipient: str = Form(''), default_queue: str = Form(''), default_sides: str = Form('one-sided'),
    print_email_body: str = Form('0'), poll_seconds: int = Form(60), max_pages: int = Form(40),
    max_mb: int = Form(12), max_files: int = Form(5), new_pin: str = Form(''),
    reply_enabled: str = Form('0'), smtp_from: str = Form(''), smtp_host: str = Form('smtp.gmail.com'),
    smtp_port: int = Form(587),
):
    csrf(request, token)
    password = ''.join(imap_password.split())
    pin = new_pin.strip()
    try:
        values = mail_values(
            mail_enabled, imap_host, imap_port, imap_user, imap_folder, allowed_senders, mail_recipient,
            default_queue, default_sides, print_email_body, poll_seconds, max_pages, max_mb, max_files,
            reply_enabled, smtp_from, smtp_host, smtp_port,
        )
    except printing.PrintError as exc:
        return nextpage('email', error=str(exc))
    if pin and not re.fullmatch(r'[A-Za-z0-9]{6,32}', pin):
        return nextpage('email', error='PIN must be 6 to 32 letters or digits, or blank to require none')
    if values['default_queue']:
        with store.connection() as db:
            if not db.execute('SELECT 1 FROM printers WHERE queue=?', (values['default_queue'],)).fetchone():
                return nextpage('email', error='Choose a configured printer')
    with store.connection() as db:
        for key, value in values.items():
            store.set_setting(key, value, db=db)
        if password:
            store.set_setting('imap_password', password, db=db)
        store.set_setting('email_pin', pin, db=db)
    store.event('info', 'Email printing settings updated')
    return nextpage('email', 'Settings saved')

@app.post('/mail/folders', response_class=HTMLResponse)
def mail_folders(
    request: Request, token: str = Form(...), mail_enabled: str = Form('0'),
    imap_host: str = Form('imap.gmail.com'), imap_port: int = Form(993), imap_user: str = Form(''),
    imap_password: str = Form(''), imap_folder: str = Form('INBOX'), allowed_senders: str = Form(''),
    mail_recipient: str = Form(''), default_queue: str = Form(''), default_sides: str = Form('one-sided'),
    print_email_body: str = Form('0'), poll_seconds: int = Form(60), max_pages: int = Form(40),
    max_mb: int = Form(12), max_files: int = Form(5), new_pin: str = Form(''),
    reply_enabled: str = Form('0'), smtp_from: str = Form(''), smtp_host: str = Form('smtp.gmail.com'),
    smtp_port: int = Form(587),
):
    csrf(request, token)
    saved = store.settings()
    typed = ''.join(imap_password.split())
    secret = typed or saved.get('imap_password') or ''
    try:
        values = mail_values(
            mail_enabled, imap_host, imap_port, imap_user, imap_folder, allowed_senders, mail_recipient,
            default_queue, default_sides, print_email_body, poll_seconds, max_pages, max_mb, max_files,
            reply_enabled, smtp_from, smtp_host, smtp_port,
        )
    except printing.PrintError as exc:
        return nextpage('email', error=str(exc))
    shown = dict(saved)
    shown.update(values)
    shown['imap_password'] = saved.get('imap_password') or ''
    shown['email_pin'] = saved.get('email_pin') or ''
    if not secret or not values['imap_user']:
        return render_page(request, 'email', error='Enter the mailbox address and app password before loading folders', config=shown)
    try:
        names = mailer.list_folders(values['imap_host'], values['imap_port'], values['imap_user'], secret)
    except Exception as exc:
        text = str(exc).replace(secret, '')[:180]
        return render_page(request, 'email', error=text or 'Could not list mailbox folders', config=shown)
    return render_page(request, 'email', notice='Folders loaded', config=shown, folders=names)

@app.post('/tailscale/up')
def tailscale_up(request: Request, token: str = Form(...), auth_key: str = Form(...), hostname: str = Form('printharbor')):
    csrf(request, token)
    try:
        tailnet.connect(auth_key, hostname)
        store.event('info', 'Tailscale joined the tailnet')
        return nextpage('tailscale', 'Tailscale connected')
    except Exception as exc:
        return nextpage('tailscale', error=str(exc))

@app.post('/tailscale/down')
def tailscale_down(request: Request, token: str = Form(...)):
    csrf(request, token)
    try:
        tailnet.disconnect()
        store.event('info', 'Tailscale logged out')
        return nextpage('tailscale', 'Tailscale logged out')
    except Exception as exc:
        return nextpage('tailscale', error=str(exc))

@app.get('/api/health')
def health(request: Request):
    require_auth(request)
    return {'service': 'printharbor', 'mail': mailer.snapshot(), 'printers': len(store.records('printers'))}
