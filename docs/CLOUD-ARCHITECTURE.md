# GTA Auto Clipper Cloud Architecture & Deployment Runbook

## 1. System Overview

GTA Auto Clipper Cloud is an autonomous, cloud-native clipping and distribution pipeline engineered to record authorized Grand Theft Auto gameplay live streams, detect high-energy moments (chases, stunts, fails, close escapes), format them into vertical 9:16 Shorts with dynamic captions, and publish or schedule them to YouTube and Instagram with **strictly ₹0 operating costs**.

The system is designed so that **the user's personal PC does not need to remain switched on** after deployment. Execution is performed on GitHub Actions runners within free-tier quotas and platform constraints.

```
+---------------------------------------------------------------------------------------------+
|                                    GITHUB ACTIONS RUNNER                                    |
|                                                                                             |
|  +--------------------+         +------------------------+         +---------------------+  |
|  | Stream Discovery   |  -----> | Source Policy & Consent|  -----> | Stream Recording    |  |
|  | (yt-dlp live scan) |         | (source_policy.py)     |         | (ffmpeg segmenter)  |  |
|  +--------------------+         +------------------------+         +---------------------+  |
|                                                                               |             |
|                                                                               v             |
|  +--------------------+         +------------------------+         +---------------------+  |
|  | Content Classifier |  <----- | Vertical Clip Renderer |  <----- | Persistent Session  |  |
|  | & Deduplication    |         | (smart crop + subtitle)|         | (recording_session) |  |
|  +--------------------+         +------------------------+         +---------------------+  |
|            |                                                                                |
|            v                                                                                |
|  +--------------------+         +------------------------+         +---------------------+  |
|  | Smart Scheduler    |  -----> | Multi-Platform Publish |  -----> | Cache & Artifacts   |  |
|  | (Audience analytics|         | (YouTube Data API v3,  |         | (Output clips, logs,|  |
|  |  Epsilon-greedy)   |         |  Instagram, Zernio)    |         |  DB persistence)    |  |
|  +--------------------+         +------------------------+         +---------------------+  |
+---------------------------------------------------------------------------------------------+
```

---

## 2. Core Components & Architecture

### 2.1 Persistent Recording & Session Recovery (`recording_session.py`, `clip.py`)
- **Session Identification:** Streams derive a deterministic session ID (`sha256(source_url)[:16]`) so interrupted runs seamlessly attach to existing state.
- **SQLite State Database:** Backed by SQLite in WAL mode (`recordings/sessions.db`) tracking captures, chunk files, worker leases, processing status (`pending`, `claimed`, `completed`, `failed`), and rendered clip paths.
- **Lease Heartbeats & Abandonment Recovery:** Workers obtain renewable leases (default 300s). Crashed workers have their leases reclaimed automatically by subsequent batches without duplicating work.
- **Unregistered Chunk Discovery:** Chunks written to disk by FFmpeg before an ungraceful termination are scanned and registered into the session on startup (`recover_unregistered_chunks`).
- **Safe Retention Pruning:** Raw `.ts` recording chunks are only deleted once clipping and metadata extraction are fully completed and verified.

### 2.2 Content Classification & Deduplication (`content_classifier.py`)
- **GTA Event Taxonomy:**
  - `police_chase`: Star levels, police scanner phrases, sirens, evasion.
  - `stunt`: Jumps, loops, aerial maneuvers, skydives.
  - `fail`: Crashes, death screen ("wasted"), explosions, blunders.
  - `trolling_funny`: Laughing, clowning, hilarious interactions.
  - `close_escape`: Near misses, last-second survivals.
  - `story_moment`: Cutscenes, dialogue, mission completions.
  - `high_action`: Shootouts, combat, high-speed driving.
- **Audio & Visual Deduplication (`ClipDeduplicator`):**
  - Uses perceptual hashing (dHash/pHash) and transcript normalized similarity.
  - Prevents posting duplicate or near-identical clips across different batches.

