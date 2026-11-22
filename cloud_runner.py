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
from find_channel_videos import find_channel_videos
from performance_tracker import VideoPerformanceLedger, collect_due_snapshots, feed_scheduler_from_48h
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
        self.performance_ledger = VideoPerformanceLedger(self.state_dir / "video_performance.json")
        self.publisher = UnifiedPublisher()
        self.deduplicator = ClipDeduplicator(state_path=self.state_dir / "dedupe_ledger.json")

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
            print(f"Stream search encountered error: {type(exc).__name__}: {exc}")
            candidates = []

        authorized, unauthorized = self.policy.filter_streams(candidates, required_scope="record")
        authorized, missing_clip_scope = self.policy.filter_streams(authorized, required_scope="clip")
        unauthorized.extend(missing_clip_scope)
        print(f"Discovery found {len(candidates)} streams: {len(authorized)} authorized for recording and clipping, {len(unauthorized)} skipped.")

        enqueued_jobs = []
        for stream in authorized:
            job_key = f"youtube-live:{stream['channel_id']}:{stream['url']}"
            job = self.queue.enqueue(
                job_type="capture_and_clip",
                job_key=job_key,
                payload={
                    "url": stream["url"],
                    "video_id": stream.get("video_id"),
                    "title": stream.get("title"),
                    "channel_id": stream.get("channel_id"),
                    "channel": stream.get("channel"),
                    "credit": stream.get("source_record", {}).get("credit") or stream.get("channel"),
                    "gameplay_verified": True,
                    "source_type": "youtube_live",
                },
                authorized=True,
                max_attempts=3,
            )
            enqueued_jobs.append(job)

        # Also inspect public uploads playlists for explicitly configured sources.
        configured = [
            source for source in self.policy.list_sources()
            if self.policy.is_authorized(source.get("channel_id"), "record")
            and self.policy.is_authorized(source.get("channel_id"), "clip")
        ]
        try:
            videos = find_channel_videos(configured, key, recent_days=14, max_per_channel=5)
        except Exception as exc:
            print(f"Regular-video discovery encountered error: {type(exc).__name__}: {exc}")
            videos = []
        for video in videos:
            source = self.policy.get_source(video["channel_id"]) or {}
            job = self.queue.enqueue(
                job_type="capture_and_clip",
                job_key=f"youtube-video:{video['channel_id']}:{video['video_id']}",
                payload={
                    "url": video["url"],
                    "video_id": video["video_id"],
                    "title": video.get("title"),
                    "channel_id": video["channel_id"],
                    "channel": video.get("channel"),
                    "credit": source.get("credit") or video.get("channel"),
                    "gameplay_verified": True,
                    "source_type": "youtube_video",
                    "published_at": video.get("published_at"),
                },
                authorized=True,
                max_attempts=3,
            )
            enqueued_jobs.append(job)

        print(f"Regular-video discovery found {len(videos)} recent uploads from configured creators.")
        return enqueued_jobs

    def run_batch(self, max_duration_seconds=21600, target_clips=10,
                  single_url=None, dry_run=False, enable_publishing=False,
                  unattended=False):
        """Execute one complete automated cloud batch cycle within execution bounds."""
        batch_id = self.queue.start_batch(self.worker_id, duration_seconds=max_duration_seconds)
        start_time = time.time()
        # Keep a small shutdown margin for artifact upload and runner cleanup.
        work_budget = max(1, max_duration_seconds - min(300, max_duration_seconds * 0.10))
        work_deadline = start_time + work_budget
        generated_outputs = []
        next_discovery_at = start_time + 15 * 60
        print(f"=== Starting Cloud Batch {batch_id} (Worker: {self.worker_id}) ===")

        stats = {
            "batch_id": batch_id,
            "worker_id": self.worker_id,
            "jobs_claimed": 0,
            "jobs_skipped_unauthorized": 0,
            "clips_generated": 0,
            "clips_published": 0,
            "clips_previewed": 0,
            "clips_skipped_unauthorized": 0,
            "errors": [],
        }

        try:
            # Collect public performance snapshots after 1h, 6h, 24h and 48h.
            api_key = os.environ.get("YOUTUBE_API_KEY")
            snapshot_stats = collect_due_snapshots(self.performance_ledger, api_key)
            imported = feed_scheduler_from_48h(self.performance_ledger, self.scheduler.ledger)
            print(
                f"Performance analyser: {snapshot_stats['recorded']} snapshot(s) recorded; "
                f"{imported} 48-hour result(s) added to posting-time learning."
            )
            if snapshot_stats.get("error"):
                print(f"Performance snapshot warning: {snapshot_stats['error']}")

            # Step 1: If direct URL provided or fallback discovery
            if single_url:
                info = stream_info(single_url)
                identity = source_identity(info)
                channel_id = identity.get("channel_id")
                if not info.get("is_live"):
                    raise ValueError("Direct source is not currently live.")
                if not channel_id or not all(
                    self.policy.is_authorized(channel_id, required_scope=scope)
                    for scope in ("record", "clip")
                ):
                    raise PermissionError(
                        "Direct stream rejected: its channel must be allowlisted, gameplay-verified, "
                        "and authorized for both recording and clipping."
                    )
                source = self.policy.get_source(channel_id)
                job_key = f"direct-stream:{channel_id}:{single_url}"
                self.queue.enqueue(
                    "capture_and_clip", job_key,
                    {
                        "url": single_url, "video_id": info.get("id"),
                        "title": info.get("title", "Direct Source"),
                        "channel_id": channel_id, "channel": identity.get("name"),
                        "credit": source.get("credit") or identity.get("credit"),
                        "gameplay_verified": True,
                    },
                    authorized=True
                )

            # Step 2: Work the queue
            while time.time() < work_deadline:
                if stats["clips_generated"] >= target_clips:
                    print(f"Target of {target_clips} clips reached for this batch.")
                    break

                job = self.queue.claim_next(self.worker_id, lease_seconds=1200)
                if not job:
                    api_key = os.environ.get("YOUTUBE_API_KEY")
                    if not api_key:
                        print("No queued jobs and YOUTUBE_API_KEY is not configured; ending batch.")
                        break

                    now = time.time()
                    if now >= next_discovery_at:
                        print("Queue is idle; searching again for newly started live streams.")
                        self.run_discovery_and_enqueue(api_key=api_key)
                        next_discovery_at = time.time() + 15 * 60
                        continue

                    wait_seconds = min(
                        30,
                        max(1, next_discovery_at - now),
                        max(1, work_deadline - now),
                    )
                    time.sleep(wait_seconds)
                    continue

                stats["jobs_claimed"] += 1
                job_id = job["id"]
                payload = job.get("payload", {})
                url = payload.get("url")
                channel_id = payload.get("channel_id")

                # Queue state is persistent and may outlive a creator permission
                # change. Re-check authorization immediately before every capture.
                if (
                    not channel_id
                    or payload.get("gameplay_verified") is not True
                    or not all(
                        self.policy.is_authorized(channel_id, required_scope=scope)
                        for scope in ("record", "clip")
                    )
                ):
                    stats["jobs_skipped_unauthorized"] += 1
                    self.queue.complete(job_id, self.worker_id)
                    print(f"Skipped queued job {job_id}: source is no longer authorized.")
                    continue

                print(f"Claimed job {job_id} for stream: {url}")
                try:
                    # Determine stable session
                    sess_id = recording_session_id(payload.get("video_id") or url)
                    session = RecordingSession(self.recordings_dir, session_id=sess_id)

                    # Recover any interrupted work
                    session.recover_unregistered_chunks()
                    rec_outputs, _ = run_recovery(
                        session, self.worker_id, str(self.output_dir),
                        {
                            "name": payload.get("channel", "Streamer"),
                            "credit": payload.get("credit"),
                            "channel_id": channel_id,
                            "gameplay_verified": bool(payload.get("gameplay_verified")),
                        },
                        payload.get("title", "GTA Live Stream"),
                        url,
                        gameplay_confirmed=bool(payload.get("gameplay_verified")),
                        deduplicator=self.deduplicator,
                    )
                    generated_outputs.extend(rec_outputs)
                    stats["clips_generated"] += len(rec_outputs)

                    # If time remains, capture more chunks (e.g. up to remaining time)
                    remaining = max(0, work_deadline - time.time())
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
                                {
                                    "name": payload.get("channel", "Streamer"),
                                    "credit": payload.get("credit"),
                                    "channel_id": channel_id,
                                    "gameplay_verified": bool(payload.get("gameplay_verified")),
                                },
                                payload.get("title", "GTA Stream"), url,
                                gameplay_confirmed=bool(payload.get("gameplay_verified")),
                                deduplicator=self.deduplicator,
                            )
                            generated_outputs.extend(outputs)
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

            # Step 3: Publish only this batch's outputs, and only when explicitly enabled.
            if not enable_publishing:
                print("Publishing is disabled. Generated clips stay on this temporary runner unless a separately authorized public artifact is enabled.")
            else:
                if unattended and os.environ.get("GTA_UNATTENDED_POSTS") != "1":
                    raise PermissionError(
                        "Unattended publishing requires the GTA_UNATTENDED_POSTS=1 secret."
                    )
                if not dry_run and (
                    self.publisher.provider != "youtube_direct"
                    or not self.publisher.yt_publisher.is_configured
                ):
                    raise RuntimeError(
                        "Real publishing is enabled, but direct YouTube OAuth is not fully configured. "
                        "Set YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, and YOUTUBE_REFRESH_TOKEN."
                    )

                unique_metadata = sorted({str(Path(pair[1]).resolve()) for pair in generated_outputs})
                scheduled_times = []
                for meta_name in unique_metadata:
                    meta_path = Path(meta_name)
                    try:
                        with open(meta_path, "r", encoding="utf-8") as f:
                            meta = json.load(f)

                        if meta.get("test") or meta.get("youtube_video_id") or meta.get("zernio_post_id") or meta.get("dry_run_published"):
                            continue

                        channel_id = meta.get("source_channel_id")
                        if not self.policy.is_authorized(channel_id, required_scope="publish"):
                            stats["clips_skipped_unauthorized"] += 1
                            print(f"Skipping {meta_path.name}: source lacks active publish permission.")
                            continue
                        if not meta.get("approved") and not unattended:
                            print(f"Skipping {meta_path.name}: clip has not been manually approved.")
                            continue

                        cat = meta.get("category", "high_action")
                        rec_slot = self.scheduler.recommend_publish_time(
                            category=cat, epsilon=0.20, existing_scheduled=scheduled_times
                        )
                        scheduled_time = rec_slot["recommended_time_utc"]
                        scheduled_times.append(scheduled_time)
                        pub_res = self.publisher.publish_clip(
                            meta_path, scheduled_for=scheduled_time, dry_run=dry_run,
                            unattended=unattended,
                        )
                        if dry_run:
                            stats["clips_previewed"] += 1
                            print(f"Dry-run preview for {meta_path.name}: would schedule at {scheduled_time}")
                        else:
                            stats["clips_published"] += 1
                            published_result = pub_res.get("result", {}) if isinstance(pub_res, dict) else {}
                            video_id = published_result.get("id") if isinstance(published_result, dict) else None
                            if video_id:
                                self.performance_ledger.register(video_id, scheduled_time, cat)
                            print(
                                f"Scheduled clip {meta_path.name} for {scheduled_time} "
                                f"({rec_slot['strategy']}; {rec_slot['slot_samples']} historical sample(s))"
                            )
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
    parser.add_argument("--enable-publishing", action="store_true", help="allow publishing of eligible batch outputs")
    parser.add_argument("--unattended", action="store_true", help="allow auto-publishing only for sources with explicit publish permission")
    args = parser.parse_args()
    if args.unattended and not args.enable_publishing:
        parser.error("--unattended requires --enable-publishing")
    if args.unattended and os.environ.get("GTA_UNATTENDED_POSTS") != "1":
        parser.error("--unattended requires GTA_UNATTENDED_POSTS=1 in the environment")

    runner = CloudBatchRunner()
    if not args.url:
        runner.run_discovery_and_enqueue(query=args.query)

    runner.run_batch(
        max_duration_seconds=args.duration,
        target_clips=args.target_clips,
        single_url=args.url,
        dry_run=args.dry_run,
        enable_publishing=args.enable_publishing,
        unattended=args.unattended,
    )


if __name__ == "__main__":
    main()
