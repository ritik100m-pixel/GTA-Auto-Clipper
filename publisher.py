"""Modular multi-provider publishing router for GTA Auto Clipper Cloud.

Supports:
- Direct YouTube Data API v3 Shorts publishing (OAuth 2.0 refresh, resumable upload)
- Zernio publishing (multi-account scheduler for YouTube and Instagram)
- Dry-run validation mode for testing and unreviewed workflows
"""
import argparse
import json
import os
import sys
from pathlib import Path

from youtube_publish import YouTubePublisher
import zernio_publish


class UnifiedPublisher:
    """Dispatches publishing requests to the configured provider."""

    def __init__(self, provider=None, environ=None):
        self.env = environ if environ is not None else os.environ
        # Auto-detect provider if not specified
        if provider:
            self.provider = provider.lower()
        elif self.env.get("YOUTUBE_REFRESH_TOKEN") and self.env.get("YOUTUBE_CLIENT_ID"):
            self.provider = "youtube_direct"
        elif self.env.get("ZERNIO_API_KEY") or self.env.get("PUBLISH_API_KEY"):
            self.provider = "zernio"
        else:
            self.provider = "dry_run"

        self.yt_publisher = YouTubePublisher(environ=self.env)

    def publish_clip(self, clip_path, scheduled_for=None, publish_now=False,
                     dry_run=False, unattended=False):
        base = Path(clip_path).with_suffix("")
        v_path = base.with_suffix(".mp4")
        m_path = base.with_suffix(".json")

        if not v_path.is_file() or not m_path.is_file():
            raise FileNotFoundError(f"Clip files missing: {v_path} or {m_path}")

        with open(m_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)

        if metadata.get("test") and not dry_run:
            print("Note: Clip is marked as test; enforcing dry_run mode.")
            dry_run = True

        if not metadata.get("approved") and not unattended and not dry_run:
            raise PermissionError("Clip is not approved. Pass --unattended to publish with standing approval.")

        if self.provider == "youtube_direct":
            privacy = "public" if publish_now else "private"
            schedule = None if publish_now else scheduled_for
            result = self.yt_publisher.publish_clip(
                v_path, m_path, scheduled_for=schedule, privacy_status=privacy, dry_run=dry_run
            )
            return {
                "provider": "youtube_direct",
                "result": result,
                "video_path": str(v_path),
                "metadata_path": str(m_path),
            }

        elif self.provider == "zernio":
            if dry_run:
                # Preview only. Do not persist a fake post ID or block a later real post.
                simulated_id = f"dry-run-zernio-{base.name}"
                return {
                    "provider": "zernio",
                    "result": {"id": simulated_id, "status": "dry_run_verified"},
                    "video_path": str(v_path),
                    "metadata_path": str(m_path),
                }

            schedule_arg = "now" if publish_now else (zernio_publish.scheduled_local_time(scheduled_for) if scheduled_for else None)
            accounts = [
                (p, self.env.get(var, ""))
                for p, var in zernio_publish.PLATFORMS
                if self.env.get(var)
            ]
            api_key = self.env.get("ZERNIO_API_KEY") or self.env.get("PUBLISH_API_KEY")
            if not api_key:
                raise ValueError("Missing ZERNIO_API_KEY / PUBLISH_API_KEY.")
            result = zernio_publish.upload_and_schedule(
                v_path, m_path, metadata, accounts, schedule_arg, api_key
            )
            return {
                "provider": "zernio",
                "result": result,
                "video_path": str(v_path),
                "metadata_path": str(m_path),
            }

        else:  # dry_run
            sim_id = f"simulated-{base.name}"
            return {
                "provider": "dry_run",
                "result": {"id": sim_id, "status": "simulated_success"},
                "video_path": str(v_path),
                "metadata_path": str(m_path),
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clip", type=Path, help="path to .mp4 or .json clip")
    parser.add_argument("--provider", choices=("youtube_direct", "zernio", "dry_run"), default=None)
    parser.add_argument("--scheduled-for", help="ISO future time with offset (e.g. 2026-11-25T18:00:00+05:30)")
    parser.add_argument("--now", action="store_true", help="publish immediately")
    parser.add_argument("--dry-run", action="store_true", help="simulate upload without network calls")
    parser.add_argument("--unattended", action="store_true", help="allow unreviewed clips under standing authorization")
    args = parser.parse_args()

    pub = UnifiedPublisher(provider=args.provider)
    print(f"Selected publisher provider: {pub.provider}")
    try:
        out = pub.publish_clip(
            args.clip,
            scheduled_for=args.scheduled_for,
            publish_now=args.now,
            dry_run=args.dry_run,
            unattended=args.unattended,
        )
        print("Publishing successful:", json.dumps(out, indent=2))
    except Exception as e:
        sys.exit(f"Publishing failed: {e}")


if __name__ == "__main__":
    main()
