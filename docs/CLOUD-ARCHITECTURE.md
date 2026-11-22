# Cloud workflow

The current primary workflow is **Daily Gameplay Video to Shorts**. It accepts one Unlisted YouTube video URL per manual GitHub Actions run, verifies that the source channel matches `GTA_OWNER_CHANNEL_ID`, requires a rights confirmation, creates Shorts, and can schedule them through the direct YouTube Data API.

The old livestream-discovery architecture is no longer the default workflow. Its code remains in the repository for possible later use when explicitly authorized creator channels are added.

See [DAILY-GAMEPLAY-WORKFLOW.md](DAILY-GAMEPLAY-WORKFLOW.md) for the current end-to-end setup and operational details.
