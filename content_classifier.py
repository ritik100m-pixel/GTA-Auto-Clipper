"""GTA content categorization, event detection, and clip deduplication.

Classifies gameplay clips into distinct GTA content categories (police chases,
stunts, funny moments, fails, close escapes, etc.) and guards against
duplicate or overlapping clips across chunks and recording sessions.
"""
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path


CATEGORIES = {
    "police_chase": {
        "keywords": {"police", "cop", "cops", "chase", "chasing", "busted", "siren", "stars", "feds", "swat", "pursuit", "helicopter", "patrol"},
        "chat": {"cops", "busted", "5 stars", "stars", "chase", "run", "feds", "siren", "police", "escaped"},
        "weight": 1.4,
    },
    "stunt": {
        "keywords": {"stunt", "jump", "jumping", "flip", "landing", "ramp", "parachute", "jet", "plane", "rooftop", "airborne", "drift", "wingsuit"},
        "chat": {"stunt", "jump", "flip", "insane jump", "smooth", "landed", "drift", "clean"},
        "weight": 1.3,
    },
    "fail": {
        "keywords": {"fail", "wasted", "died", "dead", "crashed", "wrecked", "missed", "exploded", "drowned", "fell", "hit", "slipped", "ruined"},
        "chat": {"wasted", "fail", "lmao wasted", "rip", "oof", "dead", "died", "rekt", "crash"},
        "weight": 1.3,
    },
    "funny_moment": {
        "keywords": {"funny", "hilarious", "laugh", "laughing", "joke", "ridiculous", "dumb", "clown", "stupid", "bruh"},
        "chat": {"lol", "lmao", "rofl", "haha", "hahaha", "xd", "😂", "🤣", "💀", "bruh", "kekw", "lul"},
        "weight": 1.2,
    },
    "close_escape": {
        "keywords": {"close", "clutch", "survived", "barely", "escape", "escaped", "saved", "almost", "dodged", "narrow"},
        "chat": {"that was close", "clutch", "close", "survived", "barely", "how", "pog", "poggers", "monkas", "😱", "😮"},
        "weight": 1.4,
    },
    "trolling": {
        "keywords": {"troll", "trolling", "prank", "npc", "messing", "punch", "steal", "runover", "tease", "distract"},
        "chat": {"troll", "npc", "poor guy", "evil", "menace", "why", "lmao"},
        "weight": 1.1,
    },
    "story": {
        "keywords": {"mission", "cutscene", "dialogue", "lucia", "jason", "story", "heist", "boss", "target", "objective"},
        "chat": {"story", "cutscene", "lore", "heist", "mission", "cinema"},
        "weight": 1.0,
    },
    "high_action": {
        "keywords": {"insane", "crazy", "boom", "explosion", "shoot", "shooting", "rocket", "gunfight", "heist", "chaos", "war"},
        "chat": {"insane", "boom", "holy shit", "clip it", "chaos", "fire", "hype", "wild"},
        "weight": 1.2,
    },
}

DEFAULT_CATEGORY = "high_action"


def _clean_tokens(text):
    if not text:
        return []
    words = re.findall(r"[A-Za-z0-9]+|[\U00010000-\U0010ffff]", str(text).lower())
    return [w for w in words if len(w) > 1 or ord(w[0]) > 255]


def classify_clip_content(transcript="", chat_messages=None, audio_spike=0.0, stream_title="", words=None, **kwargs):
    """Classify clip into GTA content categories based on speech, chat, and audio."""
    if not transcript and words:
        try:
            transcript = " ".join(str(w[2]) for w in words if len(w) >= 3)
        except Exception:
            pass
    transcript_tokens = _clean_tokens(transcript)
    if stream_title:
        transcript_tokens.extend(_clean_tokens(stream_title))
    chat_tokens = []
    if chat_messages:
        for msg in chat_messages:
            text = msg.get("text", "") if isinstance(msg, dict) else str(msg)
            chat_tokens.extend(_clean_tokens(text))

    t_counts = Counter(transcript_tokens)
    c_counts = Counter(chat_tokens)

    scores = {}
    matches_by_cat = {}

    for cat, rules in CATEGORIES.items():
        kw_hits = sum(t_counts[k] for k in rules["keywords"] if k in t_counts)
        chat_hits = sum(c_counts[k] for k in rules["chat"] if k in c_counts)
        matched_words = [k for k in rules["keywords"] if k in t_counts] + [k for k in rules["chat"] if k in c_counts]

        raw_score = (kw_hits * 1.5 + chat_hits * 1.0) * rules["weight"]
        scores[cat] = round(raw_score, 2)
        matches_by_cat[cat] = matched_words

    best_cat, best_score = max(scores.items(), key=lambda pair: pair[1])

    if best_score < 1.0:
        if audio_spike >= 1.5:
            best_cat = "high_action"
            confidence = 0.55
        else:
            best_cat = "funny_moment" if any(w in transcript_tokens for w in ("crazy", "what", "no")) else DEFAULT_CATEGORY
            confidence = 0.40
    else:
        confidence = min(0.95, round(0.5 + min(best_score, 5.0) * 0.09, 2))

    detected_tags = list(dict.fromkeys(["gta", "gtav", "gta6", best_cat.replace("_", "")] + matches_by_cat.get(best_cat, [])[:4]))

    return {
        "category": best_cat,
        "confidence": confidence,
        "tags": detected_tags[:8],
        "category_scores": scores,
    }


