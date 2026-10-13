"""Standalone Xray supervisor and durable, synchronous node API (stdlib only).

The REST dialect remains compatible with the control plane's MarzbanClient;
no Marzban code, panel, database or background mutation queue is used here.
"""
from __future__ import annotations
import argparse
import copy
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        os.chmod(tmp, 0o600)
        json.dump(value, f, separators=(',', ':'))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def normalize_user(body, tag, old=None):
    user = copy.deepcopy(old or {})
    user.update(body)
    name = user.get('username', '')
    if not re.fullmatch(r'[A-Za-z0-9_]{3,32}', name):
        raise APIError(422, 'Invalid username')
    proxy = user.get('proxies', {}).get('vless', {})
    try:
        uid = str(UUID(proxy.get('id') or (str(uuid4()) if old is None else '')))
    except (ValueError, TypeError, AttributeError):
        raise APIError(422, 'Invalid VLESS UUID')
    if user.get('status', 'active') not in ('active', 'disabled', 'expired', 'limited'):
        raise APIError(422, 'Unsupported status; resolve on-hold users before migration')
    if set(user.get('proxies', {'vless': {}})) - {'vless'}:
        raise APIError(422, 'Only VLESS is supported')
    if user.get('inbounds', {'vless': [tag]}) != {'vless': [tag]}:
        raise APIError(422, 'User must use the canonical VLESS inbound')
    user.update(username=name, proxies={'vless': {'id': uid, 'flow': 'xtls-rprx-vision'}},
                inbounds={'vless': [tag]}, links=[], subscription_url='')
    for field in ('data_limit', 'expire', 'used_traffic'):
        value = user.get(field) or 0
        if not isinstance(value, int) or value < 0:
            raise APIError(422, 'Invalid ' + field)
        user[field] = value
    user.setdefault('status', 'active')
    # Accounting is never supplied by a mutation caller.
    if old is not None:
        user['used_traffic'] = old['used_traffic']
    if user['status'] == 'active':
        if user['expire'] and user['expire'] <= time.time():
            user['status'] = 'expired'
        elif user['data_limit'] and user['used_traffic'] >= user['data_limit']:
            user['status'] = 'limited'
    return user


def runtime_config(state):
    config = copy.deepcopy(state['config'])
    inbound = config['inbounds'][0]
    inbound['settings']['clients'] = [
        {'id': u['proxies']['vless']['id'], 'email': u['username'],
         'flow': 'xtls-rprx-vision', 'level': 0}
        for u in state['users'].values() if u['status'] == 'active'
    ]
    return config


