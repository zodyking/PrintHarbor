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

def _kind(text):
    low = (text or '').lower()
    if any(word in low for word in ('inkjet', ' ink', 'officejet', 'deskjet', 'pixma', 'ecotank', 'eco tank', 'envy')):
        return 'Ink'
    if any(word in low for word in ('toner', 'laser', 'hl-l', 'laserjet', 'mfc-l', 'dcp-l')):
        return 'Toner'
    return 'Cartridge'

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
    supply_word = kind.lower() if kind != 'Cartridge' else 'printer'
    return {
        'kind': kind,
        'name': '',
        'note': '',
        'url': amazon_search(f'{source} {supply_word} cartridge'),
    }
