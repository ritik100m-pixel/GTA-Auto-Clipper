"""Drive queue -> scheduled YouTube Shorts uploader with verify-before-delete."""
import json, mimetypes, os, re, sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

D = "https://www.googleapis.com/drive/v3"
Y = "https://www.googleapis.com/youtube/v3"
TOKEN = "https://oauth2.googleapis.com/token"
STATE = Path("cloud_state/shorts_manager.json")
IST = ZoneInfo("Asia/Kolkata")
EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
MAX_BYTES = 5 * 1024**3
DRY_RUN = os.environ.get("SHORTS_MANAGER_DRY_RUN", "").strip().lower() == "true"


def need(key):
    value = os.environ.get(key, "").strip()
    if not value:
        raise RuntimeError(f"Missing required secret: {key}")
    return value


def auth():
    r = requests.post(TOKEN, data={
        "client_id": need("YOUTUBE_CLIENT_ID"),
        "client_secret": need("YOUTUBE_CLIENT_SECRET"),
        "refresh_token": need("YOUTUBE_REFRESH_TOKEN"),
        "grant_type": "refresh_token",
    }, timeout=30)
    if not r.ok:
        try:
            error_body = r.json()
        except ValueError:
            error_body = {}
        if error_body.get("error") == "invalid_grant":
            raise RuntimeError("Google OAuth refresh token is invalid or expired. Re-authorize the same OAuth client with the required Drive and YouTube scopes; External apps left in Testing expire refresh tokens after 7 days.")
        raise RuntimeError(f"Google OAuth token refresh failed: HTTP {r.status_code} {error_body.get('error', 'unknown_error')}")
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def title_for(filename):
    s = Path(filename).stem.replace("_", " ")
    s = re.sub(r"(?i)^retry\s+", "", s)
    s = re.sub(r"\s+", " ", s).strip(" .-_")
    s = re.sub(r"(?i)\s+(final|export|render|video)$", "", s)
    s = re.sub(r"\s+", " ", s).strip() or "Epic Gaming Moment"
    s = s[:90].rstrip()
    return (s if "#shorts" in s.lower() else s + " #Shorts")[:100]


def next_slot(state):
    now = datetime.now(IST)
    last = [r.get("publish_at") for r in state["processed"].values() if r.get("publish_at")]
    if last:
        latest = max(datetime.fromisoformat(value) for value in last)
        now = max(now, latest)
    for day in range(366):
        date = (now + timedelta(days=day)).date()
        for hour in (11, 19):
            slot = datetime(date.year, date.month, date.day, hour, 30, tzinfo=IST)
            if slot > now + timedelta(minutes=20):
                return slot.isoformat()
    raise RuntimeError("No future posting slot found")


def read_state():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE.exists():
        initial = {"version": 1, "processed": {}}
        save_state(initial)
        return initial
    state = json.loads(STATE.read_text())
    if state.get("version") != 1 or not isinstance(state.get("processed"), dict):
        raise RuntimeError("Invalid deduplication ledger; refusing to upload.")
    return state


def save_state(state):
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True))
    temp.replace(STATE)


def drive_files(headers, folder):
    out, page = [], None
    while True:
        params = {
            "q": f"'{folder}' in parents and trashed=false",
            "pageSize": 100, "orderBy": "createdTime",
            "fields": "nextPageToken,files(id,name,mimeType,size,appProperties)",
            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
        }
        if page:
            params["pageToken"] = page
        r = requests.get(D + "/files", headers=headers, params=params, timeout=45)
        r.raise_for_status()
        body = r.json()
        out.extend(body.get("files", []))
        page = body.get("nextPageToken")
        if not page:
            return out


def download(headers, file_id, path):
    r = requests.get(D + f"/files/{file_id}", headers=headers,
                     params={"alt": "media", "supportsAllDrives": "true"},
                     stream=True, timeout=(30, 300))
    r.raise_for_status()
    total = 0
    with path.open("wb") as f:
        for chunk in r.iter_content(1024 * 1024):
            if chunk:
                total += len(chunk)
                if total > MAX_BYTES:
                    raise RuntimeError("File exceeds 5 GiB; original retained in Drive.")
                f.write(chunk)
    if total == 0:
        raise RuntimeError("Downloaded file is empty; original retained in Drive.")


