import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from smart_scheduler import (
    AnalyticsLedger,
    SmartScheduler,
    compute_performance_score,
)


class SmartSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger_file = Path(self.temp.name) / "analytics.json"
        self.ledger = AnalyticsLedger(self.ledger_file)
        self.scheduler = SmartScheduler(self.ledger)

    def tearDown(self):
        self.temp.cleanup()

    def test_performance_score_calculation(self):
        metrics = {
            "views": 1000,
            "engaged_views": 600,
            "stayed_to_watch_ratio": 0.75,
            "likes": 80,
            "comments": 20,
            "subscribers_gained": 5,
        }
        score = compute_performance_score(metrics)
        self.assertGreater(score, 500.0)

    def test_record_and_persist_clip_performance(self):
        self.ledger.record_clip_performance(
            "clip-123", "2026-11-20T14:30:00+00:00", "police_chase",
            {"views": 5000, "engaged_views": 3200, "likes": 300, "comments": 40}
        )
        reopened = AnalyticsLedger(self.ledger_file)
        records = reopened.list_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["clip_id"], "clip-123")
        self.assertEqual(records[0]["category"], "police_chase")
        self.assertGreater(records[0]["score"], 1000.0)

    def test_cold_start_recommendation_uses_prior(self):
        # When no analytics exist, scheduler should still return valid recommendations
        rec = self.scheduler.recommend_publish_time(
            category="stunt", epsilon=0.0
        )
        self.assertIn("recommended_time_utc", rec)
        self.assertIn("recommended_time_ist", rec)
        self.assertEqual(rec["strategy"], "exploit_best_slot")
        self.assertEqual(rec["slot_samples"], 0)

    def test_learned_slot_scores_higher_on_exploitation(self):
        # Train a slot that is guaranteed to fall inside the scheduler's
        # next-48-hour candidate horizon. Fixed historical dates become stale.
        target_utc = (datetime.now(timezone.utc) + timedelta(hours=24)).replace(
            minute=30, second=0, microsecond=0
        )
        target_local = target_utc.astimezone(timezone(timedelta(hours=5, minutes=30)))
        for i in range(5):
            self.ledger.record_clip_performance(
                f"clip-trained-{i}", target_utc.isoformat(), "police_chase",
                {"views": 50000, "engaged_views": 35000, "likes": 4000, "comments": 500}
            )

        slots, cat_slots = self.scheduler.get_slot_stats()
        trained_slot_scores = slots.get((target_local.weekday(), target_local.hour), [])
        self.assertEqual(len(trained_slot_scores), 5)
        self.assertGreater(sum(trained_slot_scores) / 5, 10000)

        # Force exploitation
        rec = self.scheduler.recommend_publish_time(category="police_chase", epsilon=0.0)
        self.assertEqual(rec["strategy"], "exploit_best_slot")
        self.assertGreater(rec["estimated_slot_score"], 100)

    def test_exploration_picks_less_sampled_slots(self):
        # Force exploration
        rec = self.scheduler.recommend_publish_time(category="fail", epsilon=1.0)
        self.assertEqual(rec["strategy"], "explore_new_slot")
        self.assertEqual(rec["slot_samples"], 0)


if __name__ == "__main__":
    unittest.main()
