#!/bin/bash
# Verify backup integrity and content
# Can be run manually or by scheduler
set -e

BACKUP_DIR="${BACKUP_DIR:-./backups}"
VERIFY_DIR="/tmp/privateline_verify_$$"

echo "=== Backup Verification ==="

# Find latest backup
LATEST=$(ls -t "$BACKUP_DIR"/*.tar.gz 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
    echo "ERROR: No backups found in $BACKUP_DIR"
    exit 1
fi

echo "Verifying: $LATEST"
echo "Size: $(du -sh "$LATEST" | cut -f1)"

# Check archive integrity
if tar -tzf "$LATEST" > /dev/null 2>&1; then
    echo "✅ Archive integrity: OK"
else
    echo "❌ Archive integrity: FAILED"
    exit 1
fi

# Extract and check contents
mkdir -p "$VERIFY_DIR"
tar -xzf "$LATEST" -C "$VERIFY_DIR"

# Check for DB file
DB_COUNT=$(find "$VERIFY_DIR" -name "*.db" -o -name "*.sqlite" -o -name "*.sql" 2>/dev/null | wc -l)
if [ "$DB_COUNT" -gt 0 ]; then
    echo "✅ Database file found in backup"
else
    echo "⚠️  WARNING: No database file found in backup"
fi

# Check age — warn if backup is > 25 hours old
BACKUP_AGE=$(( $(date +%s) - $(stat -c %Y "$LATEST") ))
BACKUP_AGE_HOURS=$(( BACKUP_AGE / 3600 ))
if [ "$BACKUP_AGE_HOURS" -gt 25 ]; then
    echo "⚠️  WARNING: Latest backup is ${BACKUP_AGE_HOURS}h old (expected < 25h)"
else
    echo "✅ Backup age: ${BACKUP_AGE_HOURS}h (OK)"
fi

# Clean up
rm -rf "$VERIFY_DIR"
echo "=== Verification complete ==="
