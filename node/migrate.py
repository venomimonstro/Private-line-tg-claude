"""Export an existing node over its loopback API, preserving all VLESS IDs."""
import argparse
import copy
import json
from pathlib import Path
import socket
import ssl
import subprocess
import urllib.parse
import urllib.request

try:
    from .agent import atomic_json, normalize_user, runtime_config
except ImportError:
    from agent import atomic_json, normalize_user, runtime_config


def build_state(export, auth, control_ip, host):
    rows = [i for i in export['config'].get('inbounds', [])
            if i.get('protocol') == 'vless' and
            i.get('streamSettings', {}).get('security') == 'reality']
    if len(rows) != 1:
        raise ValueError('Expected exactly one VLESS Reality inbound')
    inbound = copy.deepcopy(rows[0])
    tag = inbound['tag']
    reality = inbound['streamSettings']['realitySettings']
    if not reality.get('privateKey') or not reality.get('shortIds'):
        raise ValueError('Refusing to rotate missing Reality credentials')
    if len(reality.get('serverNames', [])) != 1:
        raise ValueError('Expected one unambiguous Reality SNI')
    sni = reality['serverNames'][0]
    inbound.update(port=443, listen='0.0.0.0', settings={'clients': [], 'decryption': 'none'},
                   sniffing={'enabled': False})
    reality.update(target=sni + ':443', minClientVer='', maxClientVer='', maxTimeDiff=0)
    reality.pop('dest', None)
    inbound['streamSettings'] = {'network': 'tcp', 'security': 'reality',
                                 'realitySettings': reality}
    config = {
        'log': {'loglevel': 'warning'},
        'api': {'tag': 'api', 'services': ['HandlerService', 'StatsService']},
        'stats': {},
        'policy': {'levels': {'0': {'statsUserUplink': True, 'statsUserDownlink': True}}},
        'inbounds': [inbound, {'tag': 'api-in', 'listen': '127.0.0.1', 'port': 10085,
            'protocol': 'dokodemo-door', 'settings': {'address': '127.0.0.1'}}],
        'outbounds': [{'tag': 'direct', 'protocol': 'freedom'},
                      {'tag': 'block', 'protocol': 'blackhole'}],
        'routing': {'domainStrategy': 'AsIs', 'rules': [
            {'type': 'field', 'inboundTag': ['api-in'], 'outboundTag': 'api'},
            {'type': 'field', 'ip': ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
              '127.0.0.0/8', '169.254.0.0/16', '::1/128', 'fc00::/7', 'fe80::/10'],
             'outboundTag': 'block'}]},
    }
    users = {}
    ids = set()
    for original in export['users']:
        # Refuse unsupported accounts instead of silently dropping paid users.
        if not original.get('proxies', {}).get('vless', {}).get('id'):
            raise ValueError('Existing user has no UUID; refusing to rotate credentials')
        user = normalize_user(original, tag)
        uid = user['proxies']['vless']['id']
        if user['username'] in users or uid in ids:
            raise ValueError('Duplicate user or UUID in export')
        users[user['username']] = user
        ids.add(uid)
    return {'schema': 1, 'backend': 'private-line-xray', 'control_ip': control_ip,
            'auth': auth, 'config': config, 'users': users,
            'hosts': {tag: [{'address': host, 'port': 443, 'sni': sni,
                'remark': 'Private Line VPN', 'security': 'inbound_default',
                'fingerprint': 'chrome', 'is_disabled': False}]}}


def export_panel(auth):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path, data=None, token=None):
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        req = urllib.request.Request('http://127.0.0.1:2096' + path, data=data, headers=headers)
        with opener.open(req, timeout=15) as response:
            return json.load(response)
    token = request('/api/admin/token', urllib.parse.urlencode(auth).encode())['access_token']
    config = request('/api/core/config', token=token)
    users = []
    names = set()
    for _ in range(10000):
        page = request('/api/users?limit=1000&offset=' + str(len(users)), token=token)['users']
        if not isinstance(page, list):
            raise ValueError('Invalid users response')
        if not page:
            return {'config': config, 'users': users}
        if any(u['username'] in names for u in page):
            raise ValueError('Pagination did not advance')
        users.extend(page)
        names.update(u['username'] for u in page)
    raise ValueError('Export exceeded pagination bound')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--credentials', required=True)
    p.add_argument('--directory', required=True)
    p.add_argument('--binary', required=True)
    args = p.parse_args()
    creds = json.loads(Path(args.credentials).read_text())
    directory = Path(args.directory)
    exported = export_panel(creds['auth'])
    atomic_json(directory / 'marzban-export.json', exported)
    state = build_state(exported, creds['auth'], creds['control_ip'], creds['host'])
    # Verify the exact preserved SNI/target before stopping the old service.
    sni = state['config']['inbounds'][0]['streamSettings']['realitySettings']['serverNames'][0]
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.set_alpn_protocols(['h2'])
    with socket.create_connection((sni, 443), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=sni) as tls:
            if tls.selected_alpn_protocol() != 'h2':
                raise ValueError('Reality target does not support HTTP/2')
    candidate = directory / 'candidate.json'
    atomic_json(candidate, runtime_config(state))
    result = subprocess.run([args.binary, 'run', '-test', '-config', str(candidate)],
                            capture_output=True, timeout=20)
    if result.returncode:
        raise ValueError('Xray rejected migrated configuration (old node untouched)')
    atomic_json(directory / 'state.json', state)
    print('Migration prepared: %d users, original UUIDs and Reality keys preserved' % len(state['users']))


if __name__ == '__main__':
    main()
