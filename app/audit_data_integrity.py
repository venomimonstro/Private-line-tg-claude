"""Read-only production invariants for VPN control-plane data."""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime

from sqlalchemy import select

from app.database import async_session_maker
from app.enums import SubscriptionStatus, VpnKeyStatus, VpnServerStatus
from app.models.pending_operation import PendingOperation, PendingOperationType
from app.models.subscription import Subscription
from app.models.user import User
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer


async def audit() -> int:
    failures: list[str] = []
    now = datetime.utcnow()
    async with async_session_maker() as db:
        servers = (await db.execute(select(VpnServer))).scalars().all()
        server_by_id = {s.id: s for s in servers}
        subs = (await db.execute(select(Subscription))).scalars().all()
        sub_by_id = {s.id: s for s in subs}
        users = (await db.execute(select(User))).scalars().all()
        user_ids = {u.id for u in users}
        keys = (await db.execute(select(VpnKey))).scalars().all()
        active_keys = [k for k in keys if k.status == VpnKeyStatus.active]

        # One active key per (subscription,node), but multiple dedicated nodes
        # per subscription are allowed.
        pair_counts = Counter((k.subscription_id, k.server_id) for k in active_keys)
        for pair, count in pair_counts.items():
            if count > 1:
                failures.append(f"duplicate active keys sub/server={pair}: {count}")

        for key in active_keys:
            sub = sub_by_id.get(key.subscription_id)
            server = server_by_id.get(key.server_id)
            if key.user_id not in user_ids:
                failures.append(f"key#{key.id}: missing user")
                continue
            if sub is None:
                failures.append(f"key#{key.id}: missing subscription")
                continue
            if sub.user_id != key.user_id:
                failures.append(f"key#{key.id}: user/subscription mismatch")
            if sub.status not in (SubscriptionStatus.active, SubscriptionStatus.expires_soon):
                failures.append(f"key#{key.id}: active key on subscription {sub.status.value}")
            if not sub.expires_at or sub.expires_at <= now:
                failures.append(f"key#{key.id}: active key on expired subscription")
            if server is None:
                failures.append(f"key#{key.id}: missing VPN server")
            elif (
                server.status != VpnServerStatus.active
                or not server.is_selectable
                or server.provider != "marzban"
            ):
                failures.append(f"key#{key.id}: active key on non-production server#{server.id}")

        active_counts = Counter(k.server_id for k in active_keys)
        for server in servers:
            actual = active_counts.get(server.id, 0)
            if server.current_users != actual:
                failures.append(
                    f"server#{server.id}: current_users={server.current_users}, DB active={actual}"
                )
            if server.provider == "private-line-primary" and (
                server.status == VpnServerStatus.active or server.is_selectable
            ):
                failures.append(f"server#{server.id}: control-plane marked as VPN node")

        ops = (await db.execute(select(PendingOperation).where(
            PendingOperation.completed_at.is_(None)
        ))).scalars().all()
        pending_disable_ids = {
            op.vpn_key_id for op in ops
            if op.operation_type == PendingOperationType.disable_key.value
            and op.vpn_key_id is not None
        }
        for key in keys:
            if key.status == VpnKeyStatus.error and key.id not in pending_disable_ids:
                failures.append(
                    f"key#{key.id}: ERROR state without a pending remote-disable retry"
                )
        for op in ops:
            if op.operation_type == PendingOperationType.create_key.value:
                if op.subscription_id is None or op.server_id is None:
                    failures.append(f"pending#{op.id}: create_key missing identifiers")
                    continue
                # A stale queued create is harmless only until the worker marks it
                # complete. Report it if that exact node already has an active key.
                if pair_counts.get((op.subscription_id, op.server_id), 0):
                    failures.append(
                        f"pending#{op.id}: stale create_key while active key already exists"
                    )

    if failures:
        print(f"DATA AUDIT FAIL: {len(failures)} invariant violation(s)")
        for item in failures[:30]:
            print(" - " + item)
        return 2
    print(
        f"DATA AUDIT PASS: servers={len(servers)} subscriptions={len(subs)} "
        f"active_keys={len(active_keys)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(audit()))
