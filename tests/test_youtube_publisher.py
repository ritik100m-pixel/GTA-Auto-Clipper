import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from youtube_publish import YouTubePublisher
from publisher import UnifiedPublisher


class YouTubePublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.video_path = Path(self.temp.name) / "clip.mp4"
        self.meta_path = Path(self.temp.name) / "clip.json"
        self.video_path.write_bytes(b"test-mp4-data")
        self.metadata = {
            "title": "Insane GTA 6 Stunt",
            "youtube_description": "Watch this insane flip!",
            "detected_tags": ["GTA6", "Stunts"],
            "test": False,
            "approved": True,
        }
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(self.metadata, f)

        self.publisher = YouTubePublisher(
            client_id="dummy-id", client_secret="dummy-secret", refresh_token="dummy-refresh"
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_build_snippet_and_status(self):
        snippet, status = self.publisher.build_snippet_and_status(
            self.metadata, scheduled_for="2026-11-25T18:00:00+05:30", privacy_status="public"
        )
        self.assertIn("#Shorts", snippet["title"])
        self.assertIn("#Shorts", snippet["description"])
        self.assertEqual(snippet["categoryId"], "20")
        self.assertEqual(status["privacyStatus"], "private")
        self.assertIsNotNone(status.get("publishAt"))

    def test_publish_clip_dry_run(self):
        res = self.publisher.publish_clip(
            self.video_path, self.meta_path, scheduled_for="2026-11-25T18:00:00+05:30", dry_run=True
        )
        self.assertEqual(res["status"], "dry_run")
        self.assertTrue(res["id"].startswith("dry-run-yt-"))

        with open(self.meta_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["youtube_video_id"], res["id"])

    def test_duplicate_upload_prevention(self):
        # Once a video ID is written to metadata, re-publishing must raise ValueError
        self.metadata["youtube_video_id"] = "existing-yt-id-123"
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(self.metadata, f)

        with self.assertRaises(ValueError):
            self.publisher.publish_clip(self.video_path, self.meta_path)

    def test_unified_publisher_dispatch_dry_run(self):
        unified = UnifiedPublisher(provider="dry_run")
        res = unified.publish_clip(self.video_path, dry_run=True)
        self.assertEqual(res["provider"], "dry_run")
        self.assertEqual(res["result"]["status"], "simulated_success")


if __name__ == "__main__":
    unittest.main()
