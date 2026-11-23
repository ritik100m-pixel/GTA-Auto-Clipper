"""Drive queue -> scheduled YouTube Shorts uploader with verify-before-delete."""
import json, os, re, sys
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
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def title_for(filename):
    s = Path(filename).stem.replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip(" .-_")
    s = re.sub(r"(?i)\b(final|export|render|video)\b", "", s)
    s = re.sub(r"\s+", " ", s).strip() or "Epic Gaming Moment"
    s = s[:90].rstrip()
    return (s if "#shorts" in s.lower() else s + " #Shorts")[:100]


def next_slot():
    now = datetime.now(IST)
    for day in range(3):
        date = (now + timedelta(days=day)).date()
        for hour in (11, 19):
            slot = datetime(date.year, date.month, date.day, hour, 30, tzinfo=IST)
            if slot > now + timedelta(minutes=10):
                return slot.isoformat()
    raise RuntimeError("No future posting slot found")


def read_state():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE.exists():
        return {"version": 1, "processed": {}}
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
            "fields": "nextPageToken,files(id,name,mimeType,size)",
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


def upload(headers, path, title, channel):
    when = next_slot()
    meta = {
        "snippet": {"title": title,
                    "description": "Gaming highlights.\n\n#Shorts #Gaming",
                    "tags": ["Shorts", "Gaming", "Gameplay"], "categoryId": "20"},
        "status": {"privacyStatus": "private", "publishAt": when,
                   "selfDeclaredMadeForKids": False},
    }
    size = path.stat().st_size
    init = requests.post(Y + "/videos", headers={
        **headers, "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4",
    }, params={"uploadType": "resumable", "part": "snippet,status"},
       json=meta, timeout=60)
    init.raise_for_status()
    url = init.headers.get("Location")
    if not url:
        raise RuntimeError("YouTube did not provide a resumable upload URL.")
    with path.open("rb") as f:
        sent = requests.put(url, data=f,
            headers={"Content-Length": str(size), "Content-Type": "video/mp4"},
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
    print(f"VERIFIED: https://www.youtube.com/shorts/{video_id}")
    print(f"SCHEDULED_IST: {when}")
    return video_id


def delete_drive_file(headers, file_id):
    r = requests.delete(D + f"/files/{file_id}", headers=headers,
                        params={"supportsAllDrives": "true"}, timeout=45)
    r.raise_for_status()


def run():
    folder, channel = need("GDRIVE_INCOMING_FOLDER_ID"), need("GTA_OWNER_CHANNEL_ID")
    headers = auth()
    state = read_state()
    all_files = drive_files(headers, folder)
    todo = [f for f in all_files
            if Path(f.get("name", "")).suffix.lower() in EXT
            and f.get("mimeType", "").startswith("video/")
            and f["id"] not in state["processed"]]
    print(f"Queue: {len(todo)} unprocessed video(s).")
    if not todo:
        print("QUEUE_EMPTY")
        return
    item = todo[0]
    if int(item.get("size") or 0) > MAX_BYTES:
        raise RuntimeError("Video exceeds 5 GiB; original retained in Drive.")
    local = Path("incoming") / Path(item["name"]).name
    local.parent.mkdir(parents=True, exist_ok=True)
    print("Processing oldest file: " + item["name"])
    download(headers, item["id"], local)
    title = title_for(item["name"])
    print("Title: " + title)
    video_id = upload(headers, local, title, channel)
    # Persist verified ID before deletion. If deletion fails, next run only retries cleanup.
    state["processed"][item["id"]] = {
        "name": item["name"], "youtube_video_id": video_id,
        "title": title, "verified_at": datetime.now(IST).isoformat(),
        "cleanup_pending": True,
    }
    save_state(state)
    delete_drive_file(headers, item["id"])
    state["processed"][item["id"]]["cleanup_pending"] = False
    state["processed"][item["id"]]["deleted_at"] = datetime.now(IST).isoformat()
    save_state(state)
    print(f"SUCCESS: uploaded, verified, and deleted {item['name']}.")
    print(f"QUEUE_REMAINING_AT_SCAN: {len(todo)-1}")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"SHORTS_MANAGER_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
