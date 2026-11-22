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
        self.assertNotIn("youtube_video_id", saved)
        self.assertNotIn("youtube_publish_status", saved)

    def test_build_snippet_uses_transcript_title_and_creator_credit(self):
        metadata = {
            "category": "police_chase",
            "transcript": "The cops are chasing me through downtown and I barely escaped",
            "credit": "CreatorName",
            "source_url": "https://youtube.com/watch?v=source123",
        }
        snippet, status = self.publisher.build_snippet_and_status(metadata)
        self.assertIn("GTA police chase", snippet["title"])
        self.assertIn("#Shorts", snippet["title"])
        self.assertIn("CreatorName", snippet["description"])
        self.assertIn("source123", snippet["description"])

    def test_resumable_upload_streams_video_and_saves_real_id(self):
        init_response = MagicMock()
        init_response.headers = {"Location": "https://upload.example/session"}
        upload_response = MagicMock()
        upload_response.status_code = 201
        upload_response.headers = {}
        upload_response.json.return_value = {"id": "uploaded-video-123"}

        with patch.object(self.publisher, "refresh_access_token", return_value="access-token"), \
             patch("youtube_publish.urllib.request.urlopen") as urlopen, \
             patch("youtube_publish.requests.put", return_value=upload_response) as put:
            urlopen.return_value.__enter__.return_value = init_response
            result = self.publisher.publish_clip(
                self.video_path, self.meta_path,
                scheduled_for="2026-11-25T18:00:00+05:30",
            )

        self.assertEqual(result["id"], "uploaded-video-123")
        put.assert_called_once()
        self.assertEqual(put.call_args.kwargs["data"], b"test-mp4-data")
        self.assertIn("Content-Range", put.call_args.kwargs["headers"])
        saved = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["youtube_video_id"], "uploaded-video-123")

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
