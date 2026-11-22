"""Direct YouTube Data API v3 publishing for YouTube Shorts.

Handles OAuth 2.0 token refreshes, resumable video uploads, metadata,
scheduled publishing, status verification, and duplicate upload prevention.
"""
import datetime as dt
import json
import os
import sys
import time
import requests
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
        raw_title = metadata.get("title")
        if not raw_title:
            category = str(metadata.get("category") or "gameplay").replace("_", " ")
            transcript = " ".join(str(metadata.get("transcript") or "").split())
            if transcript:
                excerpt = transcript[:58].rsplit(" ", 1)[0]
                raw_title = f"GTA {category}: {excerpt}".strip()
            else:
                stream_title = metadata.get("stream_title") or "Gameplay moment"
                raw_title = f"GTA {category}: {stream_title}"
        # Shorts best practice: include #Shorts in title or description
        if "#shorts" not in raw_title.lower() and len(raw_title) <= 92:
            title = f"{raw_title} #Shorts"
        else:
            title = raw_title[:100]

        description = metadata.get("youtube_description") or metadata.get("transcript") or title
        credit = metadata.get("credit")
        source_url = metadata.get("source_url")
        if credit:
            description = f"{description}\n\nOriginal creator: {credit}"
        if source_url:
            description = f"{description}\nSource stream: {source_url}"
        if "#Shorts" not in description:
            description = f"{description}\n\n#Shorts #GTA"

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
            # A preview must never write a fake video ID into real clip metadata:
            # doing so would make a later real upload look like a duplicate.
            simulated_id = f"dry-run-yt-{int(time.time())}"
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

        # Step 2: Upload in resumable 8 MiB chunks. A 308 response reports
        # the last byte accepted by YouTube, allowing safe continuation after
        # a transient network interruption.
        chunk_size = 8 * 1024 * 1024
        offset = 0
        result = None
        no_progress_retries = 0
        with open(v_path, "rb") as video_file:
            while offset < file_size:
                video_file.seek(offset)
                chunk = video_file.read(min(chunk_size, file_size - offset))
                if not chunk:
                    raise RuntimeError("Unexpected end of file during YouTube upload.")
                end = offset + len(chunk) - 1
                headers = {
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{file_size}",
                }

                response = None
                for attempt in range(3):
                    try:
                        response = requests.put(
                            session_url, data=chunk, headers=headers,
                            timeout=(30, 600), allow_redirects=False,
                        )
                        break
                    except requests.RequestException:
                        if attempt == 2:
                            raise
                        time.sleep(2 ** attempt)

                if response.status_code == 308:
                    received_range = response.headers.get("Range", "")
                    if received_range:
                        try:
                            next_offset = int(received_range.rsplit("-", 1)[1]) + 1
                        except (ValueError, IndexError):
                            raise RuntimeError(f"Invalid YouTube resumable upload Range: {received_range}")
                    else:
                        next_offset = 0

                    if next_offset <= offset:
                        no_progress_retries += 1
                        if no_progress_retries >= 3:
                            raise RuntimeError("YouTube resumable upload made no progress after three attempts.")
                    else:
                        no_progress_retries = 0
                    if next_offset > file_size:
                        raise RuntimeError("YouTube resumable upload reported an invalid byte range.")
                    offset = next_offset
                    continue

                response.raise_for_status()
                result = response.json()
                break

        if not result:
            raise RuntimeError("YouTube upload ended without a completed response.")

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
