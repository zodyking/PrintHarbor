"""Brother printer readings over SNMP, the same values Home Assistant reads on port 161.

The maintenance and counter blobs are Brother's own packed records. A laser
printer such as the HL-L2380DW reports toner, drum life, drum pages, and the
door, paper, and jam flags from those records.
"""
import os
import re
import socket

OID_MODEL = '1.3.6.1.4.1.2435.2.3.9.1.1.7.0'
OID_SERIAL = '1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.0'
OID_STATUS = '1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.2.0'
OID_COUNTERS = '1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.10.0'
OID_MAINTENANCE = '1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.8.0'
OID_NEXTCARE = '1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.11.0'
OID_PAGES = '1.3.6.1.2.1.43.10.2.1.4.1.1'
OID_DEVICE = '1.3.6.1.2.1.25.3.2.1.5.1'
OID_PRINTER = '1.3.6.1.2.1.25.3.5.1.1.1'
OID_ERRORS = '1.3.6.1.2.1.25.3.5.1.2.1'

ASK = (
    OID_MODEL, OID_SERIAL, OID_STATUS, OID_COUNTERS, OID_MAINTENANCE, OID_NEXTCARE,
    OID_PAGES, OID_DEVICE, OID_PRINTER, OID_ERRORS,
)

DEVICE_STATUS = {'1': 'Unknown', '2': 'Running', '3': 'Warning', '4': 'Testing', '5': 'Down'}
PRINTER_STATUS = {'1': 'Other', '2': 'Unknown', '3': 'Idle', '4': 'Printing', '5': 'Warming up'}
ERROR_BITS = (
    'low_paper', 'no_paper', 'low_toner', 'no_toner', 'door_open', 'jammed', 'offline',
    'service_requested', 'input_tray_missing', 'output_tray_missing', 'marker_supply_missing',
    'output_near_full', 'output_full', 'input_tray_empty', 'overdue_prevent_maint',
)
COUNTERS = {
    '00': 'pages', '01': 'bw_pages', '02': 'color_pages', '06': 'duplex_pages',
    '12': 'black_pages', '13': 'cyan_pages', '14': 'magenta_pages', '15': 'yellow_pages',
    '16': 'image_pages',
}
LASER = {
    '11': 'drum_pages',
    '41': 'drum', '69': 'belt', '6a': 'fuser', '6b': 'laser',
    '6c': 'pf_mp', '6d': 'pf_1',
    '6f': 'black_toner', '70': 'cyan_toner', '71': 'magenta_toner', '72': 'yellow_toner',
    '73': 'cyan_drum_pages', '74': 'magenta_drum_pages', '75': 'yellow_drum_pages',
    '79': 'cyan_drum', '7a': 'magenta_drum', '7b': 'yellow_drum', '7e': 'black_drum_pages',
    '80': 'black_drum',
    'a1': 'black_toner', 'a2': 'cyan_toner', 'a3': 'magenta_toner', 'a4': 'yellow_toner',
}
INK = {
    '6f': 'black_ink', '70': 'cyan_ink', '71': 'magenta_ink', '72': 'yellow_ink',
    'a1': 'black_ink', 'a2': 'cyan_ink', 'a3': 'magenta_ink', 'a4': 'yellow_ink',
}
NEXTCARE = {
    '73': 'laser_pages_left', '77': 'pf_1_pages_left', '82': 'drum_pages_left',
    '86': 'pf_mp_pages_left', '88': 'belt_pages_left', '89': 'fuser_pages_left',
    'a4': 'black_drum_pages_left', 'a5': 'cyan_drum_pages_left',
    'a6': 'magenta_drum_pages_left', 'a7': 'yellow_drum_pages_left',
}
PERCENTS = {
    'drum', 'belt', 'fuser', 'laser', 'pf_mp', 'pf_1',
    'black_toner', 'cyan_toner', 'magenta_toner', 'yellow_toner',
    'black_ink', 'cyan_ink', 'magenta_ink', 'yellow_ink',
    'black_drum', 'cyan_drum', 'magenta_drum', 'yellow_drum',
}
SUPPLY_NAMES = {
    'black_toner': 'Black Toner', 'cyan_toner': 'Cyan Toner',
    'magenta_toner': 'Magenta Toner', 'yellow_toner': 'Yellow Toner',
    'black_ink': 'Black Ink', 'cyan_ink': 'Cyan Ink',
    'magenta_ink': 'Magenta Ink', 'yellow_ink': 'Yellow Ink',
    'drum': 'Drum', 'black_drum': 'Black Drum', 'cyan_drum': 'Cyan Drum',
    'magenta_drum': 'Magenta Drum', 'yellow_drum': 'Yellow Drum',
}


