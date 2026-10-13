"""Exercise REAL Xray hot-add, traffic, reboot restore, and hot-remove locally.

Uses isolated high loopback ports and a disposable TLS target, never production
listeners or customer UUIDs. Required on-node before switching runtimes.
"""
import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

try:
    from .agent import Node, atomic_json, normalize_user
except ImportError:
    from agent import Node, atomic_json, normalize_user


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def wait_port(number, process):
    for _ in range(50):
        if process.poll() is not None:
            raise RuntimeError('Test process exited')
        try:
            with socket.create_connection(('127.0.0.1', number), timeout=.2):
                return
        except OSError:
            time.sleep(.1)
    raise RuntimeError('Test listener unavailable')


def tunnel(socks_port, target_port, nonce):
    def read_exact(sock, count):
        result = b''
        while len(result) < count:
            chunk = sock.recv(count - len(result))
            if not chunk:
                raise OSError('Tunnel closed')
            result += chunk
        return result
    with socket.create_connection(('127.0.0.1', socks_port), timeout=4) as s:
        s.settimeout(4)
        s.sendall(b'\x05\x01\x00')
        if read_exact(s, 2) != b'\x05\x00':
            raise OSError('SOCKS authentication failed')
        s.sendall(b'\x05\x01\x00\x01\x7f\x00\x00\x01' + struct.pack('!H', target_port))
        reply = read_exact(s, 4)
        if reply[1] != 0:
            raise OSError('SOCKS connection rejected')
        if reply[3] == 1:
            read_exact(s, 6)
        elif reply[3] == 4:
            read_exact(s, 18)
        else:
            read_exact(s, read_exact(s, 1)[0] + 2)
        s.sendall(b'GET / HTTP/1.0\r\nHost: localhost\r\n\r\n')
        data = b''
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        return nonce.encode() in data


def run(binary):
    nonce = str(uuid4())
    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(nonce.encode())
        def log_message(self, *args):
            pass
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Target)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    processes = []
    node = None
    with tempfile.TemporaryDirectory(prefix='private-line-xray-test-') as directory:
        root = Path(directory)
        try:
            keys = subprocess.check_output([binary, 'x25519'], text=True)
            def key(pattern):
                match = re.search(pattern + r':\s*([A-Za-z0-9_-]+)', keys, re.I)
                if not match:
                    raise RuntimeError('Unrecognized Xray key output')
                return match.group(1)
            private = key(r'Private\s*Key')
            public = key(r'(?:Public\s*Key|Password(?: \(PublicKey\))?)')
            tls_port, vpn_port, api_port, socks_port = port(), port(), port(), port()
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                '-keyout', str(root / 'key.pem'), '-out', str(root / 'cert.pem'),
                '-days', '1', '-subj', '/CN=localhost'], check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            tls_config = {'log': {'loglevel': 'none'}, 'inbounds': [{
                'listen': '127.0.0.1', 'port': tls_port, 'protocol': 'http',
                'streamSettings': {'network': 'tcp', 'security': 'tls', 'tlsSettings': {
                    'minVersion': '1.3', 'alpn': ['h2', 'http/1.1'], 'certificates': [{
                        'certificateFile': str(root / 'cert.pem'),
                        'keyFile': str(root / 'key.pem')}]}}}],
                'outbounds': [{'protocol': 'freedom'}]}
            atomic_json(root / 'tls.json', tls_config)
            tls = subprocess.Popen([binary, 'run', '-config', str(root / 'tls.json')],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(tls)
            wait_port(tls_port, tls)
            config = {
                'log': {'loglevel': 'warning'}, 'stats': {},
                'api': {'tag': 'api', 'services': ['HandlerService', 'StatsService']},
                'policy': {'levels': {'0': {'statsUserUplink': True, 'statsUserDownlink': True}}},
                'inbounds': [
                    {'tag': 'test-vless', 'protocol': 'vless', 'listen': '127.0.0.1', 'port': vpn_port,
                     'settings': {'decryption': 'none', 'clients': []},
                     'streamSettings': {'network': 'tcp', 'security': 'reality', 'realitySettings': {
                         'target': '127.0.0.1:' + str(tls_port), 'serverNames': ['localhost'],
                         'privateKey': private, 'shortIds': ['0123456789abcdef'], 'minClientVer': ''}}},
                    {'tag': 'api-in', 'protocol': 'dokodemo-door', 'listen': '127.0.0.1', 'port': api_port,
                     'settings': {'address': '127.0.0.1'}}],
                'outbounds': [{'protocol': 'freedom', 'tag': 'direct', 'settings': {
                    'finalRules': [{'action': 'allow', 'ip': ['127.0.0.1/32']}]}}],
                'routing': {'rules': [{'type': 'field', 'inboundTag': ['api-in'], 'outboundTag': 'api'}]},
            }
            state = {'config': config, 'users': {}, 'control_ip': '127.0.0.1',
                     'auth': {'username': 'test', 'password': nonce}, 'hosts': {}}
            atomic_json(root / 'state.json', state)
            node = Node(root, binary)
            node.start_core()
            first_pid = node.child.pid
            user = normalize_user({'username': 'selftest', 'proxies': {'vless': {'id': str(uuid4())}}}, node.tag)
            node.mutate('selftest', user)
            assert node.child.pid == first_pid, 'Hot-add restarted Xray'
            client = {'log': {'loglevel': 'none'},
                'inbounds': [{'protocol': 'socks', 'listen': '127.0.0.1', 'port': socks_port,
                              'settings': {'auth': 'noauth'}}],
                'outbounds': [{'protocol': 'vless', 'settings': {'vnext': [{'address': '127.0.0.1',
                    'port': vpn_port, 'users': [{'id': user['proxies']['vless']['id'],
                    'encryption': 'none', 'flow': 'xtls-rprx-vision'}]}]},
                    'streamSettings': {'network': 'tcp', 'security': 'reality', 'realitySettings': {
                        'serverName': 'localhost', 'fingerprint': 'chrome', 'publicKey': public,
                        'shortId': '0123456789abcdef'}}}]}
            atomic_json(root / 'client.json', client)
            def probe():
                process = subprocess.Popen([binary, 'run', '-config', str(root / 'client.json')],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    wait_port(socks_port, process)
                    return tunnel(socks_port, httpd.server_port, nonce)
                except OSError:
                    return False
                finally:
                    process.terminate()
                    process.wait(timeout=5)
            assert probe(), 'Reality hot-add tunnel failed'
            node.accounting()
            assert node.state['users']['selftest']['used_traffic'] > 0, 'Traffic accounting is empty'
            node.stop_core()
            node = Node(root, binary)
            node.start_core()
            assert probe(), 'Durable user did not survive a fresh supervisor'
            second_pid = node.child.pid
            node.mutate('selftest', None)
            assert node.child.pid == second_pid, 'Hot-remove restarted Xray'
            assert not probe(), 'Deleted user still opens a fresh Reality tunnel'
            node.stop_core()
            node = Node(root, binary)
            node.start_core()
            assert not probe(), 'Deleted user returned after reboot'
            print('XRAY SELFTEST PASS: Reality traffic, accounting, hot-add/remove, durable recovery')
        finally:
            if node:
                node.stop_core()
            for process in processes:
                process.terminate()
                process.wait(timeout=5)
            httpd.shutdown()
            httpd.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    run(parser.parse_args().binary)