class ClipDeduplicator:
    """Detect duplicate clips in the same source and persist history between batches."""

    def __init__(self, time_overlap_margin=15.0, transcript_similarity_threshold=0.70,
                 state_path=None, max_records=5000):
        self.time_overlap_margin = float(time_overlap_margin)
        self.transcript_similarity_threshold = float(transcript_similarity_threshold)
        self.state_path = Path(state_path).resolve() if state_path else None
        self.max_records = max(1, int(max_records))
        self.seen_clips = []
        self.load()

    def _token_set(self, text):
        # Keep only one-way token hashes in the cross-run ledger. The public
        # repository cache must not contain raw creator transcripts.
        return {
            hashlib.sha256(token.encode("utf-8")).hexdigest()
            for token in _clean_tokens(text)
        }

    def _jaccard(self, set_a, set_b):
        if not set_a and not set_b:
            return 0.0
        union = len(set_a | set_b)
        if union == 0:
            return 0.0
        return len(set_a & set_b) / union

    def load(self):
        if not self.state_path or not self.state_path.is_file():
            return self
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            records = data.get("clips", []) if isinstance(data, dict) else []
            self.seen_clips = [
                {
                    "start_epoch": row.get("start_epoch"),
                    "tokens": set(row.get("tokens", [])),
                    "clip_id": row.get("clip_id"),
                    "source_id": row.get("source_id"),
                }
                for row in records[-self.max_records:]
                if isinstance(row, dict)
            ]
        except (OSError, json.JSONDecodeError, TypeError):
            # Corrupt state must not stop clipping; start with an empty ledger.
            self.seen_clips = []
        return self

    def save(self):
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "clips": [
                {
                    "start_epoch": row.get("start_epoch"),
                    "tokens": sorted(row.get("tokens", set())),
                    "clip_id": row.get("clip_id"),
                    "source_id": row.get("source_id"),
                }
                for row in self.seen_clips[-self.max_records:]
            ],
        }
        with tempfile.NamedTemporaryFile(
            "w", dir=self.state_path.parent, delete=False, encoding="utf-8"
        ) as tf:
            json.dump(payload, tf, indent=2, ensure_ascii=False)
            temp_name = tf.name
        os.replace(temp_name, self.state_path)

    def is_duplicate(self, start_epoch, transcript="", source_id=None):
        tokens = self._token_set(transcript)
        for prev in self.seen_clips:
            # Timestamp proximity is meaningful only within the same source.
            same_source = source_id is None or prev.get("source_id") == source_id
            if same_source and start_epoch is not None and prev.get("start_epoch") is not None:
                if abs(float(start_epoch) - float(prev["start_epoch"])) < self.time_overlap_margin:
                    return True, "temporal_overlap"

            # Similar transcripts can identify reposted moments across sources.
            prev_tokens = prev.get("tokens", set())
            if len(tokens) >= 5 and len(prev_tokens) >= 5:
                sim = self._jaccard(tokens, prev_tokens)
                if sim >= self.transcript_similarity_threshold:
                    return True, f"transcript_similarity_{sim:.2f}"

        return False, None

    def register_clip(self, start_epoch, transcript="", clip_id=None, source_id=None):
        self.seen_clips.append({
            "start_epoch": float(start_epoch) if start_epoch is not None else None,
            "tokens": self._token_set(transcript),
            "clip_id": clip_id,
            "source_id": source_id,
        })
        if len(self.seen_clips) > self.max_records:
            self.seen_clips = self.seen_clips[-self.max_records:]
        self.save()
