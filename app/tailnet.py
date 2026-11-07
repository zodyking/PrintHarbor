"""One Tailscale device per printer.

Each printer runs its own userspace tailscaled. Traffic for that printer
leaves through that device. The auth key is used once and is not stored.
"""
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import store

HOSTNAME_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$')
QUEUE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,40}$')
_LOCK = threading.Lock()
_PROCS = {}
_FORWARDS = {}
_HOSTS = {}
_PENDING = {}


class TailscaleError(Exception):
    pass


def _queue_name(queue):
    if not QUEUE_RE.fullmatch(queue or ''):
        raise TailscaleError('Invalid printer')
    return queue


def _dir(queue):
    return store.ROOT / 'tailscale' / _queue_name(queue)


def socket_for(queue):
    return str(_dir(queue) / 'tailscaled.sock')


def state_for(queue):
    return str(_dir(queue) / 'tailscaled.state')


def hostname_for(label, queue):
    raw = re.sub(r'[^A-Za-z0-9]+', '-', str(label or queue or 'printer')).strip('-')
    raw = re.sub(r'-{2,}', '-', raw)[:63].strip('-').lower()
    if not HOSTNAME_RE.fullmatch(raw or ''):
        raw = 'printer'
    return raw


def _installed():
    return shutil.which('tailscale') is not None and shutil.which('tailscaled') is not None


def _auth_key(auth_key):
    key = ''.join((auth_key or '').split())
    if not key.startswith('tskey-auth-') or not 20 <= len(key) <= 300:
        raise TailscaleError('Paste a Tailscale auth key. It starts with tskey-auth-.')
    return key


def _hostname(hostname, label, queue):
    host = (hostname or '').strip() or hostname_for(label, queue)
    if not HOSTNAME_RE.fullmatch(host):
        raise TailscaleError('Hostname must use letters, numbers, and hyphens.')
    return host


def _scrub(text, key):
    return ' '.join((text or '').replace(key or '', 'auth-key').split())[:180]


def _run(args):
    try:
        return subprocess.run(args, check=False, text=True, capture_output=True, timeout=40)
    except FileNotFoundError as exc:
        raise TailscaleError('Tailscale is not installed in this container. Redeploy the current image.') from exc
    except subprocess.TimeoutExpired as exc:
        raise TailscaleError('Tailscale took too long to answer.') from exc


def _port(queue, salt, base):
    digest = int(hashlib.sha256(f'{salt}:{queue}'.encode()).hexdigest()[:6], 16)
    return base + (digest % 1800)


def _socks_port(queue):
    try:
        port = int((_dir(queue) / 'socks.port').read_text().strip())
    except (OSError, ValueError):
        return 0
    if 1024 <= port <= 65535:
        return port
    return 0


def assign_proxy(queue, port):
    """Record the SOCKS port for a printer device. Tests use this directly."""
    folder = _dir(queue)
    folder.mkdir(parents=True, exist_ok=True)
    port = int(port)
    (folder / 'socks.port').write_text(str(port))
    return port


def _socket_ready(path):
    if not path.exists() or not hasattr(socket, 'AF_UNIX'):
        return False
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(0.4)
    try:
        client.connect(str(path))
    except OSError:
        return False
    finally:
        client.close()
    return True


def _free_port(preferred):
    for offset in range(20):
        port = preferred + offset
        probe = socket.socket()
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            continue
        finally:
            probe.close()
        return port
    raise TailscaleError('No local port was free for this printer')


def _log_tail(queue):
    try:
        text = (_dir(queue) / 'tailscaled.log').read_text(errors='replace')
    except OSError:
        return ''
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return ' '.join(lines[-2:])[:180]


