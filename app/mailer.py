import base64
import email
import imaplib
import re
import secrets
import select
import smtplib
import threading
import time
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, getaddresses
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape
from . import printing, store

_REPLY = None
SIDE_LABELS = {
    'one-sided': 'Single-sided',
    'two-sided-long-edge': 'Long-edge duplex',
    'two-sided-short-edge': 'Short-edge duplex',
}

LAST = {'checked': None, 'state': 'Not configured', 'detail': ''}
STOP = threading.Event()
_LOCK = threading.Lock()

def publish(**kwargs):
    with _LOCK:
        LAST.update(kwargs)

def snapshot():
    with _LOCK:
        return dict(LAST)

def summarize_poll(count, problems, waiting=0):
    if problems:
        total = count or problems
        return 'Attention', f'{problems} of {total} new messages were rejected or failed'
    if waiting:
        return 'Watching', f'{waiting} new message(s) are waiting for the printer'
    if count:
        return 'Healthy', f'Printed {count} new message(s) from the folder'
    return 'Watching', 'Watching the folder for new mail'

def parse_command(subject, token):
    """Read duplex and copies from the subject. A PIN is required only when one is set.
    Examples: PRINT DUPLEX COPIES=2; PRINT PIN:ABC123 SHORT EDGE
    """
    value = subject or ''
    token = (token or '').strip()
    if token:
        match = re.search(r'\bPIN:([A-Za-z0-9]{6,32})\b', value, re.I)
        if not match or not secrets.compare_digest(match.group(1).lower(), token.lower()):
            return None
        normalized = re.sub(r'\bPIN:[A-Za-z0-9]+\b', '', value, flags=re.I).upper()
    else:
        normalized = value.upper()
    if 'SHORT EDGE' in normalized:
        sides = 'two-sided-short-edge'
    elif 'DUPLEX' in normalized or '2-SIDED' in normalized:
        sides = 'two-sided-long-edge'
    else:
        sides = 'one-sided'
    copies_match = re.search(r'\bCOPIES\s*[:=]\s*(\d{1,2})\b', normalized)
    copies = int(copies_match.group(1)) if copies_match else 1
    if not 1 <= copies <= 10:
        return None
    return {'sides': sides, 'copies': copies}

def _addresses(raw):
    return {v.strip().lower() for _, v in getaddresses([raw or '']) if v}

def allowed_message(msg, config):
    sender = parseaddr(msg.get('From', ''))[1].strip().lower()
    allow = {v.strip().lower() for v in config.get('allowed_senders', '').replace(';', ',').split(',') if v.strip()}
    recipient = config.get('mail_recipient', '').strip().lower()
    if not sender:
        return False, 'Sender address is missing', None, sender
    if allow and sender not in allow:
        return False, 'Sender is not allowlisted', None, sender
    if recipient and recipient not in _addresses(','.join(str(v) for v in (msg.get('To'), msg.get('Cc')) if v)):
        return False, 'Recipient does not match mailbox address', None, sender
    subject = str(msg.get('Subject', ''))
    pin = (config.get('email_pin') or '').strip()
    command = parse_command(subject, pin)
    if not command:
        if pin:
            return False, 'Missing or incorrect PIN in email subject', None, sender
        return False, 'Copies must be from 1 to 10', None, sender
    upper = subject.upper()
    if 'DUPLEX' not in upper and '2-SIDED' not in upper and 'SHORT EDGE' not in upper:
        fallback = config.get('default_sides') or 'one-sided'
        command['sides'] = fallback if fallback in printing.SIDES else 'one-sided'
    return True, '', command, sender

def message_label(msg):
    subject = ' '.join(str(msg.get('Subject') or '').split())
    return subject[:180] or 'Email'

