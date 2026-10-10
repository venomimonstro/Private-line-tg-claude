#!/usr/bin/env python3
"""CLI called by release.sh to create/update release records in the DB.

Usage (from shell):
  python -m releases.cli create  --prev <hash> --commit <hash> [--version v1.2] [--changelog "..."]
  python -m releases.cli complete <release_id> [--log "..."]
  python -m releases.cli fail     <release_id> --error "message"
  python -m releases.cli rollback <release_id>
  python -m releases.cli list
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime


async def _cmd_create(args: argparse.Namespace) -> None:
    import os
    os.environ.setdefault("DATABASE_URL", "")
    from app.database import async_session_maker
    from app.models.release import Release
    from sqlalchemy import update

    async with async_session_maker() as session:
        # Mark all previous releases as not current
        await session.execute(update(Release).values(is_current=False))

        release = Release(
            version=args.version or (args.commit[:7] if args.commit else "unknown"),
            commit_hash=args.commit,
            previous_commit=args.prev,
            backup_id=int(args.backup_id) if args.backup_id else None,
            status="deploying",
            changelog=args.changelog or "",
            triggered_by=args.by or "release.sh",
            created_at=datetime.utcnow(),
            is_current=True,
        )
        session.add(release)
        await session.commit()
        print(release.id, flush=True)


async def _cmd_complete(args: argparse.Namespace) -> None:
    from app.database import async_session_maker
    from app.models.release import Release

    async with async_session_maker() as session:
        release = await session.get(Release, int(args.id))
        if release is None:
            print(f"ERROR: release {args.id} not found", file=sys.stderr)
            sys.exit(1)
        release.status = "success"
        release.deployed_at = datetime.utcnow()
        if args.log:
            release.deploy_log = args.log[:8000]
        release.is_current = True
        await session.commit()
    print("ok")


async def _cmd_fail(args: argparse.Namespace) -> None:
    from app.database import async_session_maker
    from app.models.release import Release

    async with async_session_maker() as session:
        release = await session.get(Release, int(args.id))
        if release is None:
            print(f"ERROR: release {args.id} not found", file=sys.stderr)
            sys.exit(1)
        release.status = "failed"
        release.error_message = (args.error or "Unknown error")[:2000]
        release.is_current = False
        if args.log:
            release.deploy_log = args.log[:8000]
        await session.commit()
    print("ok")


async def _cmd_rollback(args: argparse.Namespace) -> None:
    from app.database import async_session_maker
    from app.models.release import Release
    from sqlalchemy import update

    async with async_session_maker() as session:
        release = await session.get(Release, int(args.id))
        if release is None:
            print(f"ERROR: release {args.id} not found", file=sys.stderr)
            sys.exit(1)

        # Mark current as not current
        await session.execute(update(Release).values(is_current=False))

        release.status = "rolled_back"
        release.rolled_back_at = datetime.utcnow()

        # Find the target release (the one we're rolling back TO)
        target_id = args.target_id if hasattr(args, "target_id") and args.target_id else None
        if target_id:
            target = await session.get(Release, int(target_id))
            if target:
                target.status = "rolled_back_to"
                target.is_current = True

        await session.commit()
    print(release.previous_commit or "", flush=True)


async def _cmd_list(_args: argparse.Namespace) -> None:
    from app.database import async_session_maker
    from app.models.release import Release
    from sqlalchemy import select

    async with async_session_maker() as session:
        result = await session.execute(
            select(Release).order_by(Release.created_at.desc()).limit(20)
        )
        releases = result.scalars().all()
        if not releases:
            print("No releases found.")
            return
        print(f"{'ID':<5} {'VERSION':<20} {'STATUS':<15} {'COMMIT':<8} {'DATE':<20} {'CURRENT'}")
        print("-" * 80)
        for r in releases:
            dt = r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "—"
            commit = (r.commit_hash or "")[:7]
            current = "← CURRENT" if r.is_current else ""
            print(f"{r.id:<5} {r.version:<20} {r.status:<15} {commit:<8} {dt:<20} {current}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="releases.cli")
    sub = parser.add_subparsers(dest="cmd")

    p_create = sub.add_parser("create")
    p_create.add_argument("--prev", default="")
    p_create.add_argument("--commit", default="")
    p_create.add_argument("--version", default="")
    p_create.add_argument("--changelog", default="")
    p_create.add_argument("--by", default="release.sh")
    p_create.add_argument("--backup-id", default="")

    p_complete = sub.add_parser("complete")
    p_complete.add_argument("id")
    p_complete.add_argument("--log", default="")

    p_fail = sub.add_parser("fail")
    p_fail.add_argument("id")
    p_fail.add_argument("--error", default="")
    p_fail.add_argument("--log", default="")

    p_rollback = sub.add_parser("rollback")
    p_rollback.add_argument("id")
    p_rollback.add_argument("--target-id", default="")

    sub.add_parser("list")

    args = parser.parse_args()
    dispatch = {
        "create": _cmd_create,
        "complete": _cmd_complete,
        "fail": _cmd_fail,
        "rollback": _cmd_rollback,
        "list": _cmd_list,
    }
    fn = dispatch.get(args.cmd)
    if fn is None:
        parser.print_help()
        sys.exit(1)
    asyncio.run(fn(args))


if __name__ == "__main__":
    main()