def _start_daemon(queue):
    queue = _queue_name(queue)
    folder = _dir(queue)
    sock_path = folder / 'tailscaled.sock'
    if _socket_ready(sock_path):
        return
    if not _installed():
        raise TailscaleError('Tailscale is not installed in this container. Redeploy the current image.')
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    if sock_path.exists():
        try:
            sock_path.unlink()
        except OSError:
            pass
    socks = _free_port(_port(queue, 'socks', 41000))
    (folder / 'socks.port').write_text(str(socks))
    log_handle = open(folder / 'tailscaled.log', 'ab')
    try:
        proc = subprocess.Popen(
            [
                'tailscaled',
                '--tun=userspace-networking',
                f'--state={folder / "tailscaled.state"}',
                f'--socket={sock_path}',
                f'--socks5-server=127.0.0.1:{socks}',
            ],
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
        )
    except OSError as exc:
        log_handle.close()
        raise TailscaleError('Tailscale is not installed in this container. Redeploy the current image.') from exc
    (folder / 'tailscaled.pid').write_text(str(proc.pid))
    with _LOCK:
        _PROCS[queue] = {'proc': proc, 'log': log_handle}
    deadline = time.time() + 8
    while time.time() < deadline:
        if proc.poll() is not None:
            detail = _log_tail(queue) or 'The Tailscale device stopped while starting'
            _stop_daemon(queue)
            raise TailscaleError(detail)
        if _socket_ready(sock_path):
            return
        time.sleep(0.15)
    _stop_daemon(queue)
    raise TailscaleError(_log_tail(queue) or 'The Tailscale device did not start')


def _stop_daemon(queue):
    with _LOCK:
        started = _PROCS.pop(queue, None)
    proc = (started or {}).get('proc')
    log_handle = (started or {}).get('log')
    pid = proc.pid if proc else 0
    if not pid:
        try:
            pid = int((_dir(queue) / 'tailscaled.pid').read_text().strip())
        except (OSError, ValueError):
            pid = 0
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    elif pid > 1:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if log_handle:
        try:
            log_handle.close()
        except OSError:
            pass


def _exact(sock, count):
    data = b''
    while len(data) < count:
        piece = sock.recv(count - len(data))
        if not piece:
            raise TailscaleError('The tailnet proxy closed the connection')
        data += piece
    return data


def socks_connect(proxy_port, host, port, timeout):
    """Open a TCP connection through one printer's SOCKS5 proxy."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(('127.0.0.1', int(proxy_port)))
        sock.sendall(b'\x05\x01\x00')
        if _exact(sock, 2) != b'\x05\x00':
            raise TailscaleError('The tailnet proxy refused the connection')
        try:
            address = socket.inet_aton(host)
            request = b'\x05\x01\x00\x01' + address + int(port).to_bytes(2, 'big')
        except OSError:
            name = str(host).encode('idna')
            if not name or len(name) > 255:
                raise TailscaleError('That address is too long')
            request = b'\x05\x01\x00\x03' + bytes([len(name)]) + name + int(port).to_bytes(2, 'big')
        sock.sendall(request)
        head = _exact(sock, 4)
        if head[0] != 5 or head[1] != 0:
            raise TailscaleError('The printer was not reachable through its tailnet device')
        if head[3] == 1:
            _exact(sock, 6)
        elif head[3] == 4:
            _exact(sock, 18)
        elif head[3] == 3:
            length = _exact(sock, 1)[0]
            _exact(sock, length + 2)
        else:
            raise TailscaleError('The tailnet proxy sent an unexpected reply')
        return sock
    except Exception:
        sock.close()
        raise


def _queue_for_host(host):
    host = str(host or '').strip()
    with _LOCK:
        bound = _HOSTS.get(host) or ''
    if bound:
        return bound
    try:
        with store.connection() as db:
            row = db.execute(
                "SELECT queue FROM printers WHERE host=? AND tail_hostname<>''",
                (host,),
            ).fetchone()
    except Exception:
        return ''
    return row['queue'] if row else ''


def bind_host(queue, host):
    queue = _queue_name(queue)
    host = str(host or '').strip()
    with _LOCK:
        for known, name in list(_HOSTS.items()):
            if name == queue and known != host:
                _HOSTS.pop(known, None)
        if host:
            _HOSTS[host] = queue


def proxy_for(host):
    """SOCKS port for the printer that owns this address, or 0 when it has no device."""
    queue = _queue_for_host(host)
    if not queue:
        return 0
    return _socks_port(queue)


def open_tcp(host, port, timeout):
    """Dial a printer through its own device when that device is running."""
    proxy = proxy_for(host)
    if proxy:
        return socks_connect(proxy, host, port, timeout)
    return socket.create_connection((host, port), timeout=timeout)


def _socks_bind(sock, atyp):
    if atyp == 1:
        raw = _exact(sock, 6)
        return socket.inet_ntoa(raw[:4]), int.from_bytes(raw[4:6], 'big')
    if atyp == 4:
        raw = _exact(sock, 18)
        return socket.inet_ntop(socket.AF_INET6, raw[:16]), int.from_bytes(raw[16:18], 'big')
    if atyp == 3:
        length = _exact(sock, 1)[0]
        raw = _exact(sock, length + 2)
        return raw[:length].decode('idna', 'replace'), int.from_bytes(raw[length:], 'big')
    raise TailscaleError('The tailnet proxy sent an unexpected reply')


def _udp_packet(host, port, payload):
    try:
        address = socket.inet_aton(str(host))
        head = b'\x00\x00\x00\x01' + address
    except OSError:
        name = str(host).encode('idna')
        if not name or len(name) > 255:
            raise TailscaleError('That address is too long')
        head = b'\x00\x00\x00\x03' + bytes([len(name)]) + name
    return head + int(port).to_bytes(2, 'big') + payload


def _udp_body(packet):
    if len(packet) < 4 or packet[2] != 0:
        return b''
    kind = packet[3]
    if kind == 1 and len(packet) >= 10:
        return packet[10:]
    if kind == 4 and len(packet) >= 22:
        return packet[22:]
    if kind == 3 and len(packet) >= 7:
        length = packet[4]
        start = 7 + length
        if len(packet) >= start:
            return packet[start:]
    return b''


def udp_exchange(host, port, payload, timeout=3):
    """One UDP datagram through this printer's device. None when the printer has no device."""
    proxy = proxy_for(host)
    if not proxy:
        return None
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.settimeout(timeout)
    udp = None
    try:
        tcp.connect(('127.0.0.1', int(proxy)))
        tcp.sendall(b'\x05\x01\x00')
        if _exact(tcp, 2) != b'\x05\x00':
            raise TailscaleError('The tailnet proxy refused the connection')
        tcp.sendall(b'\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00')
        head = _exact(tcp, 4)
        if head[0] != 5 or head[1] != 0:
            raise TailscaleError('The tailnet proxy does not forward UDP')
        bind_host, bind_port = _socks_bind(tcp, head[3])
        if bind_host in ('0.0.0.0', '::', '::0'):
            bind_host = '127.0.0.1'
        family = socket.AF_INET6 if ':' in bind_host else socket.AF_INET
        udp = socket.socket(family, socket.SOCK_DGRAM)
        udp.settimeout(timeout)
        udp.sendto(_udp_packet(host, port, payload), (bind_host, bind_port))
        packet, _address = udp.recvfrom(65535)
        return _udp_body(packet)
    finally:
        if udp is not None:
            udp.close()
        tcp.close()


