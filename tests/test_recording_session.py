import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from recording_session import RecordingSession, _stamp


class RecordingSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.session = RecordingSession(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _chunk(self):
        capture = self.session.new_capture()
        path = Path(capture["path"]) / "chunk_00000.ts"
        path.write_bytes(b"test-video-data")
        chunk = self.session.register_chunk(capture["id"], path)
        return capture, path, chunk

    def test_capture_directories_are_unique(self):
        first = self.session.new_capture()
        second = self.session.new_capture()
        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue(Path(first["path"]).is_dir())
        self.assertTrue(Path(second["path"]).is_dir())

    def test_chunk_registration_survives_reopen(self):
        _, _, chunk = self._chunk()
        reopened = RecordingSession(self.temp.name, self.session.session_id)
        saved = reopened.get_chunk(chunk["id"])
        self.assertEqual(saved["status"], "pending")

    def test_chunk_must_be_inside_capture_and_nonempty(self):
        capture = self.session.new_capture()
        outside = Path(self.temp.name) / "outside.ts"
        outside.write_bytes(b"not-in-capture")
        with self.assertRaises(ValueError):
            self.session.register_chunk(capture["id"], outside)

        empty = Path(capture["path"]) / "empty.ts"
        empty.touch()
        with self.assertRaises(ValueError):
            self.session.register_chunk(capture["id"], empty)

    def test_worker_claim_and_completion_require_both_outputs(self):
        _, _, chunk = self._chunk()
        claimed = self.session.claim_next("worker-a")
        self.assertEqual(claimed["id"], chunk["id"])

        video = Path(self.temp.name) / "clip.mp4"
        metadata = Path(self.temp.name) / "clip.json"
        video.write_bytes(b"mp4")
        metadata.write_text('{"approved": false}', encoding="utf-8")

        self.assertTrue(self.session.complete_chunk(
            chunk["id"], "worker-a", video, metadata
        ))
        self.assertEqual(self.session.get_chunk(chunk["id"])["status"], "completed")

    def test_expired_worker_cannot_complete_and_another_can_recover(self):
        _, _, chunk = self._chunk()
        self.session.claim_next("worker-old")

        with self.session._db() as db:
            db.execute(
                "UPDATE chunks SET lease_expires_at = ? WHERE id = ?",
                (_stamp(datetime.now(timezone.utc) - timedelta(seconds=5)),
                 chunk["id"])
            )

        video = Path(self.temp.name) / "clip.mp4"
        metadata = Path(self.temp.name) / "clip.json"
        video.write_bytes(b"mp4")
        metadata.write_text("{}", encoding="utf-8")

        self.assertFalse(self.session.complete_chunk(
            chunk["id"], "worker-old", video, metadata
        ))
        recovered = self.session.claim_next("worker-new")
        self.assertEqual(recovered["id"], chunk["id"])
        self.assertEqual(recovered["lease_owner"], "worker-new")

    def test_failed_chunk_can_be_retried(self):
        _, _, chunk = self._chunk()
        claimed = self.session.claim_next("worker-a")
        self.assertEqual(claimed["id"], chunk["id"])
        self.assertTrue(self.session.fail_chunk(chunk["id"], "worker-a", "test error"))
        self.assertEqual(self.session.get_chunk(chunk["id"])["status"], "pending")
        self.assertEqual(self.session.claim_next("worker-b")["id"], chunk["id"])

    def test_two_workers_cannot_claim_one_chunk(self):
        from concurrent.futures import ThreadPoolExecutor

        self._chunk()
        other = RecordingSession(self.temp.name, self.session.session_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda item: item[0].claim_next(item[1]),
                [(self.session, "worker-a"), (other, "worker-b")]
            ))
        self.assertEqual(sum(item is not None for item in results), 1)


if __name__ == "__main__":
    unittest.main()