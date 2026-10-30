"""Join this container to a tailnet and report the local daemon."""
import re
import shutil
import subprocess
from pathlib import Path

SOCKET = Path('/var/run/tailscale/tailscaled.sock')
HOSTNAME_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$')


class TailscaleError(Exception):
    pass


def _blank(detail):
    return {
        'state': 'Unavailable',
        'ip': '',
        'hostname': '',
        'online': False,
        'installed': shutil.which('tailscale') is not None,
        'tun': Path('/dev/net/tun').exists(),
        'detail': detail,
    }


def status():
    info = _blank('')
    if not info['installed']:
        info['detail'] = 'Tailscale is not installed in this container. Redeploy the current image.'
        return info
    if not info['tun']:
        info['detail'] = 'This container has no /dev/net/tun device, so print traffic cannot use the tailnet.'
        return info
    if not SOCKET.exists():
        info['detail'] = 'The Tailscale service is not running. Restart the container.'
        return info
    try:
        import http.client
        import json
        import socket

        class UnixConnection(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(2)
                self.sock.connect(str(SOCKET))

        conn = UnixConnection('local-tailscaled.sock', timeout=2)
        conn.request('GET', '/localapi/v0/status')
        resp = conn.getresponse()
        if resp.status != 200:
            raise OSError('LocalAPI response: ' + str(resp.status))
        data = json.loads(resp.read())
        conn.close()
        node = data.get('Self') or {}
        ips = node.get('TailscaleIPs') or []
        running = data.get('BackendState') == 'Running'
        info.update({
            'state': data.get('BackendState') or 'Unknown',
            'ip': ips[0] if ips else '',
            'hostname': (node.get('DNSName') or node.get('HostName') or '').rstrip('.'),
            'online': running,
            'detail': '' if running else 'Paste an auth key to join the tailnet.',
        })
        return info
    except Exception:
        info['detail'] = 'The Tailscale service did not answer.'
        return info


def _run(args):
    try:
        return subprocess.run(args, check=False, text=True, capture_output=True, timeout=40)
    except FileNotFoundError as exc:
        raise TailscaleError('Tailscale is not installed in this container. Redeploy the current image.') from exc
    except subprocess.TimeoutExpired as exc:
        raise TailscaleError('Tailscale took too long to answer.') from exc


def _ready():
    if shutil.which('tailscale') is None:
        raise TailscaleError('Tailscale is not installed in this container. Redeploy the current image.')
    if not Path('/dev/net/tun').exists():
        raise TailscaleError('This container cannot open /dev/net/tun. Deploy the Dokploy Compose file so the app receives that device and NET_ADMIN.')
    if not SOCKET.exists():
        raise TailscaleError('The Tailscale service is not running. Restart the container.')


def connect(auth_key, hostname):
    key = ''.join((auth_key or '').split())
    host = (hostname or 'printharbor').strip() or 'printharbor'
    if not key.startswith('tskey-auth-') or not 20 <= len(key) <= 300:
        raise TailscaleError('Paste a Tailscale auth key. It starts with tskey-auth-.')
    if not HOSTNAME_RE.fullmatch(host):
        raise TailscaleError('Hostname must use letters, numbers, and hyphens.')
    _ready()
    result = _run([
        'tailscale', '--socket', str(SOCKET), 'up',
        '--auth-key=' + key,
        '--hostname=' + host,
        '--accept-routes',
        '--reset',
        '--timeout=20s',
    ])
    if result.returncode != 0:
        text = (result.stderr or result.stdout or 'Tailscale did not connect').replace(key, 'auth-key')
        raise TailscaleError(' '.join(text.split())[:180])
    return status()


def disconnect():
    _ready()
    result = _run(['tailscale', '--socket', str(SOCKET), 'logout'])
    if result.returncode != 0:
        text = (result.stderr or result.stdout or 'Tailscale did not log out').strip()
        raise TailscaleError(' '.join(text.split())[:180])
