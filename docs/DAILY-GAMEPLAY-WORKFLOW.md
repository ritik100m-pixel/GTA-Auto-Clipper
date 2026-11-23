# Daily gameplay video to Shorts

## Daily use

1. Upload your gameplay recording to your own YouTube channel as **Unlisted**.
2. Open **Actions → Daily Gameplay Video to Shorts → Run workflow**.
3. Paste the unlisted video URL, confirm that you own the footage or have explicit rights to edit and publish it, and choose the target number of clips.
4. For the first test, leave dry-run enabled and publishing disabled.
5. After the YouTube credentials are configured and the test is successful, enable publishing and unattended scheduling.

The workflow accepts one completed regular YouTube video per run. It does not search for streams or process random creators. The configured owner-channel ID must match the channel that uploaded the source video.

GitHub's workflow-dispatch form accepts text inputs, not a binary video upload. Using an unlisted YouTube URL is the first supported upload handoff. The source video remains Unlisted in your YouTube account; the runner downloads a temporary working copy.

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

- Reads metadata for the submitted YouTube video and verifies it is a single completed upload, not a livestream or playlist.
- Verifies the video channel ID matches the configured owner channel.
- Requires a manual rights confirmation in the workflow form.
- Downloads the video into a temporary directory (maximum source duration: 90 minutes).
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
- **yt-dlp cannot download:** check that the video is Unlisted (not Private), processing has finished on YouTube, and the URL is correct. The downloader retries YouTube's sign-in/bot challenge once with the `web_safari` player client. It does not use browser cookies, account sessions, proxies, or bypass access controls. If YouTube still blocks the GitHub-hosted runner, use the supported alternative source-file handoff rather than exporting your primary account cookies.
- **OAuth not configured:** dry-run can still validate processing, but real scheduling requires the three YouTube OAuth secrets.
- **No clips found:** the video may not contain sufficiently strong moments under the current scoring threshold. Try a different gameplay recording or reduce the target; the system will not invent highlights.
- **Publishing error:** check YouTube Studio before retrying. The publisher records returned YouTube video IDs to prevent duplicate uploads.

## Tests

Run the full test suite with:

```bash
python -m unittest discover -s tests -v
```
