# Daily gameplay video to Shorts

## Daily use

1. Upload your gameplay video to Google Drive.
2. In Drive, open **Share → General access → Anyone with the link → Viewer**, then copy the link. The temporary GitHub runner needs this access to download the file.
3. Open **Actions → Daily Gameplay Video to Shorts → Run workflow**.
4. Set `source_mode = google_drive` and paste the link into `drive_url`. Confirm rights and choose the target clip count.
5. For the first test, leave dry-run enabled and publishing disabled. The workflow accepts a video up to 5 GiB and 90 minutes.

The workflow accepts one completed regular YouTube video per run. It does not search for streams or process random creators. The configured owner-channel ID must match the channel that uploaded the source video.

GitHub Actions does not provide a binary file picker in the Run workflow form. Google Drive is the recommended source for large videos; GitHub Release assets remain available for files under GitHub's per-asset limit. Google Drive links set to "Anyone with the link" are accessible to anyone who obtains the link, so share them carefully and remove link access after processing if desired. YouTube URL mode remains available as an alternative, but GitHub runners may be blocked by YouTube bot checks.

## One-time GitHub setup

Open **Repository Settings → Secrets and variables → Actions → Secrets** and add:

| Secret | Purpose |
|---|---|
| `GTA_OWNER_CHANNEL_ID` | Your YouTube channel ID (must match the source video's channel) |
| `YOUTUBE_CLIENT_ID` | Google OAuth client ID for direct YouTube publishing |
| `YOUTUBE_CLIENT_SECRET` | Google OAuth client secret |
| `YOUTUBE_REFRESH_TOKEN` | OAuth refresh token with the `youtube.upload` scope |
| `GTA_UNATTENDED_POSTS` | Set to `1` only when you want the workflow to schedule clips without approving each one |
| `YOUTUBE_API_KEY` | Optional; used for public performance snapshots and schedule learning |

Keep every credential in GitHub Secrets. Never commit tokens, OAuth files, or private creator permissions.

### Publishing setup

The direct YouTube Data API publisher is used; Zernio and Instagram are not required.

For a real unattended schedule:
- Add valid YouTube OAuth credentials.
- Set `GTA_UNATTENDED_POSTS=1`.
- In the workflow form, set **dry_run = false**, **enable_publishing = true**, and **unattended = true**.

The safe default is dry-run with publishing disabled. The script rejects unattended publishing unless the secret is enabled and verifies that the submitted video belongs to `GTA_OWNER_CHANNEL_ID`. YouTube's API supports setting `status.publishAt` when uploading a private video for future publication. The API's own quota, OAuth verification, account, and upload restrictions still apply.

## What the workflow does

- Accepts a Google Drive sharing link (recommended, up to 5 GiB), a GitHub Release asset, or optionally a YouTube URL.
- For a file upload, checks that FFprobe can read a video stream and that its duration is at most 90 minutes. The workflow also requires a manual rights confirmation and uses the configured owner channel for publishing.
- Downloads the selected Google Drive file or GitHub Release asset into a temporary runner folder.
- Splits it into 90-second chunks and reuses the repository's existing audio/speech moment detection, vertical 9:16 rendering, captions, content checks, and deduplication.
- Schedules only clips created by the current run, using the existing measured posting-time scheduler.
- Deletes the original downloaded copy, chunks, generated MP4/JSON files, queue database, deduplication state, and other temporary processing data at the end of the job, including after failure.
- Keeps only the small YouTube performance/scheduling ledgers in GitHub Actions cache so future runs can learn from published results. Cache is best-effort and can be evicted.

The source video uploaded to YouTube is not deleted. The workflow deletes its temporary runner copy only. Its Unlisted source URL is masked in workflow logs and is deliberately omitted from the public Shorts description. A failed run does not publish anything unless YouTube has already accepted an upload; check the YouTube Studio dashboard before retrying after an ambiguous network failure.

## Runtime and cost

The workflow uses a GitHub-hosted Ubuntu runner and is configured for a maximum 5 hours 45 minutes, below GitHub's 6-hour hosted-job ceiling. The current source-video limit is 90 minutes. Long videos or slow rendering may produce fewer clips or time out. GitHub schedules are not continuous, and the number of clips is a target, not a guarantee.

This repository is public. GitHub-hosted standard runners for public repositories are generally free, but YouTube API quota, account eligibility, and service limits still apply. This is designed for a ₹0 budget and fails rather than silently switching to a paid service.

## Troubleshooting

- **Owner channel mismatch:** the source video must be uploaded by the YouTube channel whose ID is stored in `GTA_OWNER_CHANNEL_ID`.
- **Google Drive download failed:** set General access to "Anyone with the link" and permission to Viewer. Paste the normal file sharing URL into `drive_url`; do not use a folder link.
- **Google Drive file too large:** the workflow limit is 5 GiB; runner disk space and processing time can still constrain very large files.
- **GitHub Release asset not found:** check the exact release tag and filename. Defaults are tag `gameplay-upload` and asset `gameplay.mp4`.
- **YouTube URL mode is blocked:** YouTube may challenge GitHub-hosted runner IPs. Use GitHub Release mode instead; it does not use yt-dlp or browser cookies.
- **OAuth not configured:** dry-run can still validate processing, but real scheduling requires the three YouTube OAuth secrets.
- **No clips found:** the video may not contain sufficiently strong moments under the current scoring threshold. Try a different gameplay recording or reduce the target; the system will not invent highlights.
- **Publishing error:** check YouTube Studio before retrying. The publisher records returned YouTube video IDs to prevent duplicate uploads.

## Tests

Run the full test suite with:

```bash
python -m unittest discover -s tests -v
```