def upload(headers, path, title, channel, when):
    meta = {
        "snippet": {"title": title,
                    "description": "Gaming highlights.\n\n#Shorts #Gaming",
                    "tags": ["Shorts", "Gaming", "Gameplay"], "categoryId": "20"},
        "status": {"privacyStatus": "private", "publishAt": when},
    }
    size = path.stat().st_size
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    init = requests.post(Y + "/videos", headers={
        **headers, "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": content_type,
    }, params={"uploadType": "resumable", "part": "snippet,status"},
       json=meta, timeout=60)
    init.raise_for_status()
    url = init.headers.get("Location")
    if not url:
        raise RuntimeError("YouTube did not provide a resumable upload URL.")
    with path.open("rb") as f:
        sent = requests.put(url, data=f,
            headers={"Content-Length": str(size), "Content-Type": content_type},
            timeout=(60, 1800))
    sent.raise_for_status()
    video_id = sent.json().get("id")
    if not video_id:
        raise RuntimeError("YouTube upload response had no video ID.")
    check = requests.get(Y + "/videos", headers=headers,
        params={"part": "snippet,status", "id": video_id}, timeout=45)
    check.raise_for_status()
    items = check.json().get("items", [])
    if len(items) != 1 or items[0].get("snippet", {}).get("channelId") != channel:
        raise RuntimeError("YouTube upload could not be verified on the owner channel.")
    status = items[0].get("status", {})
    returned_publish_at = status.get("publishAt")
    try:
        returned_time = datetime.fromisoformat(returned_publish_at.replace("Z", "+00:00"))
        requested_time = datetime.fromisoformat(when)
    except (AttributeError, ValueError):
        raise RuntimeError("YouTube did not return a valid scheduled publication time.")
    if status.get("privacyStatus") != "private" or returned_time != requested_time:
        raise RuntimeError("YouTube did not confirm the expected private scheduled status.")
    print(f"VERIFIED: https://www.youtube.com/shorts/{video_id}")
    print(f"SCHEDULED_IST: {when}")
    return video_id


def mark_drive_file(headers, file_id, properties):
    r = requests.patch(D + f"/files/{file_id}", headers={**headers, "Content-Type": "application/json"},
                       params={"supportsAllDrives": "true"},
                       json={"appProperties": properties}, timeout=45)
    r.raise_for_status()


def delete_drive_file(headers, file_id):
    r = requests.delete(D + f"/files/{file_id}", headers=headers,
                        params={"supportsAllDrives": "true"}, timeout=45)
    r.raise_for_status()


def write_summary(message, remaining):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write("# YouTube Shorts Manager\n\n")
            summary.write(message + "\n\n")
            summary.write(f"Unprocessed videos remaining at scan: **{remaining}**\n")
    if remaining <= 2:
        print(f"::warning::Shorts queue is low: {remaining} unprocessed video(s) remain.")


