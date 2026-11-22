import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from cloud_runner import CloudBatchRunner


class CloudBatchRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name) / "cloud_state"
        self.output_dir = Path(self.temp.name) / "output"
        self.recordings_dir = Path(self.temp.name) / "recordings"

        self.runner = CloudBatchRunner(
            state_dir=str(self.state_dir),
            output_dir=str(self.output_dir),
            recordings_dir=str(self.recordings_dir),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_discovery_and_enqueue_authorized_streams(self):
        # Allowlist one channel
        self.runner.policy.add_source(
            "UC-auth-1", "AuthChannel", permission_status="authorized",
            permission_scope=["record", "clip", "publish"],
            proof_reference="Permission email", gameplay_verified=True,
        )

        mock_streams = [
            {"channel_id": "UC-auth-1", "channel": "AuthChannel", "url": "https://youtube.com/watch?v=aaa111", "video_id": "aaa111", "title": "GTA Stream 1"},
            {"channel_id": "UC-unauth-2", "channel": "RandomChannel", "url": "https://youtube.com/watch?v=bbb222", "title": "GTA Stream 2"},
        ]

        with patch("find_streams.find_streams", return_value=mock_streams), \
             patch("cloud_runner.find_channel_live_streams", return_value=[]), \
             patch("cloud_runner.find_channel_videos", return_value=[]):
            enqueued = self.runner.run_discovery_and_enqueue(api_key="dummy-key")

        self.assertEqual(len(enqueued), 1)
        self.assertEqual(enqueued[0]["job_key"], "youtube-live:UC-auth-1:https://youtube.com/watch?v=aaa111")
        self.assertTrue(enqueued[0]["authorized"])
        self.assertEqual(enqueued[0]["payload"]["video_id"], "aaa111")

    def test_discovery_enqueues_authorized_regular_videos(self):
        self.runner.policy.add_source(
            "UC-vod", "VideoCreator", permission_status="authorized",
            permission_scope=["record", "clip"],
            proof_reference="Written permission", gameplay_verified=True,
        )
        video = {
            "video_id": "vod123", "channel_id": "UC-vod",
            "channel": "VideoCreator", "title": "GTA full video",
            "url": "https://www.youtube.com/watch?v=vod123",
            "published_at": "2026-11-22T10:00:00+00:00",
            "source_type": "youtube_video",
        }
        with patch("find_streams.find_streams", return_value=[]), \
             patch("cloud_runner.find_channel_live_streams", return_value=[]), \
             patch("cloud_runner.find_channel_videos", return_value=[video]):
            jobs = self.runner.run_discovery_and_enqueue(api_key="dummy-key")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_key"], "youtube-video:UC-vod:vod123")
        self.assertEqual(jobs[0]["payload"]["source_type"], "youtube_video")
        self.assertTrue(jobs[0]["authorized"])

    def test_discovery_enqueues_configured_creator_livestream(self):
        self.runner.policy.add_source(
            "UC-live", "LiveCreator", permission_status="authorized",
            permission_scope=["record", "clip"], proof_reference="Permission",
            gameplay_verified=True,
        )
        stream = {
            "video_id": "live123", "channel_id": "UC-live", "channel": "LiveCreator",
            "title": "Live GTA", "url": "https://www.youtube.com/watch?v=live123",
            "source_type": "youtube_live",
        }
        with patch("find_streams.find_streams", return_value=[]), \
             patch("cloud_runner.find_channel_live_streams", return_value=[stream]), \
             patch("cloud_runner.find_channel_videos", return_value=[]):
            jobs = self.runner.run_discovery_and_enqueue(api_key="dummy-key")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_key"], "youtube-live:UC-live:https://www.youtube.com/watch?v=live123")
        self.assertEqual(jobs[0]["payload"]["source_type"], "youtube_live")

    def test_discovery_reports_vods_without_enqueuing_them(self):
        self.runner.policy.add_source(
            "UC-vod-only", "VideoCreator", permission_status="authorized",
            permission_scope=["record", "clip"], proof_reference="Written permission",
            gameplay_verified=True,
        )
        video = {
            "video_id": "vod456", "channel_id": "UC-vod-only",
            "channel": "VideoCreator", "title": "GTA full video",
            "url": "https://www.youtube.com/watch?v=vod456",
            "published_at": "2026-11-22T10:00:00+00:00",
        }
        with patch("find_streams.find_streams", return_value=[]), \\
             patch("cloud_runner.find_channel_live_streams", return_value=[]), \\
             patch("cloud_runner.find_channel_videos", return_value=[video]):
            jobs = self.runner.run_discovery_and_enqueue(api_key="dummy-key")
        self.assertEqual(jobs, [])
        self.assertEqual(self.runner.queue.claim_next("test-worker"), None)

    def test_legacy_vod_queue_job_is_skipped_without_live_recorder(self):
        self.runner.policy.add_source(
            "UC-vod-legacy", "VideoCreator", permission_status="authorized",
            permission_scope=["record", "clip"], proof_reference="Written permission",
            gameplay_verified=True,
        )
        self.runner.queue.enqueue(
            "capture_and_clip", "legacy-vod-job",
            {
                "url": "https://www.youtube.com/watch?v=vod789",
                "video_id": "vod789", "channel_id": "UC-vod-legacy",
                "channel": "VideoCreator", "gameplay_verified": True,
                "source_type": "youtube_video",
            },
            authorized=True,
        )
        with patch("cloud_runner.run_recovery") as recover:
            stats = self.runner.run_batch(max_duration_seconds=30, target_clips=1)
        self.assertEqual(stats["jobs_claimed"], 1)
        recover.assert_not_called()

    def test_run_batch_processes_queued_job_and_schedules_clips(self):
        self.runner.policy.add_source(
            "UC-auth-1", "AuthChannel", permission_status="authorized",
            permission_scope=["record", "clip", "publish"],
            proof_reference="Permission email", gameplay_verified=True,
        )
        # Create a dummy clip output in output_dir
        dummy_mp4 = self.output_dir / "clip_001.mp4"
        dummy_json = self.output_dir / "clip_001.json"
        dummy_mp4.write_bytes(b"dummy-mp4")
        with open(dummy_json, "w", encoding="utf-8") as f:
            json.dump({
                "title": "Insane Chase",
                "category": "police_chase",
                "approved": True,
                "source_channel_id": "UC-auth-1",
                "test": False,
            }, f)

        # Enqueue an authorized job
        self.runner.queue.enqueue(
            "capture_and_clip", "stream-job-1",
            {
                "url": "https://example.com/live", "channel_id": "UC-auth-1",
                "channel": "AuthChannel", "credit": "AuthChannel",
                "gameplay_verified": True,
            },
            authorized=True
        )

        with patch("cloud_runner.run_recovery", return_value=([(str(dummy_mp4), str(dummy_json))], 1)):
            stats = self.runner.run_batch(
                max_duration_seconds=30, target_clips=1, dry_run=True,
                enable_publishing=True,
            )

        self.assertEqual(stats["jobs_claimed"], 1)
        self.assertEqual(stats["clips_published"], 0)
        self.assertEqual(stats["clips_previewed"], 1)

        with open(dummy_json, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertFalse(saved.get("youtube_video_id"))
        self.assertFalse(saved.get("dry_run_published"))

    def test_publishing_is_disabled_by_default(self):
        dummy_mp4 = self.output_dir / "clip_002.mp4"
        dummy_json = self.output_dir / "clip_002.json"
        dummy_mp4.write_bytes(b"dummy-mp4")
        dummy_json.write_text(json.dumps({
            "title": "Insane Chase",
            "category": "police_chase",
            "approved": True,
            "source_channel_id": "UC-auth-2",
        }), encoding="utf-8")
        self.runner.policy.add_source(
            "UC-auth-2", "AuthChannel", permission_status="authorized",
            proof_reference="Permission email", gameplay_verified=True,
        )
        self.runner.queue.enqueue(
            "capture_and_clip", "stream-job-2",
            {"url": "https://example.com/live", "channel_id": "UC-auth-2",
             "channel": "AuthChannel", "credit": "AuthChannel", "gameplay_verified": True},
            authorized=True,
        )
        with patch("cloud_runner.run_recovery", return_value=([(str(dummy_mp4), str(dummy_json))], 1)):
            stats = self.runner.run_batch(max_duration_seconds=30, target_clips=1, dry_run=True)
        self.assertEqual(stats["clips_published"], 0)
        saved = json.loads(dummy_json.read_text(encoding="utf-8"))
        self.assertFalse(saved.get("dry_run_published"))

    def test_real_publishing_fails_closed_without_youtube_oauth(self):
        self.runner.policy.add_source(
            "UC-publish", "PublishCreator", permission_status="authorized",
            permission_scope=["record", "clip", "publish"],
            proof_reference="Permission email", gameplay_verified=True,
        )
        dummy_mp4 = self.output_dir / "clip_003.mp4"
        dummy_json = self.output_dir / "clip_003.json"
        dummy_mp4.write_bytes(b"dummy-mp4")
        dummy_json.write_text(json.dumps({
            "title": "GTA police chase",
            "approved": True,
            "source_channel_id": "UC-publish",
        }), encoding="utf-8")
        self.runner.queue.enqueue(
            "capture_and_clip", "publish-job",
            {"url": "https://example.com/live", "channel_id": "UC-publish",
             "channel": "PublishCreator", "gameplay_verified": True},
            authorized=True,
        )
        with patch("cloud_runner.run_recovery", return_value=([(str(dummy_mp4), str(dummy_json))], 1)):
            stats = self.runner.run_batch(
                max_duration_seconds=30, target_clips=1,
                dry_run=False, enable_publishing=True,
            )
        self.assertEqual(stats["clips_published"], 0)
        self.assertTrue(any("OAuth is not fully configured" in err for err in stats["errors"]))

    def test_stale_queued_job_is_skipped_after_source_permission_is_missing(self):
        self.runner.queue.enqueue(
            "capture_and_clip", "stale-stream-job",
            {"url": "https://example.com/live"},
            authorized=True,
        )
        with patch("cloud_runner.run_recovery") as recover:
            stats = self.runner.run_batch(max_duration_seconds=30, target_clips=1)
        self.assertEqual(stats["jobs_claimed"], 1)
        self.assertEqual(stats["jobs_skipped_unauthorized"], 1)
        recover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
