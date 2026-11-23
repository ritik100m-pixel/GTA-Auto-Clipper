# YouTube Shorts Manager

This workflow handles finished Shorts already placed in one dedicated Google
Drive folder. It is separate from the gameplay clipping workflow.

## Required GitHub Actions secrets

- `GDRIVE_INCOMING_FOLDER_ID`: ID of the dedicated Incoming folder.
- `GTA_OWNER_CHANNEL_ID`: destination YouTube channel ID.
- `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET`.
- `YOUTUBE_REFRESH_TOKEN`: authorize the same OAuth client with BOTH scopes:
  - `https://www.googleapis.com/auth/youtube.upload`
  - `https://www.googleapis.com/auth/drive`

The old refresh token may only have YouTube upload scope. It must be re-authorized
with Drive scope before this manager can list and delete Drive files. Never put
credentials in the repository or chat.

## Operation

- Scheduled check every 30 minutes; GitHub may delay scheduled runs.
- Reads video files in the dedicated folder, oldest first.
- Uploads at most one Short per run.
- Creates a clean title from the filename and appends `#Shorts`.
- Schedules for the next India posting slot, 11:30 or 19:30.
- Verifies the YouTube video ID and destination channel before deleting the
  Drive original.
- Stores a file-ID/video-ID ledger to prevent duplicate uploads.
- A failed download, upload, or verification leaves the original in Drive.
- Maximum file size is 5 GiB. Supported extensions: MP4, MOV, M4V, WebM, MKV.

## Important limitations and safeguards

Use a dedicated folder containing only videos approved for automatic publication.
The OAuth Drive scope is broad enough to list and delete files, so do not point it
at a folder containing personal or unrelated files.

This workflow does not edit or transcode videos. Upload finished vertical Shorts.
The filename-based title cleanup is deterministic, not AI-generated. A free
GitHub runner cannot call ChatGPT as an operator in the background. The workflow
does not spend money or use paid AI services.

The workflow uses GitHub Actions cache for its duplicate ledger. Cache is
best-effort, not permanent storage. Before enabling unattended publishing for
the first time, test with one non-critical video and verify the scheduled item
in YouTube Studio. YouTube API quota and channel restrictions still apply.