def looks_like_brother(text):
    low = (text or '').lower()
    if 'brother' in low:
        return True
    return bool(re.search(r'\b(?:hl|mfc|dcp|fax)-', low))


def _kind(identity):
    low = (identity or '').lower()
    if any(word in low for word in ('inkjet', ' ink', 'dcp-j', 'mfc-j')):
        return 'ink'
    return 'laser'


def _len(value):
    size = len(value)
    if size < 128:
        return bytes([size])
    raw = size.to_bytes((size.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag, value):
    return bytes([tag]) + _len(value) + value


def _integer(number):
    raw = int(number).to_bytes((int(number).bit_length() + 8) // 8 or 1, 'big', signed=True)
    return _tlv(0x02, raw)


def _oid(oid):
    parts = [int(part) for part in oid.split('.') if part != '']
    body = bytes([parts[0] * 40 + parts[1]])
    for part in parts[2:]:
        if part < 128:
            body += bytes([part])
            continue
        stack = [part & 0x7F]
        part >>= 7
        while part:
            stack.append(0x80 | (part & 0x7F))
            part >>= 7
        body += bytes(reversed(stack))
    return _tlv(0x06, body)


def _decode_oid(raw):
    if not raw:
        return ''
    first, second = divmod(raw[0], 40)
    parts = [str(first), str(second)]
    value = 0
    for byte in raw[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(str(value))
            value = 0
    return '.'.join(parts)


def _walk(data, index=0):
    if index >= len(data):
        raise ValueError('short snmp')
    tag = data[index]
    index += 1
    size = data[index]
    index += 1
    if size & 0x80:
        count = size & 0x7F
        size = int.from_bytes(data[index:index + count], 'big')
        index += count
    return tag, data[index:index + size], index + size


def _decode_value(tag, raw):
    if tag == 0x02 or tag in (0x41, 0x42, 0x43, 0x46):
        return int.from_bytes(raw, 'big', signed=(tag == 0x02))
    if tag == 0x04:
        return raw
    if tag == 0x06:
        return _decode_oid(raw)
    return None


def decode_message(data):
    """Return request id, error status, error index, and OID values from an SNMP reply."""
    _, message, _ = _walk(data)
    _, _, at = _walk(message)
    _, _, at = _walk(message, at)
    _, pdu, _ = _walk(message, at)
    _, request, at = _walk(pdu)
    _, status, at = _walk(pdu, at)
    _, index, at = _walk(pdu, at)
    _, binds, _ = _walk(pdu, at)
    found = {}
    cursor = 0
    while cursor < len(binds):
        _, pair, cursor = _walk(binds, cursor)
        _, oid_raw, mid = _walk(pair)
        tag, raw, _ = _walk(pair, mid)
        found[_decode_oid(oid_raw)] = _decode_value(tag, raw)
    return int.from_bytes(request, 'big', signed=True), int.from_bytes(status, 'big', signed=True), int.from_bytes(index, 'big', signed=True), found


def _request(oids, community):
    binds = b''.join(_tlv(0x30, _oid(oid) + _tlv(0x05, b'')) for oid in oids)
    pdu = _tlv(0xA0, _integer(1) + _integer(0) + _integer(0) + _tlv(0x30, binds))
    return _tlv(0x30, _integer(0) + _tlv(0x04, community.encode('ascii')) + pdu)


def snmp_get(host, oids, community='public', port=161, timeout=1.2):
    """One SNMPv1 GET. None when the printer does not answer."""
    if not oids:
        return {}
    packet = _request(oids, community)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, (host, port))
            payload, _ = sock.recvfrom(65535)
    except OSError:
        return None
    try:
        _, status, index, values = decode_message(payload)
    except (ValueError, IndexError, OverflowError):
        return None
    if status == 2 and 1 <= index <= len(oids):
        kept = [oid for number, oid in enumerate(oids, start=1) if number != index]
        return snmp_get(host, kept, community, port, timeout)
    if status == 1 and len(oids) > 1:
        middle = len(oids) // 2
        left = snmp_get(host, oids[:middle], community, port, timeout)
        right = snmp_get(host, oids[middle:], community, port, timeout)
        if left is None and right is None:
            return None
        return {**(left or {}), **(right or {})}
    if status:
        return None
    return values


def _legacy(text):
    marks = [index * 10 for index in range(len(text) // 10)][1:]
    if not marks:
        return False
    return all(text[index - 2:index] == '14' for index in marks)


def _records(blob, mapping, legacy=False):
    found = {}
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 2:
        return found
    text = blob[:-1].hex()
    size = 10 if legacy else 14
    words = [text[start:start + size] for start in range(0, len(text) - (len(text) % size), size)]
    for word in words:
        key = mapping.get(word[:2])
        if not key:
            continue
        try:
            if legacy:
                whole = int(word[8:10], 16)
                if whole <= 0:
                    continue
                found[key] = round(int(word[6:8], 16) / whole * 100)
            else:
                number = int(word[-8:], 16)
                found[key] = round(number / 100) if key in PERCENTS else number
        except ValueError:
            continue
    return found


def _errors(raw):
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return set()
    text = raw.hex()
    value = int(text, 16)
    width = len(text) * 4
    return {
        name for index, name in enumerate(ERROR_BITS)
        if index < width and value >> (width - 1 - index) & 1
    }


def _text(raw):
    if isinstance(raw, bytes):
        return raw.decode('utf-8', 'replace').strip()
    return str(raw or '').strip()


def _flag(name, bad, problem):
    return {'label': name, 'value': problem if bad else 'OK', 'bad': bad}


def snapshot(values, identity=''):
    """Turn an SNMP reply into supplies, reasons, and the rows shown on the printer."""
    model = _text((values or {}).get(OID_MODEL))
    if not re.search(r'MDL:[\w\-]+', model):
        return None
    if any(name in model.lower() for name in ('mfc-8660dn', 'mfc-8860dn')):
        return None
    kind = _kind(f'{identity} {model}')
    maintenance = LASER if kind == 'laser' else INK
    maintenance_blob = values.get(OID_MAINTENANCE)
    maintenance_text = maintenance_blob[:-1].hex() if isinstance(maintenance_blob, (bytes, bytearray)) and len(maintenance_blob) > 1 else ''
    data = {}
    if _legacy(maintenance_text):
        data.update(_records(maintenance_blob, maintenance, legacy=True))
    else:
        data.update(_records(values.get(OID_COUNTERS), COUNTERS))
        data.update(_records(maintenance_blob, maintenance))
        if kind == 'laser':
            data.update(_records(values.get(OID_NEXTCARE), NEXTCARE))
    pages = data.get('pages')
    if pages is None and isinstance(values.get(OID_PAGES), int) and values[OID_PAGES] >= 0:
        pages = values[OID_PAGES]
    flags = _errors(values.get(OID_ERRORS))
    supplies = []
    for key, name in SUPPLY_NAMES.items():
        percent = data.get(key)
        if isinstance(percent, int):
            supplies.append({'name': name, 'percent': max(0, min(100, percent))})
    reasons = []
    if 'no_paper' in flags or 'input_tray_empty' in flags:
        reasons.append('Out of paper')
    elif 'low_paper' in flags:
        reasons.append('Paper low')
    if 'jammed' in flags:
        reasons.append('Paper jam')
    status = PRINTER_STATUS.get(str(values.get(OID_PRINTER) or ''), '')
    health = DEVICE_STATUS.get(str(values.get(OID_DEVICE) or ''), '')
    checks = []
    if status:
        checks.append({'label': 'Status', 'value': status, 'bad': False})
    if health:
        checks.append({'label': 'Health', 'value': health, 'bad': health in ('Warning', 'Down')})
    if isinstance(values.get(OID_ERRORS), (bytes, bytearray)):
        checks.append(_flag('Door', 'door_open' in flags, 'Open'))
        if kind == 'laser':
            checks.append(_flag('Low toner', 'low_toner' in flags, 'Low'))
            checks.append(_flag('No toner', 'no_toner' in flags, 'Empty'))
        else:
            checks.append(_flag('Low ink', 'low_toner' in flags, 'Low'))
            checks.append(_flag('No ink', 'no_toner' in flags, 'Empty'))
        checks.append(_flag('No paper', 'no_paper' in flags or 'input_tray_empty' in flags, 'Empty'))
        checks.append(_flag('Paper jam', 'jammed' in flags, 'Jam'))
    if isinstance(data.get('drum_pages'), int):
        checks.append({'label': 'Drum pages', 'value': f"{data['drum_pages']:,}", 'bad': False})
    left = data.get('drum_pages_left')
    if isinstance(left, int):
        checks.append({'label': 'Drum pages left', 'value': f'{left:,}', 'bad': False})
    return {
        'state': status,
        'health': health,
        'pages': pages,
        'serial': _text(values.get(OID_SERIAL))[:40],
        'supplies': supplies,
        'reasons': reasons,
        'checks': checks,
    }


def readings(host, identity=''):
    community = os.getenv('SNMP_COMMUNITY', 'public').strip() or 'public'
    host = str(host or '').strip()
    if not host:
        return None
    try:
        port = int(os.getenv('SNMP_PORT', '161'))
    except ValueError:
        port = 161
    values = snmp_get(host, list(ASK), community, port)
    if not values:
        return None
    return snapshot(values, identity)
