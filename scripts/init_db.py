"""Run Alembic migrations and seed tariffs. Safe to run multiple times."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))


def run_migrations() -> None:
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("Migration failed:")
        print(result.stderr)
        sys.exit(1)
    print("Migrations applied successfully.")
    if result.stdout:
        print(result.stdout)


async def seed() -> None:
    from scripts.seed_tariffs import seed_tariffs
    await seed_tariffs()


def main() -> None:
    run_migrations()
    asyncio.run(seed())
    print("Database initialised.")


if __name__ == "__main__":
    main()
