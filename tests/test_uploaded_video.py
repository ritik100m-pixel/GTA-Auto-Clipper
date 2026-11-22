import os
import unittest
from unittest.mock import patch

from process_uploaded_video import process_uploaded_video


class FakeQueue:
    def __init__(self):
        self.job = None

    def enqueue(self, **kwargs):
        self.job = kwargs
        return {"id": "job-1", **kwargs}


class FakeRunner:
    last = None

    def __init__(self):
        self.queue = FakeQueue()
        self.batch_args = None
        FakeRunner.last = self

    def run_batch(self, **kwargs):
        self.batch_args = kwargs
        return {"clips_generated": 2, "clips_published": 0, "errors": []}


class UploadedVideoTests(unittest.TestCase):
    def setUp(self):
        self.info = {
            "id": "video123",
            "channel_id": "UC-owner",
            "channel": "Owner Channel",
            "uploader": "Owner Channel",
            "title": "GTA gameplay session",
            "duration": 600,
            "is_live": False,
            "live_status": "not_live",
        }

    def test_requires_explicit_rights_confirmation(self):
        with self.assertRaisesRegex(PermissionError, "Confirm"):
            process_uploaded_video(
                "https://youtu.be/video123", "UC-owner",
                info_reader=lambda _: self.info, runner_factory=FakeRunner,
            )

    def test_rejects_video_from_a_different_channel(self):
        with self.assertRaisesRegex(PermissionError, "does not match"):
            process_uploaded_video(
                "https://youtu.be/video123", "UC-someone-else",
                confirm_rights=True, info_reader=lambda _: self.info,
                runner_factory=FakeRunner,
            )

    def test_rejects_livestreams_and_long_videos(self):
        live = dict(self.info, is_live=True)
        with self.assertRaisesRegex(ValueError, "completed regular video"):
            process_uploaded_video(
                "https://youtu.be/video123", "UC-owner",
                confirm_rights=True, info_reader=lambda _: live,
                runner_factory=FakeRunner,
            )
        long_video = dict(self.info, duration=5401)
        with self.assertRaisesRegex(ValueError, "90 minutes"):
            process_uploaded_video(
                "https://youtu.be/video123", "UC-owner",
                confirm_rights=True, info_reader=lambda _: long_video,
                runner_factory=FakeRunner,
            )

    def test_queues_only_the_submitted_video_and_runs_ephemerally(self):
        previous = os.environ.get("GTA_SOURCE_ALLOWLIST_JSON")
        with patch.dict(os.environ, {"GTA_UNATTENDED_POSTS": "1"}, clear=False):
            stats = process_uploaded_video(
                "https://youtu.be/video123", "UC-owner",
                confirm_rights=True, target_clips=3, dry_run=True,
                info_reader=lambda _: self.info, runner_factory=FakeRunner,
            )
        runner = FakeRunner.last
        self.assertEqual(stats["clips_generated"], 2)
        self.assertEqual(runner.queue.job["payload"]["video_id"], "video123")
        self.assertEqual(runner.queue.job["payload"]["source_type"], "youtube_video")
        self.assertEqual(runner.batch_args["target_clips"], 3)
        self.assertTrue(runner.batch_args["dry_run"])
        current = os.environ.get("GTA_SOURCE_ALLOWLIST_JSON")
        self.assertEqual(current, previous)

    def test_real_auto_publishing_requires_unattended_opt_in(self):
        with self.assertRaisesRegex(ValueError, "unattended=true"):
            process_uploaded_video(
                "https://youtu.be/video123", "UC-owner",
                confirm_rights=True, dry_run=False, enable_publishing=True,
                info_reader=lambda _: self.info, runner_factory=FakeRunner,
            )


if __name__ == "__main__":
    unittest.main()