### 2.3 Smart Upload Scheduler (`smart_scheduler.py`)
- **Analytics Ledger:** Records publishing timestamp, day of week, hour of day (UTC/IST), platform, category, and historical engagement (views, watch time, completion rate).
- **Adaptive Exploration (Epsilon-Greedy):**
  - **Exploitation (1 - ε):** Schedules uploads during historically proven peak audience retention windows.
  - **Exploration (ε = 0.20):** Selects unexplored or low-data slots to discover emerging audience peaks without guessing.
- **Category Balancing:** Ensures varied content streams by rotating categories and avoiding clustering identical styles together.

### 2.4 Multi-Provider Publishing Architecture (`publisher.py`, `youtube_publish.py`, `instagram_publish.py`, `zernio_publish.py`)
- **Direct YouTube Data API v3 (`youtube_publish.py`):**
  - Native Google OAuth 2.0 with automatic token refresh (`refresh_token`).
  - Resumable video upload via Google upload endpoint (`upload/youtube/v3/videos?uploadType=resumable`).
  - Configures title, tags, description, and vertical Shorts optimization.
  - Supports scheduled publication via `status.publishAt` (automatically setting `privacyStatus='private'`).
  - Strict deduplication ledger preventing re-uploading published video IDs.
- **Meta Instagram Graph API (`instagram_publish.py`):**
  - Official two-step Reels container protocol (`/media` container creation -> status polling -> `/media_publish`).
  - Evaluates account eligibility (Instagram Professional Creator/Business account linked to a Facebook Page).
  - Documents public hosting requirements for media container ingestion.
- **Zernio API Integration (`zernio_publish.py`):**
  - Optional multi-platform gateway supporting simultaneous YouTube and Instagram publishing.
- **Dry-Run Engine:**
  - Safely simulates all publishing steps without making external network mutations or consuming API quotas.

### 2.5 Cloud Batch Runner (`cloud_runner.py`)
- **Master Orchestrator:**
  - Discovers live streams using `yt-dlp`.
  - Evaluates channels against `source_policy.py` allowlists and copyright consent rules.
  - Enqueues jobs into `cloud_queue.py`.
  - Executes chunk recording and persistent session recovery.
  - Runs clipping, vertical layout transformation, and audio transcription.
  - Classifies moments and filters duplicate clips.
  - Consults `smart_scheduler.py` for target publish times.
  - Posts or schedules via `UnifiedPublisher`.
  - Records batch metrics and safely cleans up temporary disk storage.

---

## 3. GitHub Actions Automation Workflow (`.github/workflows/cloud-autoclipper.yml`)

The cloud workflow runs automatically on a scheduled cron or on-demand dispatch:

### 3.1 Schedule & Limits
- **Cron Schedule:** `0 4,16 * * *` (Twice daily at 04:00 UTC and 16:00 UTC / 09:30 and 21:30 IST).
- **Execution Budget:** GitHub Actions free tier provides **2,000 Linux runner minutes/month** for public/private repositories.
- **Safety Cutoff:** Each runner job has a hard limit of 360 minutes (6 hours). The batch runner defaults to **300 minutes (5 hours)**, ensuring clean shutdown, cache serialization, and artifact preservation well before the runner deadline.
- **Compliance:** Absolutely no infinite job loops or runner chaining. Each batch completes independently and persists state.

### 3.2 State Preservation & Caching
- **`actions/cache@v4`:** Restores and persists `cloud_state/` (scheduler ledger, publish deduplication ledger, session database) across consecutive workflow runs.
- **`actions/upload-artifact@v4`:** Uploads all generated clips and JSON metadata as downloadable artifacts retained for 14 days, providing an immutable archive of produced content.

---

## 4. Setup & Deployment Runbook (Strictly ₹0 Budget)

