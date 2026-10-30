"""Read status from the bundled Tailscale daemon Unix socket."""
import http.client
import json
import socket
from pathlib import Path

SOCKET='/var/run/tailscale/tailscaled.sock'

class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(2)
        self.sock.connect(SOCKET)

def status():
    if not Path(SOCKET).exists():
        return {'state':'Unavailable','ip':'','online':False}
    try:
        conn=UnixConnection('local-tailscaled.sock',timeout=2)
        conn.request('GET','/localapi/v0/status')
        resp=conn.getresponse()
        if resp.status!=200:raise OSError('LocalAPI response: '+str(resp.status))
        data=json.loads(resp.read())
        conn.close()
        node=data.get('Self') or {}
        ips=node.get('TailscaleIPs') or []
        running=data.get('BackendState')=='Running'
        return {'state':data.get('BackendState','Unknown'),'ip':ips[0] if ips else '', 'online':running}
    except Exception:
        return {'state':'Unavailable','ip':'','online':False}