def handle_message(msg, job_prefix, config):
    ok, reason, options, sender = allowed_message(msg, config)
    queue = config.get('default_queue', '')
    if not ok:
        printing.record_rejection(message_label(msg), reason, queue, 'email', sender, job_prefix)
        store.event('warning', f'Email rejected: {reason}')
        return {'accepted': False, 'jobs': 0, 'errors': 0, 'reason': reason, 'queue': queue, 'notes': [], 'files': []}
    max_files = max(1, min(int(config.get('max_files', '5')), 10))
    parts = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename()
        if not name:
            continue
        if Path(name).suffix.lower() in printing.ALLOWED:
            parts.append((name, part.get_payload(decode=True) or b''))
    if len(parts) > max_files:
        store.event('warning', f'Email from {sender} included {len(parts)} attachments; only {max_files} were printed')
    if not parts and config.get('print_email_body') == '1':
        body = msg.get_body(preferencelist=('plain',))
        if body:
            text = body.get_content()
            if text.strip():
                parts = [('email.txt', text.encode('utf-8'))]
    if not parts:
        reason = 'Email contained no supported attachments'
        printing.record_rejection(message_label(msg), reason, queue, 'email', sender, job_prefix, options.get('copies', 1), options.get('sides', 'one-sided'))
        store.event('warning', reason)
        return {
            'accepted': True, 'jobs': 0, 'errors': 1, 'reason': reason, 'queue': queue,
            'notes': [reason], 'files': [], 'copies': options.get('copies', 1), 'sides': options.get('sides', 'one-sided'),
        }
    success = fail = 0
    notes = []
    files = []
    failure = ''
    created = False
    for i, (name, data) in enumerate(parts[:max_files]):
        dedupe = f'{job_prefix}:{i}'
        with store.connection() as db:
            existing = db.execute('SELECT id FROM jobs WHERE dedupe=?', (dedupe,)).fetchone()
        try:
            printing.submit_file(
                name, data, queue, source='email', sender=sender,
                dedupe=dedupe, release=False, **options,
            )
            success += 1
            created = created or not existing
            notes.append(f'{name} received for {queue}')
            files.append({'name': name, 'detail': 'Received'})
        except Exception as exc:
            detail = str(exc)[:180]
            if not failure:
                failure = detail
            store.event('error', f'Email attachment {name[:90]}: {str(exc)[:230]}')
            notes.append(f'{name}: {detail}')
            files.append({'name': name, 'detail': detail})
            fail += 1
    base = {
        'accepted': True, 'jobs': success, 'errors': fail, 'reason': '' if success else failure, 'queue': queue,
        'notes': notes, 'files': files, 'copies': options.get('copies', 1), 'sides': options.get('sides', 'one-sided'),
        'held': False, 'notices': [], 'issues': [],
    }
    if not success or not created:
        return base
    remember_batch(job_prefix, sender, str(msg.get('Subject') or ''), str(msg.get('Message-ID') or ''), queue)
    received = _phase_notice(base, 'received')
    printing.release_queues()
    if _jobs_with_status(job_prefix, 'submitted'):
        set_batch_phase(job_prefix, 'sent')
        base['notices'] = [received, _phase_notice(base, 'sending')]
        return base
    if not _jobs_with_status(job_prefix, 'pending'):
        return base
    block = printing.printer_block(queue) or {'issues': ['The printer is not ready.']}
    set_batch_phase(job_prefix, 'held')
    printing._mark_waiting(queue, block['issues'])
    base['held'] = True
    base['issues'] = block['issues']
    base['reason'] = ' '.join(block['issues'])
    base['notices'] = [received, _phase_notice(base, 'issue', block['issues'])]
    return base

def reply_template():
    global _REPLY
    if _REPLY is None:
        folder = Path(__file__).resolve().parent / 'templates'
        env = Environment(loader=FileSystemLoader(folder), autoescape=select_autoescape(['html']))
        _REPLY = env.get_template('reply.html')
    return _REPLY

_FRIENDLY_REASONS = {
    'Sender address is missing': "We couldn't tell who sent this.",
    'Sender is not allowlisted': "This email address isn't approved to print.",
    'Recipient does not match mailbox address': 'Send it to the address for this printer.',
    'Missing or incorrect PIN in email subject': "The code in the subject line didn't match.",
    'Copies must be from 1 to 10': 'Ask for 1 to 10 copies.',
    'Email contained no supported attachments': 'Attach a PDF, photo, or Office document.',
    'It could not be printed.': "We couldn't print this.",
    'Printer not configured': "This printer isn't available right now.",
    'Empty attachment': 'The file was empty.',
    'Invalid PDF file': "That PDF couldn't be opened.",
    'Invalid print options': "Those print options aren't available.",
}

