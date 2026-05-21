#!/bin/bash
# Usage: ./scripts/restore.sh <backup_file.tar.gz>
# Restores database from backup created by backup_manager
set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <backup_file.tar.gz>"
    echo "Available backups:"
    ls -la backups/*.tar.gz 2>/dev/null || echo "No backups found"
    exit 1
fi

BACKUP_FILE="$1"
RESTORE_DIR="/tmp/privateline_restore_$$"

echo "=== Private Line Restore ==="
echo "Backup file: $BACKUP_FILE"

if [ ! -f "$BACKUP_FILE" ]; then
    echo "ERROR: Backup file not found: $BACKUP_FILE"
    exit 1
fi

# Create restore directory
mkdir -p "$RESTORE_DIR"
tar -xzf "$BACKUP_FILE" -C "$RESTORE_DIR"

echo "Backup contents:"
ls -la "$RESTORE_DIR/"

# Find the database file
DB_FILE=$(find "$RESTORE_DIR" -name "*.db" -o -name "*.sqlite" 2>/dev/null | head -1)
SQL_FILE=$(find "$RESTORE_DIR" -name "*.sql" 2>/dev/null | head -1)

if [ -n "$DB_FILE" ]; then
    echo "Found SQLite database: $DB_FILE"
    # Stop services first
    echo "WARNING: Stop the bot and admin before restoring!"
    echo "Press Enter to continue or Ctrl+C to cancel..."
    read -r
    cp "$DB_FILE" ./private_line.db.restore_$(date +%Y%m%d_%H%M%S)
    cp "$DB_FILE" ./private_line.db
    echo "✅ SQLite database restored successfully"
elif [ -n "$SQL_FILE" ]; then
    echo "Found SQL dump: $SQL_FILE"
    echo "For PostgreSQL restore, run:"
    echo "  psql -U privateline -d privateline < $SQL_FILE"
else
    echo "ERROR: No database file found in backup"
    ls -la "$RESTORE_DIR/"
    exit 1
fi

# Clean up
rm -rf "$RESTORE_DIR"
echo "=== Restore complete ==="
echo "Run 'alembic upgrade head' to ensure migrations are up to date"
