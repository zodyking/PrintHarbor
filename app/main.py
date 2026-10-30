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
from . import mailer, printing, store, tailnet

@asynccontextmanager
async def lifespan(app: FastAPI):
    store.initialize()
    mailer.STOP.clear()
    thread = threading.Thread(target=mailer.run_loop, daemon=True, name='imap-poller')
    thread.start()
    yield
    mailer.STOP.set()
    thread.join(timeout=4)

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

@app.get('/', response_class=HTMLResponse)
def index(request: Request, page: str = 'overview'):
    require_auth(request)
    if page not in ('overview', 'printers', 'jobs', 'email'):
        page = 'overview'
    cfg = store.settings()
    printers = store.records('printers')
    if page in ('overview', 'printers'):
        for printer in printers:
            printer['online'] = printing.printer_reachable(printer['host'], printer['protocol'])
            printer['pages_total'] = printing.printer_snmp(printer['host']) if page == 'printers' and printer['online'] else None
    jobs = store.records('jobs', 100) if page in ('overview', 'jobs') else []
    counts = {
        'total': len(jobs),
        'errors': sum(j['status'] == 'error' for j in jobs),
        'submitted': sum(j['status'] == 'submitted' for j in jobs),
        'printers': len(printers),
    }
    return templates.TemplateResponse(request, 'dashboard.html', {
        'page': page,
        'name': request.session.get('username'),
        'token': request.session['csrf'],
        'config': cfg,
        'printers': printers,
        'jobs': jobs,
        'counts': counts,
        'mail': mailer.snapshot(),
        'events': store.records('events', 12) if page in ('overview', 'email') else [],
        'tail': tailnet.status(),
        'active_jobs': printing.active_jobs() if page == 'jobs' else EMPTY_CUPS,
        'cups_report': printing.cups_printers() if page == 'printers' else EMPTY_CUPS,
        'notice': request.query_params.get('notice', ''),
        'error': request.query_params.get('error', ''),
    })

@app.post('/printers/add')
def add_printer(request: Request, token: str = Form(...), label: str = Form(...), queue: str = Form(...), host: str = Form(...), protocol: str = Form('ipp')):
    csrf(request, token)
    try:
        printing.add_printer(label, queue, host, protocol)
        if not store.settings().get('default_queue'):
            store.set_setting('default_queue', queue)
        return nextpage('printers', 'Printer configured')
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
            return nextpage('overview', error=f'File larger than {max_mb} MB limit')
        contents = file.file.read(max_mb * 1024 * 1024 + 1)
        if len(contents) > max_mb * 1024 * 1024:
            return nextpage('overview', error=f'File larger than {max_mb} MB limit')
        printing.submit_file(file.filename or 'document.pdf', contents, printer, copies, sides)
        return nextpage('jobs', 'Document submitted to CUPS')
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
        printing.dispatch(job_id)
        return nextpage('jobs', 'Job resubmitted to CUPS')
    except Exception as exc:
        return nextpage('jobs', error=str(exc))

@app.post('/jobs/cancel')
def cancel_job(request: Request, token: str = Form(...), cups_id: str = Form(...)):
    csrf(request, token)
    try:
        printing.cancel_cups(cups_id)
        return nextpage('jobs', 'Cancellation requested')
    except Exception as exc:
        return nextpage('jobs', error=str(exc))

@app.post('/settings/save')
def save_settings(
    request: Request, token: str = Form(...), mail_enabled: str = Form('0'),
    imap_host: str = Form('imap.gmail.com'), imap_port: int = Form(993), imap_user: str = Form(''),
    imap_password: str = Form(''), imap_folder: str = Form('INBOX'), allowed_senders: str = Form(''),
    mail_recipient: str = Form(''), default_queue: str = Form(''), default_sides: str = Form('one-sided'),
    print_email_body: str = Form('0'), poll_seconds: int = Form(60), max_pages: int = Form(40),
    max_mb: int = Form(12), max_files: int = Form(5), new_pin: str = Form(''),
):
    csrf(request, token)
    enabled = '1' if mail_enabled == '1' else '0'
    print_body = '1' if print_email_body == '1' else '0'
    host = imap_host.strip()
    user = imap_user.strip()
    folder = imap_folder.strip() or 'INBOX'
    senders = allowed_senders.strip()
    recipient = mail_recipient.strip()
    queue = default_queue.strip()
    sides = default_sides.strip()
    password = ''.join(imap_password.split())
    pin = new_pin.strip()
    if not (1 <= imap_port <= 65535 and 20 <= poll_seconds <= 3600 and 1 <= max_pages <= 200 and 1 <= max_mb <= 30 and 1 <= max_files <= 10):
        return nextpage('email', error='Invalid configuration limits')
    if sides not in printing.SIDES:
        return nextpage('email', error='Invalid duplex option')
    if pin and not re.fullmatch(r'[A-Za-z0-9]{6,32}', pin):
        return nextpage('email', error='PIN must be 6 to 32 letters or digits')
    if not host:
        return nextpage('email', error='IMAP server is required')
    if queue:
        with store.connection() as db:
            if not db.execute('SELECT 1 FROM printers WHERE queue=?', (queue,)).fetchone():
                return nextpage('email', error='Choose a configured printer')
    values = {
        'mail_enabled': enabled,
        'imap_host': host,
        'imap_port': str(imap_port),
        'imap_user': user,
        'imap_folder': folder,
        'allowed_senders': senders,
        'mail_recipient': recipient,
        'default_queue': queue,
        'default_sides': sides,
        'print_email_body': print_body,
        'poll_seconds': str(poll_seconds),
        'max_pages': str(max_pages),
        'max_mb': str(max_mb),
        'max_files': str(max_files),
    }
    with store.connection() as db:
        for key, value in values.items():
            store.set_setting(key, value, db=db)
        if password:
            store.set_setting('imap_password', password, db=db)
        if pin:
            store.set_setting('email_pin', pin, db=db)
    store.event('info', 'Email printing settings updated')
    return nextpage('email', 'Settings saved')

@app.get('/api/health')
def health(request: Request):
    require_auth(request)
    return {'service': 'printharbor', 'mail': mailer.snapshot(), 'printers': len(store.records('printers'))}
