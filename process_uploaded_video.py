"""Process one user-owned YouTube gameplay upload in an ephemeral GitHub runner.

The workflow requires an explicit rights confirmation and verifies that the
video belongs to the configured owner channel before any download or processing.
"""
import argparse
import json
import os
import sys

from clip import stream_info
from cloud_runner import CloudBatchRunner
from vod_processor import _validate_youtube_url

MAX_VIDEO_SECONDS = 90 * 60
MAX_TARGET_CLIPS = 20
MAX_BATCH_SECONDS = 330 * 60


def process_uploaded_video(
    url,
    owner_channel_id,
    *,
    confirm_rights=False,
    target_clips=10,
    dry_run=True,
    enable_publishing=False,
    unattended=False,
    info_reader=stream_info,
    runner_factory=CloudBatchRunner,
):
    """Queue and process one regular YouTube upload owned by the configured channel."""
    if confirm_rights is not True:
        raise PermissionError(
            "Confirm that you own this gameplay video or have explicit rights to edit and publish it."
        )
    owner_channel_id = str(owner_channel_id or "").strip()
    if not owner_channel_id:
        raise ValueError("GTA_OWNER_CHANNEL_ID must be configured as a GitHub Actions secret.")
    if not 1 <= int(target_clips) <= MAX_TARGET_CLIPS:
        raise ValueError(f"target_clips must be between 1 and {MAX_TARGET_CLIPS}.")
    if unattended and not enable_publishing:
        raise ValueError("Unattended mode requires publishing to be enabled.")
    if enable_publishing and not unattended:
        raise ValueError(
            "Automatic publishing requires unattended=true and the GTA_UNATTENDED_POSTS=1 secret."
        )
    if unattended and os.environ.get("GTA_UNATTENDED_POSTS") != "1":
        raise PermissionError("Unattended publishing requires GTA_UNATTENDED_POSTS=1.")

    _validate_youtube_url(url)
    info = info_reader(url)
    video_id = str(info.get("id") or "").strip()
    channel_id = str(info.get("channel_id") or "").strip()
    duration = info.get("duration")
    if not video_id or not channel_id:
        raise ValueError("The supplied URL did not resolve to a single YouTube video and channel.")
    if channel_id != owner_channel_id:
        raise PermissionError(
            "The video channel does not match GTA_OWNER_CHANNEL_ID. "
            "Use an unlisted upload from your configured channel."
        )
    if (
        info.get("is_live")
        or info.get("live_status") in {"is_live", "is_upcoming", "was_live"}
        or info.get("_type") in {"playlist", "multi_video"}
        or info.get("entries")
    ):
        raise ValueError("Provide one completed regular video, not a livestream or playlist.")
    if duration is None or float(duration) <= 0:
        raise ValueError("The video's duration is missing or invalid.")
    if float(duration) > MAX_VIDEO_SECONDS:
        raise ValueError(
            f"The video is {int(float(duration))} seconds. "
            f"The current per-video limit is {MAX_VIDEO_SECONDS} seconds (90 minutes)."
        )

    # This policy exists only in this process. It is never written to the repo,
    # cache, artifact, or workflow output. The owner-channel match and required
    # manual confirmation are the basis for this one-video self-owned grant.
    policy = {
        "version": 1,
        "sources": {
            channel_id: {
                "channel_id": channel_id,
                "channel_name": info.get("channel") or info.get("uploader") or "Owner channel",
                "permission_status": "self_owned",
                "permission_scope": ["record", "clip", "publish"],
                "gameplay_verified": True,
                "approved_video_ids": [video_id],
                "proof_reference": "Per-run owner-channel match plus required workflow rights confirmation",
                "credit": info.get("uploader") or info.get("channel") or "Owner",
            }
        },
    }
    previous_policy = os.environ.get("GTA_SOURCE_ALLOWLIST_JSON")
    os.environ["GTA_SOURCE_ALLOWLIST_JSON"] = json.dumps(policy)

    try:
        runner = runner_factory()
        title = info.get("title") or "User gameplay upload"
        job = runner.queue.enqueue(
            job_type="capture_and_clip",
            job_key=f"manual-upload:{channel_id}:{video_id}",
            payload={
                "url": url,
                "video_id": video_id,
                "title": title,
                "channel_id": channel_id,
                "channel": info.get("channel") or info.get("uploader") or "Owner",
                "credit": info.get("uploader") or info.get("channel") or "Owner",
                "gameplay_verified": True,
                "source_type": "youtube_video",
                "rights_confirmed_by_user": True,
            },
            authorized=True,
            max_attempts=1,
        )
        if not job:
            raise RuntimeError("The upload could not be added to the processing queue.")

        print(f"Accepted one owner-channel video: {video_id} — {title}")
        print("Temporary source media and generated clips will be removed by the workflow after this run.")
        stats = runner.run_batch(
            max_duration_seconds=MAX_BATCH_SECONDS,
            target_clips=int(target_clips),
            dry_run=bool(dry_run),
            enable_publishing=bool(enable_publishing),
            unattended=bool(unattended),
        )
        print(json.dumps(stats, indent=2))
        if stats.get("errors"):
            raise RuntimeError(
                f"Processing finished with {len(stats['errors'])} error(s). "
                "Review this workflow run before retrying."
            )
        return stats
    finally:
        if previous_policy is None:
            os.environ.pop("GTA_SOURCE_ALLOWLIST_JSON", None)
        else:
            os.environ["GTA_SOURCE_ALLOWLIST_JSON"] = previous_policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="One unlisted YouTube video URL from your owner channel")
    parser.add_argument("--owner-channel-id", required=True, help="Expected YouTube channel ID (UC...)")
    parser.add_argument("--confirm-rights", action="store_true", help="Confirm ownership or explicit editing/publishing rights")
    parser.add_argument("--target-clips", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true", help="Do not upload or schedule any clips")
    parser.add_argument("--enable-publishing", action="store_true")
    parser.add_argument("--unattended", action="store_true")
    args = parser.parse_args()

    try:
        process_uploaded_video(
            args.url,
            args.owner_channel_id,
            confirm_rights=args.confirm_rights,
            target_clips=args.target_clips,
            dry_run=args.dry_run,
            enable_publishing=args.enable_publishing,
            unattended=args.unattended,
        )
    except Exception as error:
        sys.exit(f"Gameplay processing failed: {type(error).__name__}: {error}")


if __name__ == "__main__":
    main()
