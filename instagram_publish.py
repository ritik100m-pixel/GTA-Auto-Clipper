"""Instagram Graph API publishing adapter and eligibility verifier.

Documented limitations and official Meta Content Publishing API requirements:
1. Account eligibility: Requires an Instagram Professional account (Creator or Business)
   connected to a Facebook Page. Personal accounts have no API publishing access.
2. Direct local byte upload: Not supported by Instagram API. The API requires a
   publicly accessible video URL ('video_url') to ingest media.
3. Two-step container process:
   - Step 1: POST /{ig-user-id}/media?media_type=REELS&video_url=... -> creation_id
   - Step 2: Poll status of creation_id until FINISHED
   - Step 3: POST /{ig-user-id}/media_publish?creation_id=... -> published ID
4. Rate limit: 50 posts per rolling 24 hours.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GRAPH_API = "https://graph.facebook.com/v21.0"


class InstagramPublisher:
    """Manages Instagram Reels publication via Meta Graph API."""

    def __init__(self, access_token=None, ig_user_id=None, environ=None):
        env = environ if environ is not None else os.environ
        self.access_token = (access_token or env.get("INSTAGRAM_ACCESS_TOKEN", "")).strip()
        self.ig_user_id = (ig_user_id or env.get("INSTAGRAM_USER_ID", "")).strip()

    @property
    def is_configured(self):
        return bool(self.access_token and self.ig_user_id)

    def check_eligibility(self):
        """Verify token permissions and Instagram Professional account status."""
        if not self.is_configured:
            return {
                "eligible": False,
                "reason": "Missing INSTAGRAM_ACCESS_TOKEN or INSTAGRAM_USER_ID.",
            }
        url = f"{GRAPH_API}/{self.ig_user_id}?fields=id,username,account_type&access_token={self.access_token}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.load(resp)
            account_type = data.get("account_type", "").upper()
            if account_type not in {"BUSINESS", "CREATOR"}:
                return {
                    "eligible": False,
                    "reason": f"Account type is '{account_type}'. Instagram Content Publishing API requires BUSINESS or CREATOR.",
                    "details": data,
                }
            return {"eligible": True, "details": data}
        except urllib.error.HTTPError as err:
            return {"eligible": False, "reason": f"Meta Graph API error HTTP {err.code}: {err.reason}"}
        except Exception as exc:
            return {"eligible": False, "reason": f"Connection error: {type(exc).__name__}"}

    def publish_reel(self, public_video_url, caption="", dry_run=False):
        """Publish a Reel given a public video URL."""
        if dry_run:
            return {
                "id": f"dry-run-ig-{int(time.time())}",
                "status": "dry_run_verified",
                "caption": caption[:100],
            }
        if not self.is_configured:
            raise ValueError("Instagram credentials unconfigured.")
        if not public_video_url or not public_video_url.startswith("http"):
            raise ValueError("Instagram API requires a publicly accessible HTTP/HTTPS video URL.")

        # Step 1: Create media container
        params = urllib.parse.urlencode({
            "media_type": "REELS",
            "video_url": public_video_url,
            "caption": caption,
            "access_token": self.access_token,
        }).encode("utf-8")
        req = urllib.request.Request(f"{GRAPH_API}/{self.ig_user_id}/media", data=params, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            container = json.load(resp)
        creation_id = container.get("id")
        if not creation_id:
            raise RuntimeError(f"Container creation failed: {container}")

        # Step 2: Poll container status
        status_url = f"{GRAPH_API}/{creation_id}?fields=status_code&access_token={self.access_token}"
        for _ in range(30):
            time.sleep(5)
            with urllib.request.urlopen(urllib.request.Request(status_url), timeout=15) as s_resp:
                s_data = json.load(s_resp)
            code = s_data.get("status_code")
            if code == "FINISHED":
                break
            if code in {"ERROR", "EXPIRED"}:
                raise RuntimeError(f"Reel processing failed with status {code}: {s_data}")

        # Step 3: Publish container
        pub_params = urllib.parse.urlencode({
            "creation_id": creation_id,
            "access_token": self.access_token,
        }).encode("utf-8")
        pub_req = urllib.request.Request(f"{GRAPH_API}/{self.ig_user_id}/media_publish", data=pub_params, method="POST")
        with urllib.request.urlopen(pub_req, timeout=30) as p_resp:
            pub_result = json.load(p_resp)

        return {
            "id": pub_result.get("id"),
            "status": "published",
        }