class Node:
    def __init__(self, directory, binary):
        self.directory = Path(directory)
        self.binary = str(binary)
        self.path = self.directory / 'state.json'
        self.state = json.loads(self.path.read_text())
        self.tag = self.state['config']['inbounds'][0]['tag']
        self.api_address = '127.0.0.1:' + str(self.state['config']['inbounds'][1]['port'])
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(48)
        self.child = None
        self.counters = {}
        self.stopping = False

    def save(self):
        atomic_json(self.path, self.state)

    def command(self, *args, data=None):
        result = subprocess.run([self.binary, *args], input=data, text=True,
                                capture_output=True, timeout=8)
        if result.returncode:
            # Xray errors can contain UUIDs/config; never expose them via HTTP/logs.
            raise RuntimeError('Xray command failed: ' + args[0])
        return result.stdout

    def api(self, action, *args, data=None):
        return self.command('api', action, '--server=' + self.api_address, '--timeout=3',
                            *args, data=data)

    def live(self):
        reply = json.loads(self.api('inbounduser', '-tag=' + self.tag))
        rows = reply.get('users', [])
        if not isinstance(rows, list):
            raise RuntimeError('Invalid Xray user list')
        return {u['email']: u for u in rows}

    def stop_core(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait()
        self.child = None

    def start_core(self):
        self.stop_core()
        # Expiry is applied before every restart, including reboot recovery.
        self.state['users'] = {n: normalize_user(u, self.tag, u)
                               for n, u in self.state['users'].items()}
        self.save()
        config = self.directory / 'runtime.json'
        atomic_json(config, runtime_config(self.state))
        self.command('run', '-test', '-config', str(config))
        self.counters = {}
        with open(self.directory / 'core.log', 'a') as log:
            self.child = subprocess.Popen([self.binary, 'run', '-config', str(config)],
                                          stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        expected = {n for n, u in self.state['users'].items() if u['status'] == 'active'}
        for _ in range(30):
            if self.child.poll() is not None:
                break
            try:
                members = self.live()
                if set(members) == expected and all(
                    self.matches(members[n], self.state['users'][n]) for n in expected
                ):
                    return
            except Exception:
                pass
            time.sleep(.1)
        self.stop_core()
        raise RuntimeError('Xray startup/user verification failed')

    @staticmethod
    def matches(live, user):
        account = live.get('account', {})
        return (account.get('id') == user['proxies']['vless']['id']
                and account.get('flow') == 'xtls-rprx-vision')

    def apply_user(self, name, old, new):
        before = old and old['status'] == 'active'
        after = new and new['status'] == 'active'
        same = before and after and old['proxies'] == new['proxies']
        members = self.live()
        same = same and name in members and self.matches(members[name], new)
        if name in members and (not after or not same):
            output = self.api('rmu', '-tag=' + self.tag, name)
            # Xray CLI returns exit=0 even when individual operations fail.
            if 'Removed 1 user(s) in total.' not in output:
                raise RuntimeError('Xray removal was not acknowledged')
        if after and (name not in members or not same):
            user = {'id': new['proxies']['vless']['id'], 'email': name,
                    'flow': 'xtls-rprx-vision', 'level': 0}
            inbound = copy.deepcopy(self.state['config']['inbounds'][0])
            inbound['settings']['clients'] = [user]
            payload = {'inbounds': [inbound]}
            output = self.api('adu', 'stdin:', data=json.dumps(payload))
            if 'Added 1 user(s) in total.' not in output:
                raise RuntimeError('Xray addition was not acknowledged')
        verified = self.live()
        if ((name in verified) != bool(after)
                or (after and not self.matches(verified[name], new))):
            raise RuntimeError('Xray live membership differs from durable state')

    def mutate(self, name, new):
        old = self.state['users'].get(name)
        if new is not None:
            uid = new['proxies']['vless']['id']
            if any(n != name and u['proxies']['vless']['id'] == uid
                   for n, u in self.state['users'].items()):
                raise APIError(409, 'UUID belongs to another user')
        # Desired state is durable FIRST. A crash between save and hot-add is
        # recovered by start_core. An ambiguous HTTP response is safe to retry.
        if new is None:
            self.state['users'].pop(name, None)
        else:
            self.state['users'][name] = new
        try:
            self.save()
        except Exception:
            if old is None:
                self.state['users'].pop(name, None)
            else:
                self.state['users'][name] = old
            raise
        try:
            self.apply_user(name, old, new)
        except Exception:
            # Only an exceptional mutation failure permits a bounded restart.
            # A successful response still requires verified live membership.
            self.start_core()
        return new

    def accounting(self):
        data = json.loads(self.api('statsquery', '-pattern=user>>>'))
        changed = False
        for entry in data.get('stat', []):
            parts = entry['name'].split('>>>')
            if len(parts) != 4 or parts[2] != 'traffic':
                continue
            name = parts[1]
            count = int(entry.get('value', 0))
            previous = self.counters.get(entry['name'], 0)
            delta = max(0, count - previous)
            self.counters[entry['name']] = count
            if name in self.state['users'] and delta:
                self.state['users'][name]['used_traffic'] += delta
                changed = True
        if changed:
            self.save()
        for name, old in list(self.state['users'].items()):
            new = normalize_user(old, self.tag, old)
            if new['status'] != old['status']:
                self.mutate(name, new)

    def tick(self):
        while not self.stopping:
            time.sleep(2)
            try:
                with self.lock:
                    if self.stopping:
                        return
                    if self.child is None or self.child.poll() is not None:
                        self.start_core()
                    self.accounting()
                    logfile = self.directory / 'core.log'
                    if logfile.exists() and logfile.stat().st_size > 2 * 1024 * 1024:
                        with open(logfile, 'w'):
                            pass
            except Exception as exc:
                print('Node maintenance failed: ' + type(exc).__name__, flush=True)
                # Fail closed on broken accounting/API: systemd restarts both
                # processes from durable state; never serve unmetered forever.
                with self.lock:
                    self.stop_core()
                os._exit(1)

    def dispatch(self, method, target, headers, raw, source):
        if source not in ('127.0.0.1', self.state['control_ip']):
            raise APIError(403, 'Management source denied')
        parsed = urlsplit(target)
        path = parsed.path
        if method == 'POST' and path == '/api/admin/token':
            fields = parse_qs(raw.decode())
            auth = self.state['auth']
            if not (hmac.compare_digest(fields.get('username', [''])[0], auth['username'])
                    and hmac.compare_digest(fields.get('password', [''])[0], auth['password'])):
                raise APIError(401, 'Invalid credentials')
            return {'access_token': self.token, 'token_type': 'bearer'}
        if not hmac.compare_digest(headers.get('Authorization', ''), 'Bearer ' + self.token):
            raise APIError(401, 'Authentication required')
        with self.lock:
            if path == '/api/core' and method == 'GET':
                self.live()
                return {'started': self.child is not None and self.child.poll() is None,
                        'version': '26.6.27', 'backend': 'private-line-xray'}
            if path == '/api/core/restart' and method == 'POST':
                self.accounting()
                self.start_core()
                return {'started': True}
            if path == '/api/core/config' and method == 'GET':
                return self.state['config']
            if path == '/api/inbounds' and method == 'GET':
                return {'vless': [{'tag': self.tag, 'protocol': 'vless', 'port': 443}]}
            if path == '/api/hosts':
                if method == 'PUT':
                    body = json.loads(raw)
                    if not isinstance(body, dict) or self.tag not in body:
                        raise APIError(422, 'Invalid hosts')
                    self.state['hosts'] = body
                    self.save()
                elif method != 'GET':
                    raise APIError(405, 'Method not allowed')
                return self.state['hosts']
            if path == '/api/users' and method == 'GET':
                query = parse_qs(parsed.query)
                offset = max(0, int(query.get('offset', ['0'])[0]))
                limit = min(1000, max(1, int(query.get('limit', ['1000'])[0])))
                users = sorted(self.state['users'].values(), key=lambda u: u['username'])
                return {'users': users[offset:offset + limit], 'total': len(users)}
            if path == '/api/user' and method == 'POST':
                new = normalize_user(json.loads(raw), self.tag)
                name = new['username']
                if name in self.state['users']:
                    raise APIError(409, 'User already exists')
                return self.mutate(name, new)
            if path.startswith('/api/user/'):
                name = path.removeprefix('/api/user/')
                old = self.state['users'].get(name)
                if old is None:
                    raise APIError(404, 'User not found')
                if method == 'GET':
                    return old
                if method == 'DELETE':
                    self.mutate(name, None)
                    return {}
                if method == 'PUT':
                    body = json.loads(raw)
                    if body.get('username', name) != name:
                        raise APIError(422, 'Cannot rename user')
                    return self.mutate(name, normalize_user(body, self.tag, old))
            raise APIError(404, 'Unknown endpoint')


def serve(node, host='0.0.0.0', port=2096):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass  # URLs and bodies may identify users.

        def handle_request(self):
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 <= size <= 1024 * 1024:
                    raise APIError(413, 'Request too large')
                raw = self.rfile.read(size)
                with node.lock:
                    response = copy.deepcopy(node.dispatch(
                        self.command, self.path, self.headers, raw, self.client_address[0]))
                status = 200
            except APIError as exc:
                status, response = exc.status, {'detail': exc.message}
            except (ValueError, TypeError, KeyError):
                status, response = 422, {'detail': 'Invalid request'}
            except Exception as exc:
                print('Node API failed: ' + type(exc).__name__, flush=True)
                status, response = 503, {'detail': 'Xray operation failed; retry'}
            data = json.dumps(response).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = handle_request

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', default='/var/lib/private-line-node')
    parser.add_argument('--binary', default='/opt/private-line-node/xray')
    args = parser.parse_args()
    os.umask(0o077)
    node = Node(args.directory, args.binary)
    def stop(*_):
        node.stopping = True
        with node.lock:
            node.stop_core()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    node.start_core()
    threading.Thread(target=node.tick, daemon=True).start()
    try:
        serve(node).serve_forever()
    finally:
        stop()


if __name__ == '__main__':
    main()
