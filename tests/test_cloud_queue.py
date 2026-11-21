from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cloud_queue import CloudQueue, iso_time


class CloudQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "queue.sqlite3"
        self.queue = CloudQueue(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_job_key_is_not_duplicated(self):
        first = self.queue.enqueue(
            "capture", "stream-001", {"url": "https://example.com/live"}
        )
        second = self.queue.enqueue(
            "capture", "stream-001", {"url": "https://example.com/other"},
            authorized=True
        )
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(second["authorized"])

    def test_unauthorized_job_cannot_be_claimed(self):
        self.queue.enqueue("capture", "unauthorized-1", {"url": "test"})
        self.assertIsNone(self.queue.claim_next("worker-a"))

    def test_authorized_job_can_be_claimed_and_completed(self):
        job = self.queue.enqueue(
            "capture", "authorized-1", {"url": "test"}, authorized=True
        )
        claimed = self.queue.claim_next("worker-a")
        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["status"], "running")
        self.assertTrue(self.queue.heartbeat(job["id"], "worker-a"))
        self.assertTrue(self.queue.complete(job["id"], "worker-a"))
        self.assertEqual(self.queue.get_job(job["id"])["status"], "completed")

    def test_failed_job_can_be_retried(self):
        job = self.queue.enqueue(
            "capture", "retry-1", {"url": "test"},
            authorized=True, max_attempts=2
        )
        claimed = self.queue.claim_next("worker-a")
        self.assertEqual(claimed["id"], job["id"])
        self.assertTrue(self.queue.fail(
            job["id"], "worker-a", "temporary failure", retry_delay_seconds=0
        ))

        with self.queue._db() as db:
            db.execute(
                "UPDATE jobs SET available_at = ? WHERE id = ?",
                (iso_time(datetime.now(timezone.utc) - timedelta(seconds=5)),
                 job["id"])
            )

        retry = self.queue.claim_next("worker-b")
        self.assertEqual(retry["id"], job["id"])
        self.assertEqual(retry["attempts"], 2)

        self.assertTrue(self.queue.fail(
            job["id"], "worker-b", "second failure", retry_delay_seconds=0
        ))
        self.assertEqual(self.queue.get_job(job["id"])["status"], "failed")

    def test_expired_lease_can_be_recovered(self):
        job = self.queue.enqueue(
            "capture", "lease-1", {"url": "test"}, authorized=True
        )
        first = self.queue.claim_next("worker-a", lease_seconds=900)
        self.assertEqual(first["id"], job["id"])

        with self.queue._db() as db:
            db.execute(
                "UPDATE jobs SET lease_expires_at = ? WHERE id = ?",
                (iso_time(datetime.now(timezone.utc) - timedelta(seconds=10)),
                 job["id"])
            )

        recovered = self.queue.claim_next("worker-b")
        self.assertEqual(recovered["id"], job["id"])
        self.assertEqual(recovered["lease_owner"], "worker-b")
        self.assertEqual(recovered["attempts"], 2)

    def test_batch_survives_database_reopen(self):
        batch_id = self.queue.start_batch("worker-a", duration_seconds=21600)
        reopened = CloudQueue(self.db_path)
        batches = reopened.list_batches()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["id"], batch_id)
        self.assertTrue(reopened.finish_batch(batch_id))

        reopened_again = CloudQueue(self.db_path)
        self.assertEqual(
            reopened_again.list_batches()[0]["status"], "completed"
        )


    def test_expired_worker_cannot_heartbeat_complete_or_fail(self):
        job = self.queue.enqueue(
            "capture", "stale-worker-1", {"url": "test"},
            authorized=True
        )
        self.queue.claim_next("worker-old")

        with self.queue._db() as db:
            db.execute(
                "UPDATE jobs SET lease_expires_at = ? WHERE id = ?",
                (iso_time(datetime.now(timezone.utc) - timedelta(seconds=5)),
                 job["id"])
            )

        self.assertFalse(self.queue.heartbeat(job["id"], "worker-old"))
        self.assertFalse(self.queue.complete(job["id"], "worker-old"))
        self.assertFalse(self.queue.fail(job["id"], "worker-old", "stale"))

        recovered = self.queue.claim_next("worker-new")
        self.assertEqual(recovered["id"], job["id"])
        self.assertEqual(recovered["lease_owner"], "worker-new")

    def test_two_workers_cannot_claim_the_same_job(self):
        self.queue.enqueue(
            "capture", "race-job-1", {"url": "test"}, authorized=True
        )
        second_queue = CloudQueue(self.db_path)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda item: item[0].claim_next(item[1]),
                [(self.queue, "worker-a"), (second_queue, "worker-b")]
            ))

        claimed = [job for job in results if job is not None]
        empty = [job for job in results if job is None]

        self.assertEqual(len(claimed), 1)
        self.assertEqual(len(empty), 1)
        self.assertEqual(claimed[0]["job_key"], "race-job-1")
if __name__ == "__main__":
    unittest.main()
