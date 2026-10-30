import email
import imaplib
import re
import secrets
import threading
import time
from email import policy
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
    if not sender or sender not in allow or not allow:
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
        return {'accepted': False, 'jobs': 0, 'errors': 0}
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
        store.event('warning', 'Email contained no supported attachments')
        return {'accepted': True, 'jobs': 0, 'errors': 1}
    success = fail = 0
    for i, (name, data) in enumerate(parts[:max_files]):
        try:
            printing.submit_file(
                name, data, queue, source='email', sender=sender,
                dedupe=f'{job_prefix}:{i}', **options,
            )
            success += 1
        except Exception as exc:
            store.event('error', f'Email attachment {name[:90]}: {str(exc)[:230]}')
            fail += 1
    return {'accepted': True, 'jobs': success, 'errors': fail}

def check_once():
    conn = None
    try:
        config = store.settings()
        if config.get('mail_enabled') != '1':
            publish(state='Disabled', detail='Enable mail intake in Settings')
            return
        if not all([
            config.get('imap_host'), config.get('imap_user'), config.get('imap_password'),
            config.get('default_queue'), config.get('allowed_senders'),
        ]):
            publish(state='Needs setup', detail='Mailbox, sender allowlist, and printer queue are required')
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
            # Mark handled mail as seen, including rejects. Print errors stay in Jobs for retry.
            conn.uid('store', uid, '+FLAGS', '(\\Seen)')
            count += 1
        state, detail = summarize_poll(count, problems)
        publish(state=state, detail=detail)
    except Exception as exc:
        publish(state='Error', detail=str(exc)[:170])
        store.event('error', 'Mail check failed: ' + str(exc)[:300])
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
