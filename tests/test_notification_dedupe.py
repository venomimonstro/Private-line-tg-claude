from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import notification


class AdminNotificationDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_burst_is_sent_once(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(notification, "_STATE_FILE", Path(folder) / "state.json"), \
             patch.object(notification, "_try_send", new=AsyncMock(return_value=True)) as send:
            await notification.notify_admin("same failure", cooldown_seconds=3600)
            await notification.notify_admin("same failure", cooldown_seconds=3600)
            self.assertEqual(send.await_count, 1)

    async def test_semantic_key_suppresses_messages_with_changing_metrics(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(notification, "_STATE_FILE", Path(folder) / "state.json"), \
             patch.object(notification, "_try_send", new=AsyncMock(return_value=True)) as send:
            await notification.notify_admin(
                "Disk 81%", dedupe_key="health:disk", cooldown_seconds=3600
            )
            await notification.notify_admin(
                "Disk 82%", dedupe_key="health:disk", cooldown_seconds=3600
            )
            self.assertEqual(send.await_count, 1)

    async def test_failed_transport_releases_claim_for_retry(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(notification, "_STATE_FILE", Path(folder) / "state.json"), \
             patch.object(notification.settings, "BACKUP_BOT_TOKEN", ""), \
             patch.object(notification, "_try_send", new=AsyncMock(
                 side_effect=[False, True]
             )) as send:
            await notification.notify_admin("temporary transport failure", cooldown_seconds=3600)
            await notification.notify_admin("temporary transport failure", cooldown_seconds=3600)
            self.assertEqual(send.await_count, 2)


if __name__ == "__main__":
    unittest.main()
