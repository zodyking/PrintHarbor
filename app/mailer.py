import base64
import email
import imaplib
import re
import secrets
import smtplib
import threading
import time
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, getaddresses
from pathlib import Path
from . import printing, store

LAST = {'checked': None, 'state': 'Not configured', 'detail': ''}
STOP = threading.Event()
_LOCK = threading.Lock()

def publish(**kwargs):
    with _LOCK:
        LAST.update(kwargs)

def snapshot():
    with _LOCK:
        return dict(LAST)

def summarize_poll(count, problems):
    if problems:
        total = count or problems
        return 'Attention', f'{problems} of {total} messages were rejected or failed'
    return 'Healthy', f'Checked inbox; processed {count} messages'

def parse_command(subject, token):
    """Require mailbox subject PIN to mitigate spoofed From addresses.
    Examples: PRINT PIN:ABC123 DUPLEX COPIES=2; PRINT PIN:ABC123 SHORT EDGE
    """
    value = subject or ''
    match = re.search(r'\bPIN:([A-Za-z0-9]{6,32})\b', value, re.I)
    if not match or not secrets.compare_digest(match.group(1).lower(), (token or '').lower()):
        return None
    normalized = re.sub(r'\bPIN:[A-Za-z0-9]+\b', '', value, flags=re.I).upper()
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
    command = parse_command(subject, config.get('email_pin', ''))
    if not command:
        return False, 'Missing or incorrect PIN in email subject', None, sender
    upper = subject.upper()
    if 'DUPLEX' not in upper and '2-SIDED' not in upper and 'SHORT EDGE' not in upper:
        fallback = config.get('default_sides') or 'one-sided'
        command['sides'] = fallback if fallback in printing.SIDES else 'one-sided'
    return True, '', command, sender

def handle_message(msg, job_prefix, config):
    ok, reason, options, sender = allowed_message(msg, config)
    if not ok:
        store.event('warning', f'Email rejected: {reason}')
        return {'accepted': False, 'jobs': 0, 'errors': 0, 'reason': reason, 'queue': config.get('default_queue', ''), 'notes': []}
    queue = config.get('default_queue', '')
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
        store.event('warning', reason)
        return {'accepted': True, 'jobs': 0, 'errors': 1, 'reason': reason, 'queue': queue, 'notes': [reason]}
    success = fail = 0
    notes = []
    for i, (name, data) in enumerate(parts[:max_files]):
        try:
            printing.submit_file(
                name, data, queue, source='email', sender=sender,
                dedupe=f'{job_prefix}:{i}', **options,
            )
            success += 1
            notes.append(f'{name} accepted by CUPS for {queue}')
        except Exception as exc:
            store.event('error', f'Email attachment {name[:90]}: {str(exc)[:230]}')
            notes.append(f'{name}: {str(exc)[:180]}')
            fail += 1
    return {'accepted': True, 'jobs': success, 'errors': fail, 'reason': '', 'queue': queue, 'notes': notes}

def result_text(result):
    queue = result.get('queue') or 'the default printer'
    notes = [line for line in result.get('notes') or [] if line]
    if not result.get('accepted'):
        reason = result.get('reason') or 'It could not be printed.'
        return 'PrintHarbor rejected this message.\n\n' + reason
    jobs = result.get('jobs') or 0
    errors = result.get('errors') or 0
    if jobs and not errors:
        head = f'PrintHarbor accepted {jobs} file(s) for {queue}.'
    elif jobs:
        head = f'PrintHarbor accepted {jobs} file(s) for {queue}. {errors} file(s) failed.'
    else:
        head = 'PrintHarbor could not print this message.'
    if notes:
        return head + '\n\n' + '\n'.join(notes)
    return head

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
    reply.set_content(result_text(result))
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

def check_once():
    conn = None
    try:
        config = store.settings()
        if config.get('mail_enabled') != '1':
            publish(state='Disabled', detail='Turn on automatic email printing below, then save.')
            return
        if not all([
            config.get('imap_host'), config.get('imap_user'), config.get('imap_password'),
            config.get('default_queue'),
        ]):
            publish(state='Needs setup', detail='Mailbox login and a default printer are required')
            return
        folder = config.get('imap_folder') or 'INBOX'
        conn = imaplib.IMAP4_SSL(config['imap_host'], int(config.get('imap_port', '993')), timeout=18)
        conn.login(config['imap_user'], config['imap_password'])
        status, _ = conn.select(folder)
        if status != 'OK':
            raise RuntimeError('Cannot select IMAP folder')
        # UIDVALIDITY prevents accidentally treating a reused UID as the same message.
        uidstatus, data = conn.status(folder, '(UIDVALIDITY)')
        uidvalidity = (data[0] or b'').decode(errors='ignore') if uidstatus == 'OK' else 'unknown'
        status, ids = conn.uid('search', None, 'UNSEEN')
        if status != 'OK':
            raise RuntimeError('Unable to search inbox')
        count = problems = 0
        for uid in (ids[0] or b'').split()[:35]:
            status, response = conn.uid('fetch', uid, '(RFC822)')
            if status != 'OK' or not response:
                store.event('warning', 'Could not read an unread email')
                problems += 1
                continue
            payload = next((item[1] for item in response if isinstance(item, tuple) and isinstance(item[1], bytes)), None)
            if not payload:
                store.event('warning', 'Could not read an unread email')
                problems += 1
                continue
            msg = email.message_from_bytes(payload, policy=policy.default)
            key = f'{config["imap_user"]}:{uidvalidity}:{uid.decode()}'
            result = handle_message(msg, key, config)
            if (not result['accepted']) or result['errors']:
                problems += 1
            try:
                notify_sender(msg, config, result)
            except Exception as exc:
                secret = config.get('imap_password') or ''
                store.event('error', 'Result email failed: ' + str(exc).replace(secret, '')[:300])
            # Mark handled mail as seen, including rejects. Print errors stay in Jobs for retry.
            conn.uid('store', uid, '+FLAGS', '(\\Seen)')
            count += 1
        state, detail = summarize_poll(count, problems)
        publish(state=state, detail=detail)
    except Exception as exc:
        secret = ''
        try:
            secret = store.settings().get('imap_password') or ''
        except Exception:
            secret = ''
        detail = str(exc).replace(secret, '') if secret else str(exc)
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
        check_once()
        try:
            delay = max(20, min(int(store.settings().get('poll_seconds', '60')), 3600))
        except (TypeError, ValueError):
            delay = 60
        STOP.wait(delay)