def customer_line(text):
    raw = ' '.join(str(text or '').split())
    if raw in _FRIENDLY_REASONS:
        return _FRIENDLY_REASONS[raw]
    if raw.lower().startswith('accepted for ') or 'accepted by cups' in raw.lower():
        return 'Printed'
    lowered = raw.lower()
    if raw.startswith('File larger than'):
        return 'This file is over the size limit.'
    if raw.startswith('Unsupported file type') or 'no supported' in lowered:
        return "That file type can't be printed. Send a PDF, photo, or Office document."
    if raw.startswith('Document has') and 'page' in lowered:
        return raw.replace('Document has', 'This document has').replace('; limit is', '. The limit is')
    if any(word in lowered for word in (
        'cups', 'lpadmin', 'lpstat', 'imap', 'smtp', 'libreoffice', 'soffice',
        'traceback', 'uidvalidity', 'not installed', 'not on path', 'timed out',
        'office conversion', 'already processed',
    )):
        return "The printer couldn't take this file. Send it again."
    return raw

def printer_name(queue):
    target = (queue or '').strip()
    if not target or target == '—':
        return 'your printer'
    try:
        with store.connection() as db:
            row = db.execute('SELECT label FROM printers WHERE queue=?', (target,)).fetchone()
        if row and str(row['label']).strip():
            return str(row['label']).strip()[:100]
    except Exception:
        pass
    return target.replace('_', ' ').strip() or 'your printer'

def _file_row(name, description):
    clean = str(name or 'File')
    ext = Path(clean).suffix.replace('.', '').upper()[:4] or 'FILE'
    return {'extension': ext, 'name': clean, 'description': customer_line(description)}

def _file_rows(result):
    rows = [_file_row(item.get('name'), item.get('detail')) for item in result.get('files') or []]
    if rows:
        return rows
    for line in result.get('notes') or []:
        text = str(line or '').strip()
        if not text:
            continue
        if ' accepted by CUPS for ' in text or ' accepted by cups for ' in text.lower():
            name = re.split(r' accepted by CUPS for ', text, flags=re.I)[0]
            rows.append(_file_row(name, 'Printed'))
        elif ': ' in text:
            name, _, detail = text.partition(': ')
            rows.append(_file_row(name, detail))
        else:
            rows.append({'extension': 'MSG', 'name': 'This message', 'description': customer_line(text)})
    return [row for row in rows if row['name'] != 'This message']

def _settings_line(result):
    copies = result.get('copies')
    sides = result.get('sides')
    if copies in (None, '') and not sides:
        return ''
    try:
        count = int(copies or 1)
    except (TypeError, ValueError):
        count = 1
    copy_word = 'copy' if count == 1 else 'copies'
    layout = SIDE_LABELS.get(sides or '', sides or 'Single-sided')
    return f'{count} {copy_word} · {layout}'

def _files_are(count):
    if count == 1:
        return 'Your file is'
    return f'Your {count} files are'

def _phase_notice(base, phase, issues=None):
    notice = dict(base)
    notice['phase'] = phase
    notice['issues'] = list(issues or [])
    notice['notices'] = []
    detail = {
        'received': 'Received',
        'sending': 'Sending to the printer',
        'issue': 'Waiting for the printer',
    }[phase]
    files = []
    for item in base.get('files') or []:
        row = dict(item)
        if row.get('detail') == 'Received':
            row['detail'] = detail
        files.append(row)
    notice['files'] = files
    if phase == 'issue':
        notice['reason'] = ' '.join(issues or [])
    return notice


def _jobs_with_status(prefix, status):
    with store.connection() as db:
        rows = db.execute('SELECT dedupe, status FROM jobs').fetchall()
    head = f'{prefix}:'
    return any((row['dedupe'] or '').startswith(head) and row['status'] == status for row in rows)


def remember_batch(prefix, sender, subject, message_id, queue):
    with store.connection() as db:
        db.execute(
            '''INSERT OR IGNORE INTO mail_batches(prefix, sender, subject, message_id, queue, phase)
               VALUES(?,?,?,?,?,'new')''',
            (prefix, str(sender or '')[:200], str(subject or '')[:300], str(message_id or '')[:300], queue or ''),
        )


def set_batch_phase(prefix, phase):
    with store.connection() as db:
        db.execute('UPDATE mail_batches SET phase=? WHERE prefix=?', (phase, prefix))


