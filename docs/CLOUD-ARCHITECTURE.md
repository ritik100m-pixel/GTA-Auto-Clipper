# GTA Auto Clipper Cloud: architecture and setup

## What runs in the cloud

The GitHub Actions workflow runs the existing Python clipper on GitHub-hosted Ubuntu runners. Your PC does not need to stay on while a workflow is running. The workflow is scheduled twice daily, at 09:30 and 21:30 India time, and can also be started manually.

Each batch defaults to 5 hours. The workflow has a 5 hour 45 minute job timeout and accepts a maximum requested duration of 330 minutes, leaving time for cleanup and artifact upload. GitHub's hard maximum for a GitHub-hosted job is 6 hours. Scheduled workflows are best-effort and may be delayed by GitHub.

This repository is public. GitHub documents standard GitHub-hosted runners as free for public repositories. This does not make every external service free: YouTube API quotas, OAuth requirements, GitHub artifact/cache storage limits, and platform rules still apply.

## Pipeline

```text
GitHub Actions schedule/manual run
        |
        v
YouTube live-stream discovery
        |
        v
Explicit source allowlist
(record + clip permission + gameplay review)
        |
        v
Persistent job queue and recording sessions
        |
        v
FFmpeg 90-second chunks
        |
        v
Audio / speech / optional live-chat scoring
        |
        v
Vertical 9:16 clip + captions + JSON metadata
        |
        v
Source publish-permission check
        |
        v
Optional YouTube upload and scheduled publish
        |
        v
GitHub artifact for generated clips
```

## Source permission is fail-closed

An empty source allowlist is the default. With no configured sources, discovery may find candidates, but the runner will not record or publish them.

Every source entry must have:
- `permission_status` set to `authorized` or `self_owned`
- `permission_scope` containing the required actions: `record`, `clip`, and, if publishing is intended, `publish`
- `gameplay_verified: true`, meaning you have manually reviewed the channel/source and confirmed it is suitable for GTA gameplay clipping
- A proof reference or note describing the permission basis

A channel-level gameplay review is a standing source check, not a computer-vision guarantee that every future livestream is gameplay. Do not enable mixed-content channels unless you can reliably verify their streams. Remove or revoke a source if its permission changes. Creator credit alone is not permission.

The allowlist is supplied through the private GitHub Actions secret `GTA_SOURCE_ALLOWLIST_JSON`; it is not committed to the public repository, cached, or uploaded as an artifact. The workflow also removes any legacy cached allowlist before starting. Example structure:

```json
{
  "version": 1,
  "sources": {
    "UCxxxxxxxxxxxxxxxxxxxxxx": {
      "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx",
      "channel_name": "Creator name",
      "permission_status": "authorized",
      "permission_scope": ["record", "clip", "publish"],
      "gameplay_verified": true,
      "proof_reference": "Link or note documenting permission",
      "credit": "Creator name"
    }
  }
}
```

Do not add a creator until you have permission to record, edit, and publish their footage. No channel is configured in the repository by default.

## GitHub Actions setup

### Required secrets

Add these under **Repository Settings → Secrets and variables → Actions → Secrets** when you are ready to configure the system:

| Secret | Purpose |
|---|---|
| `YOUTUBE_API_KEY` | Required for automatic YouTube livestream discovery |
| `GTA_SOURCE_ALLOWLIST_JSON` | Private JSON source policy described above |
| `YOUTUBE_CLIENT_ID` | Google OAuth client ID for direct YouTube publishing |
| `YOUTUBE_CLIENT_SECRET` | Google OAuth client secret |
| `YOUTUBE_REFRESH_TOKEN` | Refresh token authorized for `https://www.googleapis.com/auth/youtube.upload` |
| `GTA_UNATTENDED_POSTS` | Set to `1` only after you deliberately enable unattended publishing |

Keep credentials in GitHub Secrets. Never put them in source files, workflow logs, prompts, or the source allowlist.

### Repository variables

Add these under **Repository Settings → Secrets and variables → Actions → Variables**:

