"""Repair a single registered standalone node, then check its active keys.

Used by scripts/update_vpn_stability.sh; never prints VPN credentials.
"""
import argparse
import asyncio
import ipaddress
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from app.database import async_session_maker
from app.enums import SubscriptionStatus, VpnKeyStatus
from app.models.subscription import Subscription
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _server_protocol, repair_marzban_keys
from app.repair_vless_hosts import repair
from app.diagnose_vless import main as diagnose


async def run(node, probe_only=False):
    ipaddress.ip_address(node)
    async with async_session_maker() as db:
        servers = (await db.execute(select(VpnServer))).scalars().all()
        servers = [s for s in servers if urlsplit(s.api_url).hostname == node
                   and _server_protocol(s) == 'marzban']
        if len(servers) != 1:
            raise RuntimeError('Expected exactly one registered Marzban node with this IP')
        server_id = servers[0].id
        subscriptions = list((await db.execute(
            select(Subscription.id).join(VpnKey, VpnKey.subscription_id == Subscription.id)
            .where(VpnKey.server_id == server_id,
                   VpnKey.status == VpnKeyStatus.active,
                   Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
                   Subscription.expires_at > datetime.utcnow())
            .distinct().order_by(Subscription.id)
        )).scalars().all())
    if not subscriptions:
        raise RuntimeError('No active unexpired subscriptions on this node')
    if probe_only:
        print(subscriptions[0])
        return 0
    if await repair(server_id, apply=True):
        return 1
    async with async_session_maker() as db:
        fixed, errors, changed = await repair_marzban_keys(db, server_id=server_id)
    print(f'User reconciliation: repaired={fixed}; errors={errors}; changed_links={len(changed)}')
    failures = errors
    for sub_id in subscriptions:
        failures += bool(await diagnose(sub_id, repair=True))
    if failures:
        print('FAIL: unresolved configuration errors; see diagnostics above')
        return 1
    print('Configuration checked. Next: actual VLESS handshake and HTTPS egress test.')
    print('If a destination/Reality parameter was corrected, import the key from the bot again.')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--node', required=True)
    parser.add_argument('--probe-subscription', action='store_true')
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run(args.node, args.probe_subscription)))
    except Exception as exc:
        print(f'FAIL: {type(exc).__name__}: {exc}')
        raise SystemExit(1)