def note_dispatched(record):
    """After a held email job is sent, confirm that it is on the way."""
    if not record or (record['source'] or '') != 'email':
        return
    dedupe = record['dedupe'] or ''
    if ':' not in dedupe:
        return
    prefix = dedupe.rsplit(':', 1)[0]
    with store.connection() as db:
        batch = db.execute('SELECT * FROM mail_batches WHERE prefix=?', (prefix,)).fetchone()
        if not batch or batch['phase'] != 'held':
            return
        claimed = db.execute(
            "UPDATE mail_batches SET phase='sent' WHERE prefix=? AND phase='held'",
            (prefix,),
        ).rowcount
        rows = db.execute('SELECT filename, copies, sides, queue, dedupe FROM jobs').fetchall()
    if not claimed:
        return
    head = prefix + ':'
    matched = [row for row in rows if (row['dedupe'] or '').startswith(head)]
    original = EmailMessage()
    original['From'] = batch['sender']
    original['Subject'] = batch['subject'] or 'Print job'
    if batch['message_id']:
        original['Message-ID'] = batch['message_id']
    first = matched[0] if matched else None
    result = {
        'accepted': True,
        'jobs': len(matched) or 1,
        'errors': 0,
        'reason': '',
        'queue': batch['queue'] or (first['queue'] if first else ''),
        'phase': 'sending',
        'issues': [],
        'notes': [],
        'files': [{'name': row['filename'], 'detail': 'Sending to the printer'} for row in matched],
        'copies': first['copies'] if first else 1,
        'sides': first['sides'] if first else 'one-sided',
    }
    try:
        notify_sender(original, store.settings(), result)
    except Exception as exc:
        store.event('warning', 'The sending confirmation could not be emailed: ' + str(exc)[:180])


def reply_context(result):
    printer = printer_name(result.get('queue'))
    named = printer != 'your printer'
    jobs = result.get('jobs') or 0
    errors = result.get('errors') or 0
    reason = customer_line(result.get('reason') or "We couldn't print this.")
    phase = result.get('phase') or ''
    issues = [str(item) for item in (result.get('issues') or []) if str(item).strip()]
    where = printer if named else 'the printer'
    if phase == 'received':
        outcome = 'received'
        eyebrow = 'DOCUMENTS RECEIVED'
        hero = ('Your documents', 'were received.')
        intro = f'{_files_are(jobs)} here. The printer is checked before anything is sent.'
        card_label = 'PRINTER'
        card_value = printer if named else 'Your printer'
        card_note = 'Nothing is sent until the printer looks ready.'
        preview = f'Your documents were received for {card_value}.'
    elif phase == 'sending':
        outcome = 'sending'
        eyebrow = 'SENT TO THE PRINTER'
        hero = ("It's being", 'sent.')
        intro = f'{_files_are(jobs)} being sent to {where}.'
        card_label = 'PRINTER'
        card_value = printer if named else 'Your printer'
        card_note = 'Pick up your pages there.'
        preview = f'Your documents are being sent to {card_value}.'
    elif phase == 'issue':
        outcome = 'issue'
        eyebrow = 'PRINTER NEEDS ATTENTION'
        hero = ('The printer', 'needs attention.')
        intro = 'Your documents are waiting in the queue. They will be sent when the printer is ready.'
        card_label = 'PRINTER'
        card_value = printer if named else 'Your printer'
        card_note = 'These stay in the queue until every reported reading looks ready.'
        preview = 'The printer needs attention. ' + ' '.join(issues)
    else:
        if not result.get('accepted') or not jobs:
            outcome = 'rejected'
        elif errors:
            outcome = 'partial'
        else:
            outcome = 'accepted'
        where = printer if named else 'the printer'
    if phase in ('received', 'sending', 'issue'):
        pass
    elif outcome == 'accepted':
        eyebrow = 'PRINT CONFIRMED'
        hero = ("It's on", 'the way.')
        intro = f'{_files_are(jobs)} on the way to {where}.'
        card_label = 'PRINTER'
        card_value = printer if named else 'Your printer'
        card_note = 'Pick up your pages there.'
        preview = f'Your print is on the way to {card_value}.'
    elif outcome == 'partial':
        eyebrow = 'PARTLY PRINTED'
        hero = ('Some files', 'were printed.')
        intro = f'{jobs} printed, {errors} not. Details are below.'
        card_label = 'PRINTER'
        card_value = printer if named else 'Your printer'
        card_note = 'The files that did not print are listed below.'
        preview = 'Some of your files were printed.'
    else:
        eyebrow = 'NOT PRINTED'
        hero = ('This was', 'not printed.')
        intro = "We got your message, but it didn't go to the printer."
        card_label = 'REASON'
        card_value = reason
        card_note = 'Fix this, then send it again.'
        preview = f"We couldn't print this. {reason}"
    files = _file_rows(result)
    return {
        'title': 'Print Harbor',
        'preview': preview,
        'outcome': outcome,
        'eyebrow': eyebrow,
        'hero_line_1': hero[0],
        'hero_line_2': hero[1],
        'intro': intro,
        'card_label': card_label,
        'card_value': card_value,
        'card_note': card_note,
        'issues': issues,
        'files': files,
        'show_files': bool(files),
        'printer': printer if named else '',
        'settings_line': _settings_line(result),
        'printer_image': bool(result.get('printer_image')),
    }

