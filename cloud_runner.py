"""Master cloud automation orchestrator for GTA Auto Clipper Cloud.

Runs a scheduled cloud batch:
1. Queries authorized sources and finds live gameplay streams.
2. Enqueues authorized jobs into CloudQueue.
3. Leases jobs and drives crash-resilient recording session.
4. Categorizes clips and deduplicates content.
5. Recommends publication schedules via SmartScheduler analytics.
6. Publishes/schedules clips via UnifiedPublisher.
7. Manages clean retention and records batch ledger execution statistics.
"""
import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cloud_queue import CloudQueue
from source_policy import SourcePolicy
from smart_scheduler import SmartScheduler
from publisher import UnifiedPublisher
from content_classifier import classify_clip_content, ClipDeduplicator
from clip import (
    source_identity, stream_info, recording_session_id,
    process_chunk, run_recovery, start_recorder,
)
from recording_session import RecordingSession


def _utc_now():
    return datetime.now(timezone.utc)


class CloudBatchRunner:
    def __init__(self, state_dir="cloud_state", output_dir="output", recordings_dir="recordings"):
        self.state_dir = Path(state_dir).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.recordings_dir = Path(recordings_dir).resolve()

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

        self.queue = CloudQueue(self.state_dir / "queue.sqlite3")
        self.policy = SourcePolicy(self.state_dir / "source_allowlist.json")
        self.scheduler = SmartScheduler(ledger_path=str(self.state_dir / "analytics_ledger.json"))
        self.publisher = UnifiedPublisher()
        self.deduplicator = ClipDeduplicator()

        self.worker_id = f"worker-cloud-{uuid.uuid4().hex[:8]}"

    def run_discovery_and_enqueue(self, query="GTA 6 gameplay", max_results=10, api_key=None):
        """Find streams, enforce copyright allowlist, and enqueue authorized streams."""
        key = api_key or os.environ.get("YOUTUBE_API_KEY")
        if not key:
            print("Notice: YOUTUBE_API_KEY not set; skipping live stream search.")
            return []

        from find_streams import find_streams
        try:
            candidates = find_streams(query, key, limit=max_results)
        except Exception as exc:
            print(f"Stream search encountered error: {exc}")
            return []

        authorized, unauthorized = self.policy.filter_streams(candidates, required_scope="clip")
        print(f"Discovery found {len(candidates)} streams: {len(authorized)} authorized, {len(unauthorized)} unauthorized.")

        enqueued_jobs = []
        for stream in authorized:
            job_key = f"youtube-live:{stream['channel_id']}:{stream['url']}"
            job = self.queue.enqueue(
                job_type="capture_and_clip",
                job_key=job_key,
                payload={
                    "url": stream["url"],
                    "title": stream.get("title"),
                    "channel_id": stream.get("channel_id"),
                    "channel": stream.get("channel"),
                    "credit": stream.get("source_record", {}).get("credit") or stream.get("channel"),
                },
                authorized=True,
                max_attempts=3,
            )
            enqueued_jobs.append(job)

        return enqueued_jobs

    def run_batch(self, max_duration_seconds=21600, target_clips=10,
                  single_url=None, dry_run=False, unattended=True):
        """Execute one complete automated cloud batch cycle within execution bounds."""
        batch_id = self.queue.start_batch(self.worker_id, duration_seconds=max_duration_seconds)
        start_time = time.time()
        print(f"=== Starting Cloud Batch {batch_id} (Worker: {self.worker_id}) ===")

        stats = {
            "batch_id": batch_id,
            "worker_id": self.worker_id,
            "jobs_claimed": 0,
            "clips_generated": 0,
            "clips_published": 0,
            "errors": [],
        }

        try:
            # Step 1: If direct URL provided or fallback discovery
            if single_url:
                job_key = f"direct-stream:{single_url}"
                job = self.queue.enqueue(
                    "capture_and_clip", job_key, {"url": single_url, "title": "Direct Source"},
                    authorized=True
                )

            # Step 2: Work the queue
            while time.time() - start_time < (max_duration_seconds - 300):
                if stats["clips_generated"] >= target_clips:
                    print(f"Target of {target_clips} clips reached for this batch.")
                    break

                job = self.queue.claim_next(self.worker_id, lease_seconds=1200)
                if not job:
                    print("No more authorized jobs available in the queue.")
                    break

                stats["jobs_claimed"] += 1
                job_id = job["id"]
                payload = job.get("payload", {})
                url = payload.get("url")

                print(f"Claimed job {job_id} for stream: {url}")
                try:
                    # Determine stable session
                    channel_id = payload.get("channel_id")
                    sess_id = recording_session_id(channel_id or url)
                    session = RecordingSession(self.recordings_dir, session_id=sess_id)

                    # Recover any interrupted work
                    session.recover_unregistered_chunks()
                    rec_outputs, rec_count = run_recovery(
                        session, self.worker_id, str(self.output_dir),
                        {"name": payload.get("channel", "Streamer"), "credit": payload.get("credit")},
                        payload.get("title", "GTA Live Stream"),
                        url,
                    )
                    stats["clips_generated"] += rec_count

                    # If time remains, capture more chunks (e.g. up to remaining time)
                    remaining = max_duration_seconds - (time.time() - start_time)
                    if remaining > 600 and stats["clips_generated"] < target_clips:
                        capture = session.new_capture()
                        wdir = capture["path"]
                        capture_id = capture["id"]

                        # Start recorder for up to remaining time
                        rec = start_recorder(url, None, chunk=90, max_seconds=min(int(remaining - 300), 7200), outdir=wdir)
                        registered = set()

                        while rec.poll() is None or session.list_recoverable():
                            self.queue.heartbeat(job_id, self.worker_id, lease_seconds=1200)
                            chunks = sorted(Path(wdir).glob("chunk_*.ts"))
                            finished = [str(p) for p in (chunks if rec.poll() is not None else chunks[:-1])]

                            for c in finished:
                                if c not in registered:
                                    try:
                                        session.register_chunk(capture_id, c)
                                        registered.add(c)
                                    except ValueError:
                                        pass

                            claimed_chunk = session.claim_next(self.worker_id)
                            if not claimed_chunk:
                                if rec.poll() is not None:
                                    break
                                time.sleep(3)
                                continue

                            outputs, count = process_chunk(
                                session, claimed_chunk, self.worker_id, str(self.output_dir),
                                {"name": payload.get("channel", "Streamer"), "credit": payload.get("credit")},
                                payload.get("title", "GTA Stream"), url,
                            )
                            stats["clips_generated"] += count

                            if stats["clips_generated"] >= target_clips:
                                if rec.poll() is None:
                                    rec.terminate()
                                break

                    self.queue.complete(job_id, self.worker_id)

                except Exception as exc:
                    err_msg = f"Job {job_id} failed: {exc}"
                    print(err_msg)
                    stats["errors"].append(err_msg)
                    self.queue.fail(job_id, self.worker_id, str(exc))

            # Step 3: Publish and schedule generated clips
            for meta_path in sorted(self.output_dir.glob("*.json")):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    if meta.get("youtube_video_id") or meta.get("zernio_post_id") or meta.get("dry_run_published"):
                        continue  # Already scheduled or published

                    cat = meta.get("category", "high_action")
                    rec_slot = self.scheduler.recommend_publish_time(category=cat, epsilon=0.20)
                    scheduled_time = rec_slot["recommended_time_utc"]

                    pub_res = self.publisher.publish_clip(
                        meta_path, scheduled_for=scheduled_time, dry_run=dry_run, unattended=unattended
                    )
                    stats["clips_published"] += 1
                    print(f"Scheduled clip {meta_path.name} for {scheduled_time} ({rec_slot['strategy']})")
                except Exception as exc:
                    err = f"Failed to schedule clip {meta_path.name}: {exc}"
                    print(err)
                    stats["errors"].append(err)

            self.queue.finish_batch(batch_id)
            print(f"=== Cloud Batch {batch_id} Finished Successfully ===")
            print(json.dumps(stats, indent=2))
            return stats

        except Exception as exc:
            err_msg = f"Batch failed with unexpected error: {exc}"
            print(err_msg)
            self.queue.finish_batch(batch_id, error=err_msg)
            stats["errors"].append(err_msg)
            return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=21600, help="max batch duration in seconds (default: 6h / 21600)")
    parser.add_argument("--target-clips", type=int, default=10, help="target quality clips per batch")
    parser.add_argument("--query", default="GTA 6 gameplay", help="search query for stream discovery")
    parser.add_argument("--url", default=None, help="direct stream URL override")
    parser.add_argument("--dry-run", action="store_true", help="simulate publishing without actual network posts")
    args = parser.parse_args()

    runner = CloudBatchRunner()
    if not args.url:
        runner.run_discovery_and_enqueue(query=args.query)

    runner.run_batch(
        max_duration_seconds=args.duration,
        target_clips=args.target_clips,
        single_url=args.url,
        dry_run=args.dry_run,
        unattended=True,
    )


if __name__ == "__main__":
    main()
