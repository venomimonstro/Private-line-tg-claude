"""Print verified node IDs that hold active keys for one subscription."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

from sqlalchemy import select

from app.database import async_session_maker
from app.enums import SubscriptionStatus, VpnKeyStatus, VpnServerStatus
from app.models.subscription import Subscription
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _server_protocol


async def main(subscription_id: int) -> int:
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(VpnServer)
            .join(VpnKey, VpnKey.server_id == VpnServer.id)
            .join(Subscription, Subscription.id == VpnKey.subscription_id)
            .where(
                VpnKey.subscription_id == subscription_id,
                VpnKey.status == VpnKeyStatus.active,
                Subscription.status.in_([
                    SubscriptionStatus.active,
                    SubscriptionStatus.expires_soon,
                ]),
                Subscription.expires_at > datetime.utcnow(),
                VpnServer.status == VpnServerStatus.active,
                VpnServer.is_selectable.is_(True),
                VpnServer.provider != "private-line-primary",
            )
            .order_by(VpnKey.server_id.asc())
        )).scalars().all()
        ids = sorted({
            server.id for server in rows
            if _server_protocol(server) == "marzban"
        })
        if not ids:
            return 2
        for server_id in ids:
            print(server_id)
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--subscription", type=int, required=True)
    args = parser.parse_args()
    if args.subscription <= 0:
        parser.error("subscription must be positive")
    raise SystemExit(asyncio.run(main(args.subscription)))