def notice(printers, status):
    """Sidebar line for Tailscale. A missing device names that printer."""
    if status.get('online'):
        return {
            'kind': 'online',
            'title': status.get('hostname') or 'Tailscale',
            'detail': status.get('ip') or status.get('state') or 'Connected',
        }
    missing = []
    for printer in printers or []:
        item = printer.get('tail') or {}
        if item.get('online') or item.get('hostname'):
            continue
        missing.append(printer.get('label') or 'A printer')
    if not missing:
        return None
    if len(missing) == 1:
        detail = 'This printer has no Tailscale device'
    else:
        detail = f'{len(missing)} printers have no Tailscale device'
    return {'kind': 'unset', 'title': f'{missing[0]} is not set up', 'detail': detail}


def _bridge(left, right):
    def copy(src, dst):
        try:
            while True:
                chunk = src.recv(16384)
                if not chunk:
                    break
                dst.sendall(chunk)
        except OSError:
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    left_to_right = threading.Thread(target=copy, args=(left, right), daemon=True)
    right_to_left = threading.Thread(target=copy, args=(right, left), daemon=True)
    left_to_right.start()
    right_to_left.start()
    left_to_right.join(timeout=120)
    right_to_left.join(timeout=1)


class _Forward(threading.Thread):
    def __init__(self, host, port, proxy):
        super().__init__(daemon=True, name=f'tail-forward-{host}')
        self.host = host
        self.port = int(port)
        self.proxy = int(proxy)
        self.listen = 0
        self.stop = threading.Event()
        self.server = None

    def matches(self, host, port, proxy):
        return self.host == host and self.port == int(port) and self.proxy == int(proxy) and not self.stop.is_set()

    def start_listening(self, preferred):
        last_error = None
        for offset in range(20):
            server = socket.socket()
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                server.bind(('127.0.0.1', preferred + offset))
            except OSError as exc:
                server.close()
                last_error = exc
                continue
            server.listen(16)
            server.settimeout(0.4)
            self.server = server
            self.listen = server.getsockname()[1]
            self.start()
            return self.listen
        raise TailscaleError('No local port was free for this printer') from last_error

    def run(self):
        while not self.stop.is_set():
            try:
                client, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        remote = None
        try:
            remote = socks_connect(self.proxy, self.host, self.port, timeout=8)
            _bridge(client, remote)
        except Exception:
            pass
        finally:
            for item in (client, remote):
                if item is None:
                    continue
                try:
                    item.close()
                except OSError:
                    pass

    def close(self):
        self.stop.set()
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass


def _stop_forward(queue):
    with _LOCK:
        forward = _FORWARDS.pop(queue, None)
    if forward:
        forward.close()


def local_forward(queue, host, port):
    """Local TCP port that CUPS can use. Zero when this printer has no device."""
    try:
        queue = _queue_name(queue)
    except TailscaleError:
        return 0
    proxy = _socks_port(queue)
    if not proxy:
        return 0
    host = str(host or '').strip()
    with _LOCK:
        current = _FORWARDS.get(queue)
    if current and current.is_alive() and current.matches(host, port, proxy):
        return current.listen
    _stop_forward(queue)
    forward = _Forward(host, port, proxy)
    try:
        listen = forward.start_listening(_port(queue, 'forward', 44000))
    except TailscaleError:
        return 0
    with _LOCK:
        _FORWARDS[queue] = forward
    return listen


def has_node(queue):
    try:
        folder = _dir(queue)
    except TailscaleError:
        return False
    return (folder / 'tailscaled.state').exists() or bool(_socks_port(queue))


def _read_node(queue):
    path = Path(socket_for(queue))
    if not hasattr(socket, 'AF_UNIX') or not path.exists():
        raise TailscaleError('The printer device is not running')

    class UnixConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(2)
            self.sock.connect(str(path))

    conn = UnixConnection('local-tailscaled', timeout=2)
    try:
        conn.request('GET', '/localapi/v0/status')
        response = conn.getresponse()
        if response.status != 200:
            raise TailscaleError('The printer device did not answer')
        data = json.loads(response.read())
    finally:
        conn.close()
    node = data.get('Self') or {}
    ips = [item for item in (node.get('TailscaleIPs') or []) if ':' not in item]
    if not ips:
        ips = list(node.get('TailscaleIPs') or [])
    running = data.get('BackendState') == 'Running'
    return {
        'online': running,
        'hostname': (node.get('HostName') or '').strip(),
        'ip': ips[0] if ips else '',
        'state': data.get('BackendState') or 'Unknown',
        'detail': '' if running else 'Paste an auth key to join this printer.',
    }


def _write_columns(queue, info):
    try:
        with store.connection() as db:
            row = db.execute('SELECT 1 FROM printers WHERE queue=?', (queue,)).fetchone()
            if not row:
                return
            db.execute(
                'UPDATE printers SET tail_hostname=?, tail_ip=? WHERE queue=?',
                (info.get('hostname') or '', info.get('ip') or '', queue),
            )
    except Exception:
        return


def _clear_columns(queue):
    try:
        with store.connection() as db:
            db.execute(
                "UPDATE printers SET tail_hostname='', tail_ip='' WHERE queue=?",
                (queue,),
            )
    except Exception:
        return


def _wipe(queue):
    _stop_forward(queue)
    _stop_daemon(queue)
    shutil.rmtree(_dir(queue), ignore_errors=True)


def connect(queue, auth_key, hostname='', label=''):
    queue = _queue_name(queue)
    key = _auth_key(auth_key)
    host = _hostname(hostname, label, queue)
    if not _installed():
        raise TailscaleError('Tailscale is not installed in this container. Redeploy the current image.')
    fresh = not Path(state_for(queue)).exists()
    try:
        _start_daemon(queue)
        result = _run([
            'tailscale', '--socket', socket_for(queue), 'up',
            '--auth-key=' + key,
            '--hostname=' + host,
            '--accept-routes',
            '--reset',
            '--timeout=20s',
        ])
    except Exception:
        if fresh:
            _wipe(queue)
        raise
    if result.returncode != 0:
        if fresh:
            _wipe(queue)
        raise TailscaleError(_scrub(result.stderr or result.stdout or 'Tailscale did not connect', key))
    try:
        info = _read_node(queue)
    except TailscaleError:
        info = {'online': False, 'hostname': host, 'ip': '', 'state': 'Joined', 'detail': ''}
    if not info.get('hostname'):
        info['hostname'] = host
    with _LOCK:
        _PENDING[queue] = info
    _write_columns(queue, info)
    _forget_status()
    return info


