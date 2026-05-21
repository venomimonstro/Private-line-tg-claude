#!/bin/bash
set -e

if [ ! -f .env ]; then
    echo "ERROR: .env file not found. Copy .env.example to .env and fill in the values."
    exit 1
fi

echo "Running database migrations..."
alembic upgrade head

echo "Starting Private Line bot..."
exec python bot/main.py
