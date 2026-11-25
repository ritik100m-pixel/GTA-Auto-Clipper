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

**OAuth lifetime warning:** if the Google OAuth consent screen is configured as
External and its publishing status is Testing, refresh tokens for these non-basic
scopes expire after 7 days. For a cloud service that should run continuously, do
not assume a Testing refresh token is permanent. Google documents this behavior
in its [OAuth 2.0 refresh-token guidance](https://developers.google.com/identity/protocols/oauth2). For a personal-only app, review Google's personal-use exception and app publishing requirements before changing the status; do not publish or request verification blindly.

## Enable only after setup

Add repository Actions variable `SHORTS_MANAGER_ENABLED` with value `true` only after the OAuth refresh token has both scopes, the folder ID is configured, and a supervised test upload succeeds. Until then the scheduled workflow job is skipped.

## Operation

- Scheduled check every 30 minutes; GitHub may delay scheduled runs.
- Reads video files in the dedicated folder, oldest first.
- Uploads at most one Short per run.
- Creates a clean title from the filename and appends `#Shorts`.
- Schedules for the next India posting slot, 11:30 or 19:30.
- Verifies the YouTube video ID, destination channel, private status, and actual scheduled time before deleting the Drive original.
- Stores a file-ID/video-ID ledger to prevent duplicate uploads.
- A failed download or definite upload failure leaves the original in Drive. A timeout or interrupted upload after the upload-start marker is treated as uncertain and held for manual review rather than blindly retried, to avoid duplicate publication.
- Drive appProperties record upload-start and verified YouTube video IDs, so cleanup markers survive GitHub Actions cache eviction. The workflow still uses the Actions cache as a secondary local ledger.
- Maximum file size is 5 GiB. Supported extensions: MP4, MOV, M4V, WebM, MKV.

## Important limitations and safeguards

Use a dedicated folder containing only videos approved for automatic publication.
The OAuth Drive scope is broad enough to list and delete files, so do not point it
at a folder containing personal or unrelated files.

This workflow does not edit or transcode videos. Upload finished vertical Shorts.
Titles are deterministically cleaned from filenames, not AI-generated. The uploader detects the media MIME type from the filename and verifies YouTube's returned scheduled timestamp independent of timezone formatting. A free
GitHub runner cannot call ChatGPT as an operator in the background. The workflow
does not spend money or use paid AI services.

The GitHub Actions cache is best-effort, not permanent storage. Drive-side appProperties are used as the durable per-file marker while the original file remains in the Incoming folder. If an upload result is uncertain, the workflow intentionally stops for manual reconciliation rather than retrying automatically. Keep an eye on YouTube Studio
and do not manually re-upload an item whose result is uncertain. Before enabling unattended publishing for
the first time, test with one non-critical video and verify the scheduled item
in YouTube Studio. YouTube API quota and channel restrictions still apply.

## Validation

Pull requests run Python compilation and unit tests for title cleanup, schedule spacing, state corruption handling, empty queues, and uncertain-upload duplicate protection. A real Drive/YouTube end-to-end test is still required before enabling unattended runs.

## Handling an uncertain upload

If a run logs `MANUAL_REVIEW`, do not immediately re-upload that file. Open YouTube Studio and check the channel's uploads and scheduled/private videos around the run time. If the video exists, leave the Drive marker in place so the manager can clean up the original. Only if you have confirmed that no corresponding upload exists, rename the Drive file by adding the prefix `RETRY_` (for example, `RETRY_GTA funny moment.mp4`). The next run clears the uncertain marker and allows one deliberate retry.

Low queue status is written to the GitHub Actions run summary and emitted as an Actions warning when two or fewer unprocessed videos remain. This is not yet a direct phone notification.