def run():
    folder, channel = need("GDRIVE_INCOMING_FOLDER_ID"), need("GTA_OWNER_CHANNEL_ID")
    headers = auth()
    state = read_state()
    # Retry cleanup for uploads already verified on YouTube.
    for old_file_id, record in list(state["processed"].items()):
        if record.get("cleanup_pending"):
            if DRY_RUN:
                print(f"DRY_RUN: would verify and retry cleanup for Drive file {old_file_id}; no deletion performed.")
                continue
            try:
                delete_drive_file(headers, old_file_id)
            except requests.HTTPError as error:
                if error.response is None or error.response.status_code != 404:
                    raise
            record["cleanup_pending"] = False
            record["deleted_at"] = datetime.now(IST).isoformat()
            save_state(state)

    all_files = drive_files(headers, folder)
    # Drive-side markers survive GitHub cache eviction and stop ambiguous
    # upload outcomes from being blindly retried as duplicate publications.
    for existing in all_files:
        props = existing.get("appProperties") or {}
        if props.get("youtubeVideoId"):
            if DRY_RUN:
                print(f"DRY_RUN: would verify and clean up {existing.get('name')} -> {props['youtubeVideoId']}; no deletion performed.")
            else:
                delete_drive_file(headers, existing["id"])
                print(f"Cleanup verified upload: {existing.get('name')} -> {props['youtubeVideoId']}")
        elif props.get("uploadStartedAt") and existing.get("name", "").lower().startswith("retry_"):
            print(f"Owner requested retry after manual YouTube Studio check: {existing.get('name')}")
            if DRY_RUN:
                print("DRY_RUN: would clear the retry marker only after the owner's manual YouTube Studio check; no changes performed.")
            else:
                mark_drive_file(headers, existing["id"], {})
                existing["appProperties"] = {}
        elif props.get("uploadStartedAt"):
            print(f"MANUAL_REVIEW: previous upload outcome is uncertain for {existing.get('name')}; keeping original and refusing duplicate retry. Check YouTube Studio; prefix the filename with RETRY_ only if no upload exists.")
    todo = [f for f in all_files
            if Path(f.get("name", "")).suffix.lower() in EXT
            and f.get("mimeType", "").startswith("video/")
            and f["id"] not in state["processed"]
            and not (f.get("appProperties") or {}).get("uploadStartedAt")
            and not (f.get("appProperties") or {}).get("youtubeVideoId")]
    print(f"Queue: {len(todo)} unprocessed video(s).")
    if not todo:
        print("QUEUE_EMPTY")
        write_summary("Queue is empty. Add more ready-to-publish Shorts to the Incoming folder.", 0)
        return
    item = todo[0]
    if int(item.get("size") or 0) > MAX_BYTES:
        raise RuntimeError("Video exceeds 5 GiB; original retained in Drive.")
    title = title_for(item["name"])
    scheduled_at = next_slot(state)
    if DRY_RUN:
        print("DRY_RUN: no download, upload, Drive marker update, or deletion will occur.")
        print("DRY_RUN_FILE: " + item["name"])
        print("DRY_RUN_TITLE: " + title)
        print("DRY_RUN_SCHEDULED_IST: " + scheduled_at)
        write_summary(f"Dry run only: would schedule **{title}**. No file was changed.", len(todo))
        return
    local = Path("incoming") / Path(item["name"]).name
    local.parent.mkdir(parents=True, exist_ok=True)
    print("Processing oldest file: " + item["name"])
    download(headers, item["id"], local)
    print("Title: " + title)
    # Mark before contacting YouTube. If the runner loses the response after
    # YouTube accepts the upload, the next run will hold for review, not duplicate it.
    mark_drive_file(headers, item["id"], {"uploadStartedAt": datetime.now(IST).isoformat()})
    video_id = upload(headers, local, title, channel, scheduled_at)
    # Persist verified ID on Drive itself before deleting the original.
    mark_drive_file(headers, item["id"], {
        "uploadStartedAt": datetime.now(IST).isoformat(),
        "youtubeVideoId": video_id,
    })
    # Persist verified ID before deletion. If deletion fails, next run only retries cleanup.
    state["processed"][item["id"]] = {
        "name": item["name"], "youtube_video_id": video_id,
        "title": title, "publish_at": scheduled_at, "verified_at": datetime.now(IST).isoformat(),
        "cleanup_pending": True,
    }
    save_state(state)
    delete_drive_file(headers, item["id"])
    state["processed"][item["id"]]["cleanup_pending"] = False
    state["processed"][item["id"]]["deleted_at"] = datetime.now(IST).isoformat()
    save_state(state)
    print(f"SUCCESS: uploaded, verified, and deleted {item['name']}.")
    remaining = len(todo) - 1
    print(f"QUEUE_REMAINING_AT_SCAN: {remaining}")
    write_summary(f"Successfully scheduled **{title}**. YouTube video ID: `{video_id}`.", remaining)


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"SHORTS_MANAGER_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
