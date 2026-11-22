import tempfile
import unittest
from pathlib import Path
from content_classifier import classify_clip_content, ClipDeduplicator


class ContentClassifierTests(unittest.TestCase):
    def test_classify_police_chase(self):
        transcript = "The police are on me! Five stars, the cops and helicopter are everywhere!"
        result = classify_clip_content(transcript)
        self.assertEqual(result["category"], "police_chase")
        self.assertGreater(result["confidence"], 0.6)
        self.assertIn("police", result["tags"])

    def test_classify_stunt(self):
        transcript = "Watch this insane ramp jump, perfect airborne landing on the rooftop!"
        result = classify_clip_content(transcript)
        self.assertEqual(result["category"], "stunt")
        self.assertIn("stunt", result["tags"])

    def test_classify_fail_wasted(self):
        chat = [{"text": "WASTED LMAO"}, {"text": "he crashed into the gas station"}]
        transcript = "I exploded into the wall"
        result = classify_clip_content(transcript, chat_messages=chat)
        self.assertEqual(result["category"], "fail")

    def test_classify_funny_moment_with_emojis(self):
        chat = [{"text": "😂😂😂 bro fell off the bike"}, {"text": "dead 💀"}]
        result = classify_clip_content("look at this npc", chat_messages=chat)
        self.assertIn(result["category"], {"funny_moment", "fail", "trolling"})

    def test_classify_close_escape(self):
        transcript = "That was close! We barely survived that clutch rocket!"
        result = classify_clip_content(transcript)
        self.assertEqual(result["category"], "close_escape")

    def test_deduplicator_detects_temporal_overlap(self):
        dedup = ClipDeduplicator(time_overlap_margin=10.0)
        dedup.register_clip(100.0, "unique first moment")

        is_dup, reason = dedup.is_duplicate(105.0, "completely different words here")
        self.assertTrue(is_dup)
        self.assertEqual(reason, "temporal_overlap")

        is_dup, _ = dedup.is_duplicate(120.0, "completely different words here")
        self.assertFalse(is_dup)

    def test_deduplicator_detects_transcript_similarity(self):
        dedup = ClipDeduplicator()
        phrase1 = "holy shit look at that crazy stunt jump over the maze bank"
        phrase2 = "holy shit look at that crazy stunt jump over the maze tower"
        dedup.register_clip(500.0, phrase1)

        # Far away in time (600s), but nearly identical words
        is_dup, reason = dedup.is_duplicate(600.0, phrase2)
        self.assertTrue(is_dup)
        self.assertTrue(reason.startswith("transcript_similarity"))

    def test_temporal_overlap_is_scoped_to_same_source(self):
        dedup = ClipDeduplicator(time_overlap_margin=10.0)
        dedup.register_clip(100.0, "a distinct moment from source one", source_id="UC-one")
        is_dup, _ = dedup.is_duplicate(
            105.0, "completely different words from source two", source_id="UC-two"
        )
        self.assertFalse(is_dup)

    def test_deduplication_history_persists_between_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "dedupe.json"
            first = ClipDeduplicator(state_path=state_path)
            first.register_clip(100.0, "a unique recorded clip transcript", clip_id="clip-a", source_id="UC-one")
            reopened = ClipDeduplicator(state_path=state_path)
            is_dup, reason = reopened.is_duplicate(
                105.0, "different words for the same clip", source_id="UC-one"
            )
            self.assertTrue(is_dup)
            self.assertEqual(reason, "temporal_overlap")


if __name__ == "__main__":
    unittest.main()