| Variable | Safe default | Meaning |
|---|---|---|
| `GTA_ENABLE_PUBLISHING` | `false` | Allows scheduled workflow runs to publish eligible clips |
| `GTA_DRY_RUN` | `true` | Keeps scheduled runs in preview mode until deliberately changed |
| `GTA_UPLOAD_OUTPUT_ARTIFACTS` | `false` | Opts into public downloadable clip artifacts; leave false unless every clip is cleared for public access |

For scheduled unattended YouTube publishing, all of the following must be deliberately configured:
1. A valid `GTA_SOURCE_ALLOWLIST_JSON` with record, clip, and publish permission for each source.
2. Working YouTube OAuth secrets.
3. Repository variable `GTA_ENABLE_PUBLISHING=true`.
4. Repository variable `GTA_DRY_RUN=false`.
5. Secret `GTA_UNATTENDED_POSTS=1`.

Without these settings, publishing stays disabled or in dry-run mode. Manual workflow runs also default to dry-run with publishing disabled. The cloud runner only considers clips created or recovered in that batch; it does not sweep and upload arbitrary old files from the output directory.

### First test

After the workflow is merged into the default branch:
1. Open **Actions → GTA Cloud Auto-Clipper → Run workflow**.
2. Set duration to 15 minutes and target clips to 1.
3. Leave dry-run enabled and publishing disabled.
4. Check the run summary and inspect the generated-clips artifact.

With no authorized source configured, a successful first run is expected to report no authorized streams and produce no clips. This confirms the default safety gate without capturing anyone's footage.

## Runtime, persistence, and storage

- Standard GitHub-hosted Ubuntu runners are free for public repositories. Each job is limited to 6 hours; this workflow stops earlier.
- The workflow caches only the queue database and a deduplication ledger containing one-way hashes of transcript tokens. The private source allowlist, raw recordings, readable transcripts, YouTube credentials, and analytics data are not cached or uploaded. Cache is best-effort storage and can be evicted; it is not a permanent database.
- The scheduler currently uses default IST audience-hour priors and spreads clips apart within each batch. Importing actual YouTube Analytics performance data is a later enhancement, so it does not yet learn from channel metrics automatically.
- Raw recordings and recording-session databases are temporary within one runner job. Completed raw chunks are deleted after their MP4 and metadata outputs are saved. If a runner stops before a chunk is processed, that raw chunk is not carried into the next workflow run; a later batch must rediscover and recapture it.
- Clip artifacts are disabled by default because anyone with read access to this public repository can download them. You can opt in with the manual `upload_output_artifact` input or repository variable `GTA_UPLOAD_OUTPUT_ARTIFACTS=true`. Before upload, the workflow checks that every clip has active `publish` permission in the private source allowlist; test clips and unapproved sources are rejected. Opt-in artifacts expire after 1 day. GitHub Free has a 500 MB artifact-storage allowance and a 10 GB cache limit.
- The workflow runs twice daily, not continuously. It can miss a livestream that starts and ends between scheduled runs. The 10-clip target is a goal, not a guarantee; source availability, clip quality, processing speed, API quotas, and platform limits can reduce the daily count.

## Publishing scope

The default cloud route is the direct YouTube Data API publisher. It supports OAuth refresh, resumable upload, duplicate protection, and scheduled publishing using YouTube's private-video plus `publishAt` flow. Zernio and Instagram integrations remain optional code paths and are not required for the ₹0 YouTube workflow.

A dry run is side-effect-free: it does not upload a video, save a fake video ID, or mark a clip as published. A real upload writes the returned YouTube video ID to the clip metadata so later batches will not upload it again.

## Tests

The repository's GitHub Actions test workflow covers:
- Persistent job queue and worker leases
- Recording-session recovery and raw-chunk cleanup
- Clipper framing, captions, and moment selection
- Live-chat fallback and reaction scoring
- Source allowlist and permission gates
- Content classification and scheduling
- YouTube publisher dry-run and duplicate protection
- Cloud batch orchestration and publishing opt-in

Run the suite locally with:

```powershell
python -m unittest discover -s tests -v
```
