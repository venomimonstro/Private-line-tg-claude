"""Recover one existing paid/previously-attempted subscription without reissuing working keys.

Usage:
  python -m app.recover_failed_activations --subscription 2
  python -m app.recover_failed_activations --subscription 2 --apply

Run when the regular bot polling process is stopped, so the scheduler cannot
race with this one-shot recovery. Never activates pending_payment subscriptions.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session_maker
from app.enums import SubscriptionStatus, VpnKeyStatus
from app.models.key_delivery_log import KeyDeliveryLog
from app.models.pending_operation import PendingOperation, PendingOperationType
from app.models.subscription import Subscription
from app.models.user import User
from app.services.subscription import activate_subscription
from app.services.user_notifications import send_key_to_user
from app.services.vpn_key import get_access_url

log = logging.getLogger(__name__)

ALLOWED = (
    SubscriptionStatus.activation_error,
    SubscriptionStatus.active,
    SubscriptionStatus.expires_soon,
)


async def recover(subscription_id: int, apply: bool) -> int:
    async with async_session_maker() as session:
        result = await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.tariff), selectinload(Subscription.vpn_keys))
            .where(Subscription.id == subscription_id)
            .with_for_update()
        )
        sub = result.scalar_one_or_none()
        if sub is None:
            print(f"ERROR: subscription {subscription_id} not found")
            return 2
        keys = [k for k in sub.vpn_keys if k.status == VpnKeyStatus.active]
        print(
            f"Subscription {sub.id}: status={sub.status.value} "
            f"attempts={sub.activation_attempts} active_keys={len(keys)} "
            f"tariff={'present' if sub.tariff else 'missing'}"
        )
        if sub.status not in ALLOWED:
            print("SKIP: non-activated/pending/expired/disabled subscription; no payment bypass")
            return 2
        if sub.tariff is None:
            print("ERROR: tariff missing")
            return 2
        user = await session.get(User, sub.user_id)
        if user is None or not user.telegram_id:
            print("ERROR: Telegram account is not linked to this subscription")
            return 2
        if len(keys) > 1:
            print("ERROR: multiple active keys; resolve before recovery")
            return 2
        if not apply:
            print("DRY RUN: no database changes, no message sent")
            return 0

        if keys:
            key = keys[0]
            # Never rotate a functioning key. If a queued operation created
            # it after an activation error, reconcile status without reissuing.
            if sub.status == SubscriptionStatus.activation_error:
                sub.status = SubscriptionStatus.active
                sub.last_activation_error = None
                sub.activation_attempts = 0
                sub.started_at = sub.started_at or datetime.utcnow()
                if sub.expires_at is None:
                    from datetime import timedelta
                    sub.expires_at = datetime.utcnow() + timedelta(days=sub.tariff.duration_days)
            print(f"Existing key {key.id} preserved")
        else:
            if sub.status != SubscriptionStatus.activation_error:
                print("ERROR: active subscription without a key; manual review required")
                return 2
            sub.activation_attempts = 0
            key = await activate_subscription(sub, sub.tariff, session)
            if key is None:
                print("ERROR: key activation unsuccessful; check subscription and bot logs")
                await session.commit()
                return 1
            print(f"Created VPN key ID {key.id} on server {key.server_id}")

        try:
            url = get_access_url(key)
            if not url.startswith(("vless://", "ss://")):
                raise ValueError("Unexpected VPN key protocol")
        except Exception as exc:
            print(f"ERROR: stored VPN URL invalid: {type(exc).__name__}: {exc}")
            await session.rollback()
            return 1

        # Retire stale create_key queue entries for this subscription so that
        # retries cannot delete/replace the key we have just validated.
        pending = await session.execute(
            select(PendingOperation).where(
                PendingOperation.subscription_id == sub.id,
                PendingOperation.operation_type == PendingOperationType.create_key.value,
                PendingOperation.completed_at.is_(None),
            )
        )
        for op in pending.scalars():
            op.completed_at = datetime.utcnow()
            op.vpn_key_id = key.id

        await session.commit()

    delivered = None
    try:
        await send_key_to_user(int(user.telegram_id), key)
        delivered = datetime.utcnow()
        print("SUCCESS: existing/created key sent to Telegram")
    except Exception as exc:
        print(f"KEY SAVED but Telegram delivery failed: {type(exc).__name__}: {exc}")
    async with async_session_maker() as session:
        session.add(
            KeyDeliveryLog(
                user_id=user.id,
                subscription_id=subscription_id,
                vpn_key_id=key.id,
                delivered_at=delivered,
                attempt_number=1,
                error_msg=None if delivered is not None else "Delivery failed; check Telegram bot logs",
                created_at=datetime.utcnow(),
            )
        )
        await session.commit()
    return 0 if delivered is not None else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely recover VPN key delivery for one subscription")
    parser.add_argument("--subscription", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.subscription <= 0:
        parser.error("--subscription must be a positive integer")
    raise SystemExit(asyncio.run(recover(args.subscription, args.apply)))


if __name__ == "__main__":
    main()
