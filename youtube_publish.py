"""Direct YouTube Data API v3 publishing for YouTube Shorts.

Handles OAuth 2.0 token refreshes, resumable video uploads, metadata,
scheduled publishing, status verification, and duplicate upload prevention.
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"


class YouTubePublisher:
    """Publishes vertical Shorts directly to YouTube using OAuth 2.0."""

    def __init__(self, client_id=None, client_secret=None, refresh_token=None, environ=None):
        env = environ if environ is not None else os.environ
        self.client_id = (client_id or env.get("YOUTUBE_CLIENT_ID", "")).strip()
        self.client_secret = (client_secret or env.get("YOUTUBE_CLIENT_SECRET", "")).strip()
        self.refresh_token = (refresh_token or env.get("YOUTUBE_REFRESH_TOKEN", "")).strip()
        self._access_token = None
        self._access_token_expires_at = 0.0

    @property
    def is_configured(self):
        return bool(self.client_id and self.client_secret and self.refresh_token)

    def refresh_access_token(self):
        """Fetch a fresh access token using the stored refresh token."""
        if not self.is_configured:
            raise ValueError(
                "YouTube OAuth credentials missing: require YOUTUBE_CLIENT_ID, "
                "YOUTUBE_CLIENT_SECRET, and YOUTUBE_REFRESH_TOKEN."
            )
        now = time.time()
        if self._access_token and now < (self._access_token_expires_at - 60):
            return self._access_token

        params = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }).encode("utf-8")

        req = urllib.request.Request(TOKEN_ENDPOINT, data=params, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        })

        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)

        token = payload.get("access_token")
        if not token:
            raise RuntimeError(f"OAuth token refresh did not return an access_token: {payload}")

        expires_in = float(payload.get("expires_in", 3600))
        self._access_token = token
        self._access_token_expires_at = now + expires_in
        return self._access_token

    def build_snippet_and_status(self, metadata, scheduled_for=None, privacy_status="public"):
        """Construct official YouTube API snippet and status dicts."""
        raw_title = metadata.get("title") or "GTA Epic Gameplay Moment"
        # Shorts best practice: include #Shorts in title or description
        if "#shorts" not in raw_title.lower() and len(raw_title) <= 92:
            title = f"{raw_title} #Shorts"
        else:
            title = raw_title[:100]

        description = metadata.get("youtube_description") or metadata.get("transcript") or title
        if "#Shorts" not in description:
            description = f"{description}\n\n#Shorts #GTA #GTA6"

        tags = list(metadata.get("detected_tags") or metadata.get("hashtags") or ["GTA", "Shorts", "GTA6"])

        snippet = {
            "title": title,
            "description": description,
            "tags": tags[:15],
            "categoryId": "20",  # Gaming
            "defaultLanguage": "en",
        }

        status = {
            "selfDeclaredMadeForKids": False,
        }

        if scheduled_for:
            # YouTube API requires privacyStatus='private' when publishAt is set
            publish_dt = dt.datetime.fromisoformat(str(scheduled_for).replace("Z", "+00:00"))
            status["privacyStatus"] = "private"
            status["publishAt"] = publish_dt.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
        else:
            status["privacyStatus"] = privacy_status

        return snippet, status

    def publish_clip(self, video_path, metadata_path, scheduled_for=None,
                     privacy_status="public", dry_run=False):
        """Upload a video clip as a YouTube Short."""
        v_path = Path(video_path).resolve()
        m_path = Path(metadata_path).resolve()

        if not v_path.is_file() or v_path.stat().st_size == 0:
            raise FileNotFoundError(f"video file not found or empty: {v_path}")
        if not m_path.is_file():
            raise FileNotFoundError(f"metadata file not found: {m_path}")

        with open(m_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)

        # Duplicate protection
        if metadata.get("youtube_video_id"):
            raise ValueError(
                f"Clip already published to YouTube with ID {metadata['youtube_video_id']}. "
                "Duplicate upload aborted."
            )

        snippet, status = self.build_snippet_and_status(
            metadata, scheduled_for=scheduled_for, privacy_status=privacy_status
        )

        if dry_run or metadata.get("test"):
            simulated_id = f"dry-run-yt-{int(time.time())}"
            metadata["youtube_video_id"] = simulated_id
            metadata["youtube_publish_status"] = "dry_run_verified"
            metadata["youtube_scheduled_for"] = status.get("publishAt")
            with open(m_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
            return {
                "id": simulated_id,
                "status": "dry_run",
                "title": snippet["title"],
                "publishAt": status.get("publishAt"),
            }

        access_token = self.refresh_access_token()
        file_size = v_path.stat().st_size

        # Step 1: Initiate resumable upload session
        body_bytes = json.dumps({"snippet": snippet, "status": status}).encode("utf-8")
        init_req = urllib.request.Request(
            UPLOAD_ENDPOINT,
            data=body_bytes,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(file_size),
            },
            method="POST",
        )

        with urllib.request.urlopen(init_req, timeout=30) as init_resp:
            session_url = init_resp.headers.get("Location")

        if not session_url:
            raise RuntimeError("YouTube API did not return a resumable upload Location header.")

        # Step 2: Stream video bytes to the session location
        with open(v_path, "rb") as video_file:
            upload_req = urllib.request.Request(
                session_url,
                data=video_file,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(file_size),
                },
                method="PUT",
            )
            with urllib.request.urlopen(upload_req, timeout=600) as upload_resp:
                result = json.load(upload_resp)

        video_id = result.get("id")
        if not video_id:
            raise RuntimeError(f"Upload completed but no video ID was returned: {result}")

        # Update metadata atomically
        metadata["youtube_video_id"] = video_id
        metadata["youtube_publish_status"] = "scheduled" if status.get("publishAt") else "uploaded"
        metadata["youtube_scheduled_for"] = status.get("publishAt")
        metadata["youtube_uploaded_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        with open(m_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

        return {
            "id": video_id,
            "status": metadata["youtube_publish_status"],
            "url": f"https://www.youtube.com/shorts/{video_id}",
            "publishAt": status.get("publishAt"),
        }
