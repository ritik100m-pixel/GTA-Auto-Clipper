# Instructions for AI agents

Use this repository's commands as documented in [README.md](README.md). Keep API keys and OAuth tokens in private environment variables or GitHub Secrets. Never print, ask the user to paste, or write credentials into prompts, logs, metadata, or tracked files.

## Local clip workflow

1. Run `python find_streams.py "GTA 6" --max 25 --json` and show the candidates. YouTube's `viewCount` search order is not a current concurrent-viewer ranking.
2. Use a source only when the user owns it or has permission to record, edit, and publish it. Public availability and creator credit do not grant those rights.
3. Before a local live capture, inspect the actual video and confirm gameplay is visible. Never pass `--gameplay-confirmed` based only on a title, thumbnail, facecam, or commentary.
4. Run `python clip.py URL --gameplay-confirmed --max-seconds 0 --clip-len 30 --lead-in 6 --top 2` only after the source check.
5. Inspect every MP4 and JSON. Confirm lead-in and payoff, gameplay framing, readable/synchronized captions, intelligible audio, accurate creator credit, and safe metadata.
6. Preview publishing before posting. Publish only when the user approved that clip or explicitly enabled unattended publishing for a source with a standing publish permission.
7. After posting, report the actual provider post/video ID and status. If an upload request errors, check the provider dashboard before retrying.

## Cloud automation rules

- Cloud capture uses `SourcePolicy`. A source must be `authorized` or `self_owned`, have `record` and `clip` scopes, and have `gameplay_verified: true`.
- Cloud publishing additionally requires the source's `publish` scope, `GTA_ENABLE_PUBLISHING=true`, and a non-dry-run configuration. Unattended publishing also requires `GTA_UNATTENDED_POSTS=1` and the explicit `--unattended` flag.
- The source allowlist is supplied through the private `GTA_SOURCE_ALLOWLIST_JSON` secret. Do not commit creator permissions or proof references.
- Never bypass the allowlist for a direct URL. Never publish arbitrary files from the output directory; publish only outputs created or recovered by the current batch.
- A channel-level gameplay review is not a visual guarantee for every later stream. Do not enable mixed-content channels unless the user can reliably verify their live content.
- Dry runs must not upload, persist fake post IDs, or mark clips as published.
- A `--test` output is never publishable.
