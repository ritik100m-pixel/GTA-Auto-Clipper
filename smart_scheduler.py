"""Analytics-driven upload scheduling and reinforcement model for GTA Shorts.

Learns high-engagement publication windows and content-slot affinities using
actual channel performance data (views, stayed-to-watch ratio, engaged views,
likes, comments). Uses epsilon-greedy exploration to discover new audience peaks.
"""
import json
import os
import random
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

# Baseline viewing priors for GTA gaming audience (IST local hour: prior weight)
# Peak gaming engagement occurs in early evening and night hours.
# Cold-start slots are deliberately neutral. Only measured channel results
# should create a preferred hour or weekday; no generic audience peak is assumed.
DEFAULT_HOUR_PRIORS = {hour: 1.0 for hour in range(24)}


def _utc_now():
    return datetime.now(timezone.utc)


def compute_performance_score(metrics):
    """Calculate normalized engagement score from actual YouTube analytics."""
    if not metrics:
        return 0.0
    views = float(metrics.get("views", 0))
    engaged = float(metrics.get("engaged_views", 0))
    stayed_ratio = float(metrics.get("stayed_to_watch_ratio", 0.0))  # 0.0 to 1.0
    likes = float(metrics.get("likes", 0))
    comments = float(metrics.get("comments", 0))
    subs = float(metrics.get("subscribers_gained", 0))

    # Balanced engagement composite
    score = (
        views * 0.35 +
        engaged * 0.30 +
        (stayed_ratio * 100.0) * 0.15 +
        (likes * 2.0 + comments * 5.0) * 0.15 +
        subs * 10.0 * 0.05
    )
    return round(score, 3)


class AnalyticsLedger:
    """Persistent storage for historical clip performance metrics."""

    def __init__(self, ledger_path="cloud_state/analytics_ledger.json"):
        self.ledger_path = Path(ledger_path).resolve()
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.records = {}
        self.load()

    def load(self):
        if self.ledger_path.is_file():
            try:
                with open(self.ledger_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self.records = data.get("records", {})
            except (json.JSONDecodeError, OSError):
                self.records = {}
        else:
            self.records = {}
        return self

    def save(self):
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        dir_name = self.ledger_path.parent
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
            json.dump({
                "version": 1,
                "updated_at": _utc_now().isoformat(),
                "records": self.records,
            }, tf, indent=2, ensure_ascii=False)
            temp_name = tf.name
        os.replace(temp_name, self.ledger_path)

    def record_clip_performance(self, clip_id, published_at_iso, category, metrics):
        """Save verified metrics for a published clip."""
        cid = str(clip_id).strip()
        pub_dt = datetime.fromisoformat(published_at_iso.replace("Z", "+00:00"))
        pub_local = pub_dt.astimezone(IST)

        score = compute_performance_score(metrics)
        self.records[cid] = {
            "clip_id": cid,
            "published_at": pub_dt.isoformat(),
            "local_day": pub_local.weekday(),  # 0=Monday, 6=Sunday
            "local_hour": pub_local.hour,
            "category": str(category or "general"),
            "metrics": dict(metrics),
            "score": score,
            "updated_at": _utc_now().isoformat(),
        }
        self.save()
        return self.records[cid]

    def list_records(self):
        return list(self.records.values())


class SmartScheduler:
    """Determines optimal publication times using exploration-exploitation."""

    def __init__(self, ledger=None, ledger_path="cloud_state/analytics_ledger.json"):
        self.ledger = ledger if ledger is not None else AnalyticsLedger(ledger_path)

    def get_slot_stats(self):
        """Aggregate performance history by (local_day, local_hour) and category."""
        slots = defaultdict(list)
        cat_slots = defaultdict(list)

        for rec in self.ledger.list_records():
            day = rec.get("local_day")
            hour = rec.get("local_hour")
            score = rec.get("score", 0.0)
            cat = rec.get("category", "general")

            if day is not None and hour is not None:
                slots[(day, hour)].append(score)
                cat_slots[(cat, day, hour)].append(score)

        return slots, cat_slots

    def score_candidate_slot(self, dt_candidate, category=None, slots=None, cat_slots=None):
        """Score a specific candidate datetime based on analytics and priors."""
        local_dt = dt_candidate.astimezone(IST)
        day = local_dt.weekday()
        hour = local_dt.hour

        prior = DEFAULT_HOUR_PRIORS.get(hour, 1.0)
        if slots is None or cat_slots is None:
            slots, cat_slots = self.get_slot_stats()

        slot_scores = slots.get((day, hour), [])
        cat_scores = cat_slots.get((category, day, hour), []) if category else []

        if not slot_scores:
            # Cold slot: rely on prior
            return prior * 10.0, 0

        avg_slot = sum(slot_scores) / len(slot_scores)
        if cat_scores:
            avg_cat = sum(cat_scores) / len(cat_scores)
            combined = avg_slot * 0.6 + avg_cat * 0.4
        else:
            combined = avg_slot

        # Confidence weighting: more historical samples increase certainty
        sample_count = len(slot_scores)
        weight = min(1.0, sample_count / 5.0)
        final_score = combined * weight + (prior * 10.0) * (1.0 - weight)
        return round(final_score, 2), sample_count

    def recommend_publish_time(self, category=None, earliest_time=None,
                               horizon_hours=48, epsilon=0.20,
                               existing_scheduled=None):
        """Recommend optimal publication timestamp within the horizon window."""
        now = _utc_now()
        start = earliest_time if earliest_time else (now + timedelta(minutes=45))
        if start < now:
            start = now + timedelta(minutes=15)

        # Generate candidate hourly slots across the horizon
        start_rounded = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        candidates = [start_rounded + timedelta(hours=i) for i in range(horizon_hours)]

        # Exclude slots that already have a scheduled clip within 2 hours
        reserved_slots = set()
        if existing_scheduled:
            for s in existing_scheduled:
                try:
                    s_dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
                    for delta in (-1, 0, 1):
                        reserved_slots.add(s_dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=delta))
                except (ValueError, TypeError):
                    pass

        valid_candidates = [c for c in candidates if c not in reserved_slots]
        if not valid_candidates:
            valid_candidates = candidates

        slots, cat_slots = self.get_slot_stats()

        # Epsilon-greedy decision
        is_exploring = random.random() < float(epsilon)

        if is_exploring:
            # Explore: select from candidates with low observations in favorable overall periods
            candidate_counts = [(c, self.score_candidate_slot(c, category, slots, cat_slots))
                                for c in valid_candidates]
            # Prioritize least-observed slots that meet minimum baseline
            candidate_counts.sort(key=lambda item: (item[1][1], -item[1][0]))
            chosen, (score, samples) = candidate_counts[0]
            strategy = "explore_new_slot"
        else:
            # Exploit: select highest-scoring slot
            candidate_scores = [(c, self.score_candidate_slot(c, category, slots, cat_slots))
                                for c in valid_candidates]
            candidate_scores.sort(key=lambda item: item[1][0], reverse=True)
            chosen, (score, samples) = candidate_scores[0]
            strategy = "exploit_best_slot"

        chosen_ist = chosen.astimezone(IST)
        return {
            "recommended_time_utc": chosen.isoformat(),
            "recommended_time_ist": chosen_ist.isoformat(),
            "strategy": strategy,
            "estimated_slot_score": score,
            "slot_samples": samples,
            "category": category,
            "local_day": chosen_ist.strftime("%A"),
            "local_hour": chosen_ist.hour,
        }