def render_reply(result):
    return reply_template().render(**reply_context(result))

def result_text(result):
    ctx = reply_context(result)
    lines = [f"{ctx['hero_line_1']} {ctx['hero_line_2']}", '', ctx['intro']]
    if ctx['outcome'] == 'rejected':
        lines += ['', ctx['card_value']]
    elif ctx['printer']:
        lines += ['', f"Printer: {ctx['printer']}"]
    for item in ctx.get('issues') or []:
        lines.append(item)
    for item in ctx['files']:
        lines.append(f"{item['name']} — {item['description']}")
    if ctx['settings_line']:
        lines += ['', ctx['settings_line']]
    return '\n'.join(lines)

def notify_sender(original, config, result):
    if config.get('reply_enabled') != '1':
        return
    recipient = parseaddr(original.get('From', ''))[1].strip()
    login = (config.get('imap_user') or '').strip()
    sender = (config.get('smtp_from') or login).strip()
    password = config.get('imap_password') or ''
    if not recipient or not sender or not login or not password:
        return
    reply = EmailMessage()
    reply['From'] = sender
    reply['To'] = recipient
    subject = str(original.get('Subject') or 'Print job')
    reply['Subject'] = subject if subject.lower().startswith('re:') else 'Re: ' + subject
    message_id = original.get('Message-ID')
    if message_id:
        reply['In-Reply-To'] = message_id
        reply['References'] = message_id
    image = None
    try:
        from . import images
        image = images.email_bytes(result.get('queue') or '')
    except Exception:
        image = None
    shown = dict(result)
    shown['printer_image'] = bool(image)
    reply.set_content(result_text(shown))
    reply.add_alternative(render_reply(shown), subtype='html')
    if image:
        data, subtype = image
        reply.get_body(preferencelist=('html',)).add_related(
            data, maintype='image', subtype=subtype, cid='<printer>',
        )
    host = (config.get('smtp_host') or 'smtp.gmail.com').strip()
    port = int(config.get('smtp_port') or '587')
    if port == 465:
        client = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        client = smtplib.SMTP(host, port, timeout=20)
        client.ehlo()
        client.starttls()
        client.ehlo()
    try:
        client.login(login, password)
        client.send_message(reply)
    finally:
        try:
            client.quit()
        except Exception:
            pass

def decode_imap_utf7(text):
    if text is None:
        return ''
    if isinstance(text, bytes):
        text = text.decode('ascii', 'replace')
    out = []
    i = 0
    while i < len(text):
        if text[i] != '&':
            out.append(text[i])
            i += 1
            continue
        end = text.find('-', i)
        if end == -1:
            out.append(text[i:])
            break
        token = text[i + 1:end]
        if token == '':
            out.append('&')
        else:
            padded = token.replace(',', '/')
            padded += '=' * ((4 - len(padded) % 4) % 4)
            out.append(base64.b64decode(padded).decode('utf-16-be'))
        i = end + 1
    return ''.join(out)

