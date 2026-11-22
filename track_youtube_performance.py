"""Hourly best-effort collector for public YouTube video performance."""
import json
import os

from performance_tracker import (
    VideoPerformanceLedger,
    collect_due_snapshots,
    feed_scheduler_from_48h,
)
from smart_scheduler import AnalyticsLedger


def main():
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    tracker = VideoPerformanceLedger("cloud_state/video_performance.json")
    scheduler = AnalyticsLedger("cloud_state/analytics_ledger.json")
    result = collect_due_snapshots(tracker, key)
    result["scheduler_records_added"] = feed_scheduler_from_48h(tracker, scheduler)
    if not key:
        result["notice"] = "YOUTUBE_API_KEY is not configured; no API snapshots were collected."
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