### Step 1: Create Google Cloud Project (100% Free)
1. Navigate to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (e.g., `gta-cloud-clipper`).
3. Enable the **YouTube Data API v3**.
4. Configure the **OAuth Consent Screen**:
   - User Type: External.
   - Publishing Status: Testing.
   - Add your own Google Account email as a **Test User**.
5. Create Credentials:
   - Type: **OAuth 2.0 Client ID**.
   - Application Type: **Desktop Application** or **Web Application**.
   - Note down `Client ID` and `Client Secret`.

### Step 2: Generate OAuth Refresh Token
Run a one-time local token exchange script (or use Google OAuth 2.0 Playground) with scope `https://www.googleapis.com/auth/youtube.upload`:
- Authorize with your channel's test user account.
- Securely copy the resulting `refresh_token`.

### Step 3: Configure GitHub Repository Secrets
In your GitHub repository (`https://github.com/ritik100m-pixel/GTA-Auto-Clipper/settings/secrets/actions`), add the following secrets:

| Secret Name | Description | Required? |
|---|---|---|
| `YOUTUBE_CLIENT_ID` | Google OAuth Client ID | Yes (for direct YT) |
| `YOUTUBE_CLIENT_SECRET` | Google OAuth Client Secret | Yes (for direct YT) |
| `YOUTUBE_REFRESH_TOKEN` | Google OAuth Refresh Token | Yes (for direct YT) |
| `YOUTUBE_API_KEY` | Optional public API key | Optional |
| `ZERNIO_API_KEY` | Zernio API Key | Optional (if using Zernio) |
| `ZERNIO_YOUTUBE_ACCOUNT_ID` | Zernio YouTube Account ID | Optional |
| `ZERNIO_INSTAGRAM_ACCOUNT_ID` | Zernio Instagram Account ID | Optional |

### Step 4: Configure Source Channels (`source_policy.json`)
Ensure you have permission to clip target creators. Add allowed channel IDs or handles to `cloud_state/source_policy.json`:
```json
{
  "allowed_channels": [
    "UCxxxxxxxxxxxxxxxxxxxxxx",
    "@YourAuthorizedStreamer"
  ],
  "require_allowlist": false,
  "credit_template": "Gameplay by {creator} | Watch live: {url}"
}
```

### Step 5: Trigger Initial Test Run
1. Go to the **Actions** tab on GitHub.
2. Select **GTA Cloud Auto-Clipper**.
3. Click **Run workflow**:
   - Set `duration_minutes`: `15` (for a quick verification run).
   - Set `target_clips`: `1`.
   - Set `dry_run`: `true` (validates clipping and scheduling without publishing).
4. Verify all steps complete green and inspect the output artifacts.

---

## 5. Verification & Testing Matrix

The repository contains 10 comprehensive unit test suites:

1. `tests/test_cloud_queue.py`: Job enqueuing, priority sorting, worker leases, heartbeats, and retry counts.
2. `tests/test_recording_session.py`: Persistent database operations, chunk recovery, atomic commits, lease expiration.
3. `tests/test_clipper.py`: Video framing, smart crop offset, whisper word alignment, chunk processing, recovery mode.
4. `tests/test_live_chat.py`: Live chat collection, chatter reaction density, moment scoring.
5. `tests/test_publisher.py`: Publisher routing, payload construction, dry-run simulation.
6. `tests/test_source_policy.py`: Channel allowlist filtering, copyright consent validation, credit string formatting.
7. `tests/test_content_classifier.py`: GTA category heuristics, confidence calculation, perceptual hash deduplication.
8. `tests/test_smart_scheduler.py`: Analytics ledger updates, epsilon-greedy exploration, peak time calculation.
9. `tests/test_youtube_publisher.py`: Direct YouTube Data API v3 token refresh, snippet formatting, duplicate prevention.
10. `tests/test_cloud_runner.py`: Stream filtering, persistent queueing, batch execution lifecycle.
