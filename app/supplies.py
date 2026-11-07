"""Cartridge name for a saved printer, and an Amazon search for a low-priced replacement."""
import re
from urllib.parse import quote_plus

# Brother's own list for the TN630 / TN660 family. TN660 is the high-yield cartridge.
_TN660 = (
    'DCP-L2520DW', 'DCP-L2540DW', 'HL-L2300D', 'HL-L2305W', 'HL-L2315DW', 'HL-L2320D',
    'HL-L2340DW', 'HL-L2360DW', 'HL-L2380DW', 'MFC-L2680W', 'MFC-L2685DW', 'MFC-L2700DW',
    'MFC-L2705DW', 'MFC-L2707DW', 'MFC-L2720DW', 'MFC-L2740DW',
)

_FAMILIES = (
    {
        'kind': 'Toner',
        'name': 'TN660',
        'note': 'High-yield cartridge. TN630 is the smaller one.',
        'models': _TN660,
    },
)

def _compact(text):
    return re.sub(r'[^a-z0-9]+', '', (text or '').lower())

_GENERIC = {'printer', 'unknown', 'ipp', 'everywhere', 'ippeverywhere', 'generic', 'driverless'}
_ID_KEYS = {'MFG', 'MDL', 'CID', 'DES', 'SN', 'SERN', 'CMD', 'CLS'}

def _kind(text):
    low = (text or '').lower()
    if any(word in low for word in ('inkjet', ' ink', 'officejet', 'deskjet', 'pixma', 'ecotank', 'eco tank', 'envy')):
        return 'Ink'
    if any(word in low for word in ('toner', 'laser', 'hl-l', 'laserjet', 'mfc-l', 'dcp-l')):
        return 'Toner'
    return 'Cartridge'

def _phrase(source, kind):
    """The old search, without a generic IPP name such as Printer standing in for the model."""
    words = []
    for raw in re.split(r'[\s;]+', source or ''):
        token = raw.strip(' ,')
        if not token:
            continue
        if ':' in token:
            key, _, value = token.partition(':')
            if key.strip().upper() not in _ID_KEYS or not value.strip():
                continue
            if key.strip().upper() in {'SN', 'SERN', 'CMD', 'CLS'}:
                continue
            token = value.strip()
        if _compact(token) in _GENERIC:
            continue
        words.append(token)
    phrase = ' '.join(words)
    if not phrase:
        return ''
    supply_word = kind.lower() if kind != 'Cartridge' else 'printer'
    return f'{phrase} {supply_word} cartridge'

def amazon_search(query):
    term = ' '.join(str(query or '').split())[:120] or 'printer cartridge'
    return 'https://www.amazon.com/s?k=' + quote_plus(term) + '&s=price-asc-rank'

def replacement_for(identity):
    """Name the cartridge when the model is known. Otherwise search by the printer name."""
    source = ' '.join(str(identity or '').split())
    if not source:
        return None
    compact = _compact(source)
    for family in _FAMILIES:
        for model in family['models']:
            if _compact(model) in compact:
                kind = family['kind']
                name = family['name']
                return {
                    'kind': kind,
                    'name': name,
                    'note': family.get('note') or '',
                    'url': amazon_search(f'{name} {kind.lower()} cartridge'),
                }
    kind = _kind(source)
    phrase = _phrase(source, kind)
    if not phrase:
        return None
    return {
        'kind': kind,
        'name': '',
        'note': '',
        'url': amazon_search(phrase),
    }

def from_printer(printer):
    """Match a cartridge from the model, including the id the printer still reports."""
    printer = printer or {}
    stats = printer.get('stats') or {}
    primary = ' '.join(
        str(part).strip()
        for part in (
            printer.get('identity'),
            printer.get('driver'),
            stats.get('model'),
            stats.get('device_id'),
        )
        if str(part or '').strip()
    )
    found = replacement_for(primary)
    if found:
        return found
    return replacement_for(printer.get('label') or '')
