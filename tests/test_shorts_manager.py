import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import shorts_manager as manager


class TitleTests(unittest.TestCase):
    def test_title_is_cleaned_and_short_length(self):
        title = manager.title_for("GTA_5__Funny_Police_Chase_FINAL.mp4")
        self.assertIn("#Shorts", title)
        self.assertNotIn("_", title)
        self.assertLessEqual(len(title), 100)

    def test_empty_title_gets_fallback(self):
        self.assertTrue(manager.title_for("___video.mp4").strip())


class ScheduleTests(unittest.TestCase):
    def test_next_slot_is_at_least_twenty_minutes_away(self):
        state = {"processed": {}}
        now = datetime.now(manager.IST)
        slot = datetime.fromisoformat(manager.next_slot(state))
        self.assertGreater(slot, now + timedelta(minutes=19))

    def test_next_slot_advances_after_last_reserved_slot(self):
        now = datetime.now(manager.IST)
        first = datetime.fromisoformat(manager.next_slot({"processed": {}}))
        state = {"processed": {"one": {"publish_at": first.isoformat()}}}
        second = datetime.fromisoformat(manager.next_slot(state))
        self.assertGreater(second, first)


class StateTests(unittest.TestCase):
    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{broken")
            with patch.object(manager, "STATE", path):
                with self.assertRaises(Exception):
                    manager.read_state()

    def test_empty_state_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with patch.object(manager, "STATE", path):
                state = manager.read_state()
                self.assertEqual(state, {"version": 1, "processed": {}})
                self.assertTrue(path.exists())


class QueueSafetyTests(unittest.TestCase):
    def test_empty_queue_does_not_upload_or_delete(self):
        with patch.dict("os.environ", {
            "GDRIVE_INCOMING_FOLDER_ID": "folder",
            "GTA_OWNER_CHANNEL_ID": "channel",
        }), patch.object(manager, "auth", return_value={"Authorization": "Bearer test"}), \
             patch.object(manager, "drive_files", return_value=[]), \
             patch.object(manager, "upload") as upload, \
             patch.object(manager, "delete_drive_file") as delete, \
             tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "STATE", Path(tmp) / "state.json"):
                manager.run()
            upload.assert_not_called()
            delete.assert_not_called()

    def test_uncertain_upload_marker_is_not_retried(self):
        video = {
            "id": "drive-file-1", "name": "clip.mp4", "mimeType": "video/mp4",
            "size": "123", "appProperties": {"uploadStartedAt": "2026-11-23T10:00:00+05:30"},
        }
        with patch.dict("os.environ", {
            "GDRIVE_INCOMING_FOLDER_ID": "folder",
            "GTA_OWNER_CHANNEL_ID": "channel",
        }), patch.object(manager, "auth", return_value={"Authorization": "Bearer test"}), \
             patch.object(manager, "drive_files", return_value=[video]), \
             patch.object(manager, "upload") as upload, \
             patch.object(manager, "delete_drive_file") as delete, \
             tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "STATE", Path(tmp) / "state.json"):
                manager.run()
            upload.assert_not_called()
            delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