def disconnect(queue):
    queue = _queue_name(queue)
    sock = Path(socket_for(queue))
    try:
        if _socket_ready(sock) and shutil.which('tailscale'):
            _run(['tailscale', '--socket', str(sock), 'logout'])
    except TailscaleError:
        pass
    _wipe(queue)
    _clear_columns(queue)
    with _LOCK:
        _PENDING.pop(queue, None)
        for host, name in list(_HOSTS.items()):
            if name == queue:
                _HOSTS.pop(host, None)
    _forget_status()


def resume():
    """Start the device for each printer that already joined."""
    if not _installed():
        return
    try:
        with store.connection() as db:
            rows = db.execute(
                "SELECT queue, host FROM printers WHERE tail_hostname<>''"
            ).fetchall()
    except Exception:
        return
    for row in rows:
        if not Path(state_for(row['queue'])).exists():
            continue
        try:
            _start_daemon(row['queue'])
            bind_host(row['queue'], row['host'])
        except TailscaleError:
            continue


def stop_all():
    """Stop devices started in this process. State stays for the next launch."""
    with _LOCK:
        forwards = list(_FORWARDS)
        queues = list(_PROCS)
    for queue in forwards:
        _stop_forward(queue)
    for queue in queues:
        _stop_daemon(queue)


def device(queue):
    stored = {'hostname': '', 'ip': ''}
    try:
        queue = _queue_name(queue)
    except TailscaleError:
        return {
            'queue': '', 'online': False, 'hostname': '', 'ip': '',
            'state': 'Not joined', 'detail': '',
        }
    try:
        with store.connection() as db:
            row = db.execute(
                'SELECT tail_hostname, tail_ip FROM printers WHERE queue=?',
                (queue,),
            ).fetchone()
        if row:
            stored = {'hostname': row['tail_hostname'] or '', 'ip': row['tail_ip'] or ''}
    except Exception:
        pass
    blank = {
        'queue': queue,
        'online': False,
        'hostname': stored['hostname'],
        'ip': stored['ip'],
        'state': 'Not joined' if not stored['hostname'] else 'Offline',
        'detail': '',
    }
    if not _installed() or not _socket_ready(Path(socket_for(queue))):
        return blank
    try:
        info = _read_node(queue)
    except Exception:
        return blank
    if not info.get('hostname'):
        info['hostname'] = stored['hostname']
    if info.get('online'):
        _write_columns(queue, info)
    info['queue'] = queue
    return info


_STATUS_CACHE = {'at': 0.0, 'value': None}

def _forget_status():
    with _LOCK:
        _STATUS_CACHE['value'] = None
        _STATUS_CACHE['at'] = 0.0

def status():
    now = time.monotonic()
    with _LOCK:
        cached = _STATUS_CACHE['value']
        if cached is not None and now - _STATUS_CACHE['at'] < 8:
            return dict(cached)
    value = _collect_status()
    with _LOCK:
        _STATUS_CACHE['at'] = time.monotonic()
        _STATUS_CACHE['value'] = value
    return dict(value)

def _collect_status():
    devices = []
    try:
        with store.connection() as db:
            rows = db.execute('SELECT queue FROM printers ORDER BY label').fetchall()
        for row in rows:
            devices.append(device(row['queue']))
    except Exception:
        devices = []
    online = [item for item in devices if item.get('online')]
    installed = _installed()
    if not installed:
        state = 'Not installed'
        detail = 'Tailscale is not installed in this container. Redeploy the current image.'
    elif not devices:
        state = 'No devices'
        detail = 'Each printer joins your tailnet as its own device. Add a printer to begin.'
    elif not online:
        state = 'Not connected'
        detail = 'No printer device is signed in.'
    elif len(online) == 1:
        state = 'Connected'
        detail = '1 printer device on the tailnet.'
    else:
        state = 'Connected'
        detail = f'{len(online)} printer devices on the tailnet.'
    if len(online) == 1:
        ip = online[0].get('ip') or ''
        hostname = online[0].get('hostname') or ''
    elif online:
        ip = f'{len(online)} devices'
        hostname = ''
    else:
        ip = ''
        hostname = ''
    return {
        'installed': installed,
        'tun': True,
        'online': bool(online),
        'state': state,
        'hostname': hostname,
        'ip': ip,
        'detail': detail,
        'devices': devices,
    }
