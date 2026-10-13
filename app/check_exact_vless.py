"""Read-only verification of a privately supplied Telegram URL against its node."""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import sys
from urllib.parse import parse_qs, urlsplit
from sqlalchemy import select
from app.database import async_session_maker
from app.models.vpn_server import VpnServer
from app.models.vpn_key import VpnKey
from app.services.vpn_key import _make_client, _server_protocol, get_client_access_url

FIELDS = ('security', 'encryption', 'type', 'flow', 'fp', 'sni', 'pbk', 'sid', 'headerType')


def signature(raw):
    raw = raw.strip()
    if any(c.isspace() for c in raw) or '&amp;' in raw:
        raise ValueError('Link contains whitespace or HTML &amp;; copy the complete plain-text link')
    parsed = urlsplit(raw)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if any(len(values) != 1 for values in query.values()):
        raise ValueError('Duplicate URI parameter')
    if parsed.scheme != 'vless' or not parsed.username or not parsed.hostname or not parsed.port:
        raise ValueError('Incomplete VLESS URI')
    unsupported = set(query) - set(FIELDS) - {'spx'}
    if unsupported or query.get('spx', [''])[0] not in ('', '/'):
        raise ValueError('URI contains unsupported parameters; refusing to silently ignore them')
    return (parsed.username.lower(), parsed.hostname.lower(), parsed.port,
            tuple((key, query.get(key, [''])[0]) for key in FIELDS))


async def check(raw, server_id):
    supplied = signature(raw)
    print('LINK fingerprint (not a credential): ' + hashlib.sha256(raw.strip().encode()).hexdigest()[:16])
    print('Supplied endpoint: %s:%s' % (supplied[1], supplied[2]))
    async with async_session_maker() as db:
        server = await db.get(VpnServer, server_id)
        if server is None or _server_protocol(server) != 'marzban':
            raise ValueError('Requested node does not exist')
        if supplied[1] != urlsplit(server.api_url).hostname:
            raise ValueError('Link points to a different host than the selected node')
        rows = (await db.execute(select(VpnKey).where(VpnKey.server_id == server_id))).scalars().all()
        matches = []
        for row in rows:
            try:
                current = signature(get_client_access_url(row))
            except Exception:
                continue
            if current[0] == supplied[0]:
                matches.append(row)
                print('DB match: key=%s subscription=%s status=%s' % (
                    row.id, row.subscription_id, row.status.value))
                print('Telegram URI parameters match stored display URI: ' + str(current == supplied))
        if not matches:
            print('WARN: UUID is absent from local subscription keys on this node')
        client = _make_client(server)
        try:
            config = await client._get_core_config()
            inbounds = [x for x in (config or {}).get('inbounds', [])
                        if x.get('protocol') == 'vless' and x.get('streamSettings', {}).get('security') == 'reality']
            if len(inbounds) != 1:
                raise ValueError('Node has no unambiguous Reality inbound')
            inbound = inbounds[0]
            reality = await client.get_reality_params()
            users = await client.get_users()
            matched = [u for u in users.values()
                       if u.get('proxies', {}).get('vless', {}).get('id', '').lower() == supplied[0]]
            if len(matched) != 1:
                raise ValueError('Supplied UUID is missing or duplicated on node')
            user = matched[0]
            params = dict(supplied[3])
            actual_reality = inbound['streamSettings']['realitySettings']
            checks = {
                'UUID registered and active': user.get('status') == 'active',
                'VPN port': supplied[2] == inbound['port'],
                'assigned inbound': inbound['tag'] in user.get('inbounds', {}).get('vless', []),
                'Vision flow': user.get('proxies', {}).get('vless', {}).get('flow') == params['flow'] == 'xtls-rprx-vision',
                'Reality public key': bool(reality) and params['pbk'] == reality[0],
                'shortId accepted': params['sid'] in actual_reality.get('shortIds', []),
                'SNI accepted': params['sni'] in actual_reality.get('serverNames', []),
                'transport': params['type'] in ('tcp', 'raw') and params['security'] == 'reality',
            }
            for name, ok in checks.items():
                print(('PASS: ' if ok else 'FAIL: ') + name)
            if not all(checks.values()):
                raise ValueError('Supplied Telegram key does not match node; tunnel test stopped')
            print('Key/node configuration matches. Live authentication is proved by the tunnel test next.')
        finally:
            await client.aclose()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--server', required=True, type=int)
    args = p.parse_args()
    try:
        asyncio.run(check(sys.stdin.readline(8192).strip(), args.server))
    except Exception as exc:
        # Transport exceptions may include request URLs. Never dump them.
        print('FAIL: ' + (str(exc) if type(exc) is ValueError else type(exc).__name__), file=sys.stderr)
        raise SystemExit(2)