def mailbox_name(line):
    if isinstance(line, bytes):
        line = line.decode('latin-1', 'replace')
    if line.startswith('('):
        end = line.find(')')
        if end != -1:
            line = line[end + 1:].strip()
    if line[:3].upper() == 'NIL':
        line = line[3:].strip()
    elif line.startswith('"'):
        end = line.find('"', 1)
        line = line[end + 1:].strip() if end != -1 else line[1:]
    line = line.strip()
    if len(line) >= 2 and line.startswith('"') and line.endswith('"'):
        line = line[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    return decode_imap_utf7(line)

def list_folders(host, port, user, password):
    conn = imaplib.IMAP4_SSL(host, int(port), timeout=18)
    try:
        conn.login(user, password)
        status, data = conn.list()
        if status != 'OK':
            raise RuntimeError('Could not list mailbox folders')
        names = []
        for item in data or []:
            if not item:
                continue
            name = mailbox_name(item)
            if name and name not in names:
                names.append(name)
        if not names:
            raise RuntimeError('The mailbox returned no folders')
        return names
    finally:
        try:
            conn.logout()
        except Exception:
            pass

def uids_after(payload, last_uid):
    raw = payload or b''
    if isinstance(raw, str):
        raw = raw.encode()
    found = []
    last = int(last_uid)
    for piece in raw.split():
        try:
            number = int(piece)
        except ValueError:
            continue
        if number > last:
            found.append(piece)
    return found

def parse_status(payload):
    text = payload.decode('utf-8', 'replace') if isinstance(payload, bytes) else str(payload)
    validity = re.search(r'UIDVALIDITY\s+(\d+)', text)
    nxt = re.search(r'UIDNEXT\s+(\d+)', text)
    if not validity or not nxt:
        raise RuntimeError('Mailbox did not report a position')
    return int(validity.group(1)), int(nxt.group(1))

def cursor_key(config):
    host = (config.get('imap_host') or '').strip()
    user = (config.get('imap_user') or '').strip()
    folder = (config.get('imap_folder') or 'INBOX').strip() or 'INBOX'
    return host + '\n' + user + '\n' + folder

def mailbox_identity(config):
    return (
        (config.get('imap_host') or '').strip(),
        str(config.get('imap_port') or '993'),
        (config.get('imap_user') or '').strip(),
        (config.get('imap_folder') or 'INBOX').strip() or 'INBOX',
        config.get('imap_password') or '',
    )

def fallback_wait(config):
    try:
        value = int(config.get('poll_seconds', '3'))
    except (TypeError, ValueError):
        value = 3
    return max(2, min(value, 5))

def ready(config):
    if config.get('mail_enabled') != '1':
        publish(state='Disabled', detail='Turn on email printing, then save.')
        return False
    if not all([
        config.get('imap_host'), config.get('imap_user'), config.get('imap_password'),
        config.get('default_queue'),
    ]):
        publish(state='Needs setup', detail='Mailbox login and a default printer are required')
        return False
    return True

def adopt_cursor(config, uidvalidity, uidnext):
    key = cursor_key(config)
    validity = str(uidvalidity)
    last_raw = str(config.get('mail_last_uid') or '')
    if config.get('mail_watch') == key and config.get('mail_uidvalidity') == validity and last_raw.isdigit():
        return int(last_raw), False
    last = max(int(uidnext) - 1, 0)
    store.set_setting('mail_watch', key)
    store.set_setting('mail_uidvalidity', validity)
    store.set_setting('mail_last_uid', str(last))
    return last, True

def supports_idle(conn):
    typ, data = conn.capability()
    if typ != 'OK':
        return False
    blob = b' '.join(item if isinstance(item, bytes) else str(item).encode() for item in (data or []))
    return b'IDLE' in blob.upper()

def wait_for_new_mail(conn, seconds=30):
    tag = conn._new_tag()
    if isinstance(tag, str):
        tag = tag.encode()
    conn.send(tag + b' IDLE\r\n')
    reply = conn.readline()
    if not reply.startswith(b'+'):
        raise RuntimeError('Mailbox cannot watch for new mail')
    changed = False
    try:
        deadline = time.monotonic() + seconds
        while not STOP.is_set() and time.monotonic() < deadline:
            remaining = max(0, deadline - time.monotonic())
            try:
                readable, _, _ = select.select([conn.sock], [], [], min(1, remaining))
            except (OSError, ValueError) as exc:
                raise RuntimeError('Mailbox watch stopped') from exc
            if not readable:
                continue
            line = conn.readline()
            if not line:
                raise RuntimeError('Mailbox connection closed')
            upper = line.upper()
            if b'EXISTS' in upper or b'RECENT' in upper:
                changed = True
                break
            if b'BYE' in upper:
                raise RuntimeError('Mailbox connection closed')
    finally:
        try:
            conn.sock.settimeout(3)
        except Exception:
            pass
        try:
            conn.send(b'DONE\r\n')
            while True:
                line = conn.readline()
                if not line or line.startswith(tag):
                    break
        except Exception:
            pass
        try:
            conn.sock.settimeout(None)
        except Exception:
            pass
    return changed

def take_new(conn, config, folder):
    status, data = conn.status(folder, '(UIDVALIDITY UIDNEXT)')
    if status != 'OK' or not data or not data[0]:
        raise RuntimeError('Cannot read mailbox position')
    validity, uidnext = parse_status(data[0])
    last, primed = adopt_cursor(config, validity, uidnext)
    if primed:
        publish(state='Watching', detail=f'Watching {folder} for new mail. Mail already there is left alone.')
        return False
    status, ids = conn.uid('search', None, 'UID', f'{last + 1}:*')
    if status != 'OK':
        raise RuntimeError('Unable to search the folder')
    pending = uids_after(ids[0] if ids else b'', last)[:35]
    if not pending:
        publish(state='Watching', detail=f'Watching {folder} for new mail.')
        return False
    count = problems = waiting = 0
    for uid in pending:
        uid_text = uid.decode() if isinstance(uid, bytes) else str(uid)
        status, response = conn.uid('fetch', uid, '(BODY.PEEK[])')
        payload = None
        if status == 'OK' and response:
            payload = next((item[1] for item in response if isinstance(item, tuple) and isinstance(item[1], bytes)), None)
        if not payload:
            store.event('warning', 'Could not read a new email')
            problems += 1
        else:
            msg = email.message_from_bytes(payload, policy=policy.default)
            key = f'{config["imap_user"]}:{validity}:{uid_text}'
            result = handle_message(msg, key, config)
            if (not result['accepted']) or result['errors']:
                problems += 1
            elif result.get('held'):
                waiting += 1
            secret = config.get('imap_password') or ''
            for notice in result.get('notices') or [result]:
                try:
                    notify_sender(msg, config, notice)
                except Exception as exc:
                    store.event('error', 'Result email failed: ' + str(exc).replace(secret, '')[:300])
        try:
            conn.uid('store', uid, '+FLAGS', '(\\Seen)')
        except Exception:
            store.event('warning', 'Could not mark a handled email as seen')
        store.set_setting('mail_last_uid', uid_text)
        count += 1
    state, detail = summarize_poll(count, problems, waiting)
    publish(state=state, detail=detail)
    return len(pending) >= 35

def _scrub(exc):
    secret = ''
    try:
        secret = store.settings().get('imap_password') or ''
    except Exception:
        secret = ''
    detail = str(exc).replace(secret, '') if secret else str(exc)
    return detail

def check_once():
    conn = None
    try:
        config = store.settings()
        if not ready(config):
            return
        folder = config.get('imap_folder') or 'INBOX'
        conn = imaplib.IMAP4_SSL(config['imap_host'], int(config.get('imap_port', '993')), timeout=18)
        conn.login(config['imap_user'], config['imap_password'])
        status, _ = conn.select(folder)
        if status != 'OK':
            raise RuntimeError('Cannot select IMAP folder')
        take_new(conn, config, folder)
    except Exception as exc:
        detail = _scrub(exc)
        publish(state='Error', detail=detail[:170])
        store.event('error', 'Mail check failed: ' + detail[:300])
    finally:
        publish(checked=time.strftime('%Y-%m-%d %H:%M:%S'))
        if conn:
            try:
                conn.logout()
            except Exception:
                pass

def run_loop():
    while not STOP.is_set():
        conn = None
        try:
            config = store.settings()
            if not ready(config):
                STOP.wait(2)
                continue
            folder = (config.get('imap_folder') or 'INBOX').strip() or 'INBOX'
            identity = mailbox_identity(config)
            conn = imaplib.IMAP4_SSL(config['imap_host'], int(config.get('imap_port', '993')), timeout=18)
            conn.login(config['imap_user'], config['imap_password'])
            status, _ = conn.select(folder)
            if status != 'OK':
                raise RuntimeError('Cannot select IMAP folder')
            idle = supports_idle(conn)
            while not STOP.is_set():
                fresh = store.settings()
                if not ready(fresh) or mailbox_identity(fresh) != identity:
                    break
                if take_new(conn, fresh, folder):
                    continue
                if idle:
                    wait_for_new_mail(conn)
                elif STOP.wait(fallback_wait(fresh)):
                    break
        except Exception as exc:
            detail = _scrub(exc)
            publish(state='Error', detail=detail[:170])
            store.event('error', 'Mail check failed: ' + detail[:300])
            STOP.wait(3)
        finally:
            publish(checked=time.strftime('%Y-%m-%d %H:%M:%S'))
            if conn:
                try:
                    conn.logout()
                except Exception:
                    pass
