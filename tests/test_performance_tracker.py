import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from performance_tracker import (
    VideoPerformanceLedger,
    collect_due_snapshots,
    feed_scheduler_from_48h,
)
from smart_scheduler import AnalyticsLedger


class PerformanceTrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.ledger = VideoPerformanceLedger(root / "video_performance.json")
        self.scheduler = AnalyticsLedger(root / "analytics.json")

    def tearDown(self):
        self.temp.cleanup()

    def test_register_is_idempotent_and_milestones_are_due(self):
        self.assertTrue(self.ledger.register("video-1", "2026-11-22T10:00:00+00:00", "stunt"))
        self.assertFalse(self.ledger.register("video-1", "2026-11-22T10:00:00+00:00", "stunt"))
        now = datetime(2026, 11, 22, 11, 5, tzinfo=timezone.utc)
        self.assertIn(("video-1", 1), self.ledger.due_milestones(now))

    def test_snapshot_stores_only_returned_public_metrics(self):
        self.ledger.register("video-2", "2026-11-22T10:00:00+00:00")
        snap = self.ledger.record_snapshot(
            "video-2", 1, {"viewCount": "125", "likeCount": "8", "commentCount": "2"}
        )
        self.assertEqual(snap["metrics"], {"views": 125, "likes": 8, "comments": 2})

    @patch("performance_tracker.fetch_video_statistics", return_value={
        "video-3": {"viewCount": "500", "likeCount": "20", "commentCount": "4"}
    })
    def test_collects_due_snapshot(self, fetch):
        self.ledger.register("video-3", "2026-11-22T10:00:00+00:00")
        now = datetime(2026, 11, 22, 11, 1, tzinfo=timezone.utc)
        result = collect_due_snapshots(self.ledger, "test-key", now=now)
        self.assertEqual(result["recorded"], 1)
        fetch.assert_called_once()

    def test_48h_snapshot_trains_scheduler_once(self):
        self.ledger.register("video-4", "2026-11-20T10:00:00+00:00", "chase")
        self.ledger.record_snapshot("video-4", 48, {"viewCount": "1200", "likeCount": "50"})
        self.assertEqual(feed_scheduler_from_48h(self.ledger, self.scheduler), 1)
        self.assertEqual(feed_scheduler_from_48h(self.ledger, self.scheduler), 0)
        record = self.scheduler.list_records()[0]
        self.assertEqual(record["category"], "chase")
        self.assertEqual(record["metrics"]["views"], 1200)


if __name__ == "__main__":
    unittest.main()
