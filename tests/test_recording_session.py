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
            chunk["id"], "worker-a", [(video, metadata)]
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
            chunk["id"], "worker-old", [(video, metadata)]
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

    def test_worker_can_renew_its_lease(self):
        _, _, chunk = self._chunk()
        claimed = self.session.claim_next("worker-a")
        self.assertEqual(claimed["id"], chunk["id"])

        old_expiry = claimed["lease_expires_at"]
        self.assertTrue(self.session.renew_lease(
            chunk["id"], "worker-a", lease_seconds=1800
        ))

        renewed = self.session.get_chunk(chunk["id"])
        self.assertGreater(renewed["lease_expires_at"], old_expiry)

    def test_expired_worker_cannot_renew_lease(self):
        _, _, chunk = self._chunk()
        self.session.claim_next("worker-old")

        with self.session._db() as db:
            db.execute(
                "UPDATE chunks SET lease_expires_at = ? WHERE id = ?",
                (_stamp(datetime.now(timezone.utc) - timedelta(seconds=5)),
                 chunk["id"])
            )

        self.assertFalse(self.session.renew_lease(
            chunk["id"], "worker-old"
        ))

    def test_chunk_can_complete_with_multiple_output_pairs(self):
        _, _, chunk = self._chunk()
        self.session.claim_next("worker-a")

        video1 = Path(self.temp.name) / "clip-1.mp4"
        metadata1 = Path(self.temp.name) / "clip-1.json"
        video2 = Path(self.temp.name) / "clip-2.mp4"
        metadata2 = Path(self.temp.name) / "clip-2.json"

        for path in (video1, metadata1, video2, metadata2):
            path.write_bytes(b"test-output")

        self.assertTrue(self.session.complete_chunk(
            chunk["id"], "worker-a",
            [(video1, metadata1), (video2, metadata2)]
        ))

        saved = self.session.get_chunk(chunk["id"])
        self.assertEqual(saved["status"], "completed")

        import json
        outputs = json.loads(saved["outputs_json"])
        self.assertEqual(len(outputs), 2)
        self.assertEqual(outputs[0]["video"], str(video1.resolve()))
        self.assertEqual(outputs[1]["metadata"], str(metadata2.resolve()))

    def test_chunk_can_complete_with_zero_outputs_and_prunes_raw_media(self):
        _, raw_chunk, chunk = self._chunk()
        self.session.claim_next("worker-a")

        self.assertTrue(self.session.complete_chunk(
            chunk["id"], "worker-a", []
        ))

        saved = self.session.get_chunk(chunk["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["outputs_json"], "[]")
        self.assertFalse(raw_chunk.exists())

    def test_chunk_becomes_failed_after_maximum_attempts(self):
        _, _, chunk = self._chunk()

        for attempt in range(3):
            claimed = self.session.claim_next(f"worker-{attempt}")
            self.assertEqual(claimed["id"], chunk["id"])
            self.assertTrue(self.session.fail_chunk(
                chunk["id"], f"worker-{attempt}", "simulated failure"
            ))

        saved = self.session.get_chunk(chunk["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertIsNone(self.session.claim_next("worker-final"))
        self.assertEqual(self.session.list_recoverable(), [])

    def test_resolve_chunk_path_matches_original_file(self):
        capture, path, chunk = self._chunk()
        resolved_by_id = self.session.resolve_chunk_path(chunk["id"])
        resolved_by_dict = self.session.resolve_chunk_path(chunk)
        self.assertEqual(resolved_by_id, path.resolve())
        self.assertEqual(resolved_by_dict, path.resolve())

    def test_resolve_chunk_path_rejects_unknown(self):
        with self.assertRaises(ValueError):
            self.session.resolve_chunk_path("00000000-0000-0000-0000-000000000000")

    def test_get_and_list_captures(self):
        first = self.session.new_capture()
        second = self.session.new_capture()
        captures = self.session.list_captures()
        self.assertEqual(len(captures), 2)
        self.assertEqual(captures[0]["id"], first["id"])
        self.assertEqual(captures[1]["id"], second["id"])
        fetched = self.session.get_capture(first["id"])
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["id"], first["id"])
        self.assertEqual(fetched["directory"], captures[0]["directory"])

    def test_recover_unregistered_chunks_discovers_unregistered_files(self):
        capture = self.session.new_capture()
        chunk_file = Path(capture["path"]) / "chunk_00001.ts"
        chunk_file.write_bytes(b"recovered-video-data")

        # An empty file should be ignored
        empty_file = Path(capture["path"]) / "chunk_00002.ts"
        empty_file.touch()

        recovered = self.session.recover_unregistered_chunks(capture["id"])
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["relative_path"], "chunk_00001.ts")
        self.assertEqual(recovered[0]["status"], "pending")

        # Second recovery should not duplicate already registered files
        again = self.session.recover_unregistered_chunks(capture["id"])
        self.assertEqual(again, [])

        # Recoverable list should now include the newly discovered chunk
        recoverable = self.session.list_recoverable()
        self.assertTrue(any(c["id"] == recovered[0]["id"] for c in recoverable))


if __name__ == "__main__":
    unittest.main()