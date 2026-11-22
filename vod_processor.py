"""Finite YouTube VOD download and segmentation helpers.

Only use this module for videos the user is authorized to download, edit,
and publish. It deliberately does not use browser cookies, account sessions,
proxies, or other access-control workarounds.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def ytdlp_cmd(*args):
    """Build a yt-dlp command with an installed JavaScript runtime if available."""
    command = [sys.executable, "-m", "yt_dlp"]
    if not shutil.which("deno"):
        for runtime in ("node", "bun"):
            if shutil.which(runtime):
                command += ["--js-runtimes", runtime]
                break
    return command + list(args)


def _run_checked(command, runner, description):
    result = runner(command, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stderr or result.stdout or "No diagnostic output returned.")[-800:].strip()
        raise RuntimeError(f"{description}: {detail}")
    return result


def _validate_youtube_url(url):
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    allowed_host = (
        host == "youtu.be"
        or host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtube-nocookie.com"
        or host.endswith(".youtube-nocookie.com")
    )
    if parsed.scheme != "https" or not allowed_host:
        raise ValueError("Only HTTPS YouTube video URLs are supported.")
    if host.endswith("youtube.com") and parsed.path not in ("/watch",) and not parsed.path.startswith(("/shorts/", "/live/")):
        raise ValueError("Provide a direct YouTube video URL, not a playlist or channel URL.")


def download_youtube_video(url, destination_dir, *, expected_video_id=None,
                           expected_channel_id=None, max_duration_seconds=5400,
                           runner=subprocess.run):
    """Download one finite video and return (metadata, local_path).

    Does not use cookies or attempt to bypass YouTube bot/access challenges.
    """
    _validate_youtube_url(url)
    if int(max_duration_seconds) < 1:
        raise ValueError("max_duration_seconds must be positive.")

    destination = Path(destination_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)

    metadata_result = _run_checked(
        ytdlp_cmd("--dump-single-json", "--no-download", "--no-playlist",
                  "--no-warnings", str(url)),
        runner,
        "Could not read YouTube video metadata",
    )
    try:
        info = json.loads(metadata_result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("yt-dlp returned invalid video metadata.") from exc

    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "was_live"}:
        raise ValueError("This is a livestream or livestream archive; finite VOD processing requires a regular upload.")
    if info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
        raise ValueError("Playlists are not accepted; provide one video URL.")

    video_id = str(info.get("id") or "")
    channel_id = str(info.get("channel_id") or "")
    duration = info.get("duration")
    if expected_video_id and video_id != str(expected_video_id):
        raise PermissionError("The resolved YouTube video ID does not match the discovered video.")
    if expected_channel_id and channel_id != str(expected_channel_id):
        raise PermissionError("The resolved YouTube channel does not match the authorized source.")
    if duration is None or float(duration) <= 0:
        raise ValueError("Video duration is missing or invalid.")
    if float(duration) > int(max_duration_seconds):
        raise ValueError(
            f"Video is {int(float(duration))} seconds; the configured limit is {int(max_duration_seconds)} seconds."
        )

    output_template = str(destination / "source.%(ext)s")
    _run_checked(
        ytdlp_cmd("--no-playlist", "--no-warnings",
                  "--format", "bv*[height<=1080]+ba/b[height<=1080]/b",
                  "--merge-output-format", "mp4",
                  "--output", output_template, str(url)),
        runner,
        "Could not download the authorized YouTube video",
    )
    files = [
        path for path in destination.glob("source.*")
        if path.is_file() and not path.name.endswith((".part", ".ytdl"))
    ]
    if len(files) != 1 or files[0].stat().st_size == 0:
        raise RuntimeError("yt-dlp completed without producing exactly one non-empty video file.")
    return info, files[0]


def split_video(video_path, chunks_dir, *, chunk_seconds=90, runner=subprocess.run):
    """Split a finite video into transport-stream chunks for the existing clip analyser."""
    source = Path(video_path).resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError("Input video is missing or empty.")
    if int(chunk_seconds) < 1:
        raise ValueError("chunk_seconds must be positive.")

    output_dir = Path(chunks_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(output_dir / "chunk_%05d.ts")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
        "-c", "copy", "-f", "segment", "-segment_time", str(int(chunk_seconds)),
        "-reset_timestamps", "1", pattern,
    ]
    _run_checked(command, runner, "Could not split the downloaded video")
    chunks = sorted(
        path for path in output_dir.glob("chunk_*.ts")
        if path.is_file() and path.stat().st_size > 0
    )
    if not chunks:
        raise RuntimeError("FFmpeg produced no usable video chunks.")
    return chunks
