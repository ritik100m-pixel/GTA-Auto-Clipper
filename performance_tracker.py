"""Persist real public YouTube performance snapshots for scheduled uploads.

The tracker records public view/like/comment counts at the first hourly run
after each age milestone (1h, 6h, 24h, 48h). It never invents missing metrics.
"""
import json
import os
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

MILESTONES_HOURS = (1, 6, 24, 48)
API = "https://www.googleapis.com/youtube/v3/videos"


def _now():
    return datetime.now(timezone.utc)


def _parse(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class VideoPerformanceLedger:
    def __init__(self, path="cloud_state/video_performance.json"):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {"version": 1, "videos": {}}
        self.load()

    def load(self):
        try:
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("videos"), dict):
                    self.data = {"version": 1, "videos": loaded["videos"]}
        except (OSError, ValueError, TypeError):
            self.data = {"version": 1, "videos": {}}
        return self

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", dir=self.path.parent, delete=False, encoding="utf-8"
            ) as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                temp_name = handle.name
            os.replace(temp_name, self.path)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)

    def register(self, video_id, published_at, category="general"):
        video_id = str(video_id or "").strip()
        if not video_id:
            return False
        published = _parse(published_at).astimezone(timezone.utc).isoformat()
        existing = self.data["videos"].get(video_id)
        if existing:
            return False
        self.data["videos"][video_id] = {
            "video_id": video_id,
            "published_at": published,
            "category": str(category or "general"),
            "snapshots": {},
        }
        self.save()
        return True

    def due_milestones(self, now=None):
        now = now or _now()
        due = []
        for video in self.data["videos"].values():
            age_hours = (now - _parse(video["published_at"])).total_seconds() / 3600
            snapshots = video.get("snapshots", {})
            for milestone in MILESTONES_HOURS:
                key = str(milestone)
                # Capture the first run after a milestone; allow a 72h catch-up.
                if age_hours >= milestone and age_hours <= milestone + 2 and key not in snapshots:
                    due.append((video["video_id"], milestone))
        return due

    def record_snapshot(self, video_id, milestone_hours, statistics, captured_at=None):
        if str(milestone_hours) not in {str(x) for x in MILESTONES_HOURS}:
            raise ValueError("unsupported performance milestone")
        video = self.data["videos"].get(str(video_id))
        if not video:
            raise KeyError("video is not registered")
        clean = {}
        for key in ("viewCount", "likeCount", "commentCount"):
            value = statistics.get(key)
            if value is not None:
                try:
                    clean[key.removesuffix("Count") + "s"] = int(value)
                except (TypeError, ValueError):
                    pass
        video.setdefault("snapshots", {}).setdefault(str(milestone_hours), {
            "milestone_hours": int(milestone_hours),
            "captured_at": (captured_at or _now()).isoformat(),
            "metrics": clean,
        })
        self.save()
        return video["snapshots"][str(milestone_hours)]


def fetch_video_statistics(video_ids, api_key):
    """Fetch current public statistics in batches of at most 50 video IDs."""
    ids = sorted({str(value).strip() for value in video_ids if str(value).strip()})
    results = {}
    for offset in range(0, len(ids), 50):
        batch = ids[offset:offset + 50]
        params = urllib.parse.urlencode({
            "part": "statistics",
            "id": ",".join(batch),
            "key": api_key,
            "maxResults": len(batch),
        })
        request = urllib.request.Request(
            f"{API}?{params}", headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
        for item in payload.get("items", []):
            results[item["id"]] = item.get("statistics", {})
    return results


def collect_due_snapshots(ledger, api_key, now=None):
    """Collect due snapshots, returning counts without exposing API credentials."""
    due = ledger.due_milestones(now=now)
    if not due or not api_key:
        return {"due": len(due), "recorded": 0, "error": None}
    try:
        stats = fetch_video_statistics([video_id for video_id, _ in due], api_key)
    except Exception as exc:
        return {"due": len(due), "recorded": 0, "error": f"{type(exc).__name__}: {exc}"}
    recorded = 0
    for video_id, milestone in due:
        if video_id in stats:
            ledger.record_snapshot(video_id, milestone, stats[video_id], captured_at=now)
            recorded += 1
    return {"due": len(due), "recorded": recorded, "error": None}


def feed_scheduler_from_48h(ledger, scheduler_ledger):
    """Train posting-slot recommendations only from real 48-hour snapshots."""
    imported = 0
    for video in ledger.data["videos"].values():
        snapshot = video.get("snapshots", {}).get("48")
        if not snapshot or video.get("scheduler_recorded"):
            continue
        metrics = snapshot.get("metrics", {})
        if not metrics:
            continue
        scheduler_ledger.record_clip_performance(
            video["video_id"], video["published_at"], video.get("category", "general"), metrics
        )
        video["scheduler_recorded"] = True
        imported += 1
    if imported:
        ledger.save()
    return imported
