"""Discover recent regular YouTube uploads from explicitly configured creator channels.

Uses the YouTube Data API v3 uploads playlist. This only discovers public
metadata; the cloud runner must still enforce record/clip permission.
"""
import datetime as dt
import json
import urllib.parse
import urllib.request

API = "https://www.googleapis.com/youtube/v3"


def _get(endpoint, params):
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{API}/{endpoint}?{query}",
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _parse_time(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def find_channel_videos(channels, api_key, *, recent_days=14, max_per_channel=5, now=None):
    """Return recent VOD uploads for the supplied channel IDs.

    Channels are explicit allowlist entries, not search results. Only public
    videos newer than recent_days are returned. Live/upcoming broadcasts are
    excluded because the live discovery path handles them separately.
    """
    channel_ids = sorted({
        str(item.get("channel_id") or "").strip()
        for item in channels
        if isinstance(item, dict) and str(item.get("channel_id") or "").strip()
    })
    if not channel_ids or not api_key:
        return []

    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=max(1, int(recent_days)))
    channel_payload = _get("channels", {
        "part": "contentDetails,snippet",
        "id": ",".join(channel_ids[:50]),
        "key": api_key,
        "maxResults": min(50, len(channel_ids)),
    })
    channel_data = {}
    for item in channel_payload.get("items", []):
        details = item.get("contentDetails", {}).get("relatedPlaylists", {})
        playlist_id = details.get("uploads")
        if playlist_id:
            channel_data[item["id"]] = {
                "playlist_id": playlist_id,
                "channel": item.get("snippet", {}).get("title", ""),
            }

    candidates = []
    for channel_id in channel_ids:
        channel = channel_data.get(channel_id)
        if not channel:
            continue
        playlist_payload = _get("playlistItems", {
            "part": "snippet,contentDetails",
            "playlistId": channel["playlist_id"],
            "maxResults": min(50, max(1, int(max_per_channel) * 3)),
            "key": api_key,
        })
        for item in playlist_payload.get("items", []):
            snippet = item.get("snippet", {})
            content = item.get("contentDetails", {})
            video_id = content.get("videoId")
            published = _parse_time(content.get("videoPublishedAt") or snippet.get("publishedAt"))
            if not video_id or not published or published < cutoff:
                continue
            candidates.append({
                "video_id": video_id,
                "channel_id": channel_id,
                "channel": channel["channel"],
                "title": snippet.get("title", ""),
                "published_at": published.isoformat(),
                "url": f"https://www.youtube.com/watch?v={video_id}",
            })

    results = []
    for offset in range(0, len(candidates), 50):
        batch = candidates[offset:offset + 50]
        payload = _get("videos", {
            "part": "snippet,liveStreamingDetails",
            "id": ",".join(item["video_id"] for item in batch),
            "key": api_key,
            "maxResults": len(batch),
        })
        by_id = {item.get("id"): item for item in payload.get("items", [])}
        for candidate in batch:
            video = by_id.get(candidate["video_id"])
            if not video:
                continue
            live_status = video.get("snippet", {}).get("liveBroadcastContent", "none")
            if live_status != "none" or video.get("liveStreamingDetails"):
                continue
            results.append({
                **candidate,
                "title": video.get("snippet", {}).get("title") or candidate["title"],
                "source_type": "youtube_video",
            })

    results.sort(key=lambda item: item["published_at"], reverse=True)
    per_channel = {}
    limited = []
    for item in results:
        count = per_channel.get(item["channel_id"], 0)
        if count >= max(1, int(max_per_channel)):
            continue
        per_channel[item["channel_id"]] = count + 1
        limited.append(item)
    return limited
