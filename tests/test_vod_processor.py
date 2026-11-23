import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from vod_processor import download_youtube_video, split_video


class VodProcessorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def metadata(self, **updates):
        value = {
            "id": "video123",
            "channel_id": "UC-authorized",
            "title": "GTA gameplay",
            "duration": 600,
            "is_live": False,
            "live_status": "not_live",
        }
        value.update(updates)
        return value

    def test_downloads_one_authorized_regular_video(self):
        info = self.metadata()
        def runner(command, **kwargs):
            if "--dump-single-json" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps(info), "")
            output_template = command[command.index("--output") + 1]
            output_path = Path(output_template.replace("%(ext)s", "mp4"))
            output_path.write_bytes(b"video")
            return subprocess.CompletedProcess(command, 0, "", "")

        result, path = download_youtube_video(
            "https://www.youtube.com/watch?v=video123",
            self.root / "download",
            expected_video_id="video123",
            expected_channel_id="UC-authorized",
            runner=runner,
        )
        self.assertEqual(result["id"], "video123")
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), b"video")

    def test_download_command_does_not_use_cookies_or_browser_profiles(self):
        info = self.metadata()
        commands = []
        def runner(command, **kwargs):
            commands.append(command)
            if "--dump-single-json" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps(info), "")
            Path(command[command.index("--output") + 1].replace("%(ext)s", "mp4")).write_bytes(b"video")
            return subprocess.CompletedProcess(command, 0, "", "")

        download_youtube_video(
            "https://youtu.be/video123", self.root / "download",
            expected_video_id="video123", expected_channel_id="UC-authorized",
            runner=runner,
        )
        joined = " ".join(" ".join(command) for command in commands).lower()
        self.assertNotIn("--cookies", joined)
        self.assertNotIn("--cookies-from-browser", joined)


    def test_retries_youtube_bot_challenge_with_safari_client(self):
        info = self.metadata()
        commands = []

        def runner(command, **kwargs):
            commands.append(command)
            if "--extractor-args" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps(info), "")
            return subprocess.CompletedProcess(
                command, 1, "", "Sign in to confirm you're not a bot"
            )

        result, _ = download_youtube_video(
            "https://youtu.be/video123", self.root / "download",
            expected_video_id="video123", expected_channel_id="UC-authorized",
            runner=runner,
        )
        self.assertEqual(result["id"], "video123")
        self.assertEqual(len(commands), 2)
        self.assertIn("youtube:player_client=web_safari", commands[1])

    def test_rejects_livestream(self):
        info = self.metadata(is_live=True)
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(info), ""))
        with self.assertRaisesRegex(ValueError, "livestream"):
            download_youtube_video(
                "https://www.youtube.com/watch?v=video123", self.root / "download",
                runner=runner,
            )
        runner.assert_called_once()

    def test_rejects_wrong_channel_before_download(self):
        info = self.metadata(channel_id="UC-other")
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(info), ""))
        with self.assertRaisesRegex(PermissionError, "channel"):
            download_youtube_video(
                "https://www.youtube.com/watch?v=video123", self.root / "download",
                expected_channel_id="UC-authorized", runner=runner,
            )
        runner.assert_called_once()

    def test_rejects_video_over_duration_limit(self):
        info = self.metadata(duration=7200)
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(info), ""))
        with self.assertRaisesRegex(ValueError, "configured limit"):
            download_youtube_video(
                "https://www.youtube.com/watch?v=video123", self.root / "download",
                max_duration_seconds=3600, runner=runner,
            )
        runner.assert_called_once()

    def test_split_video_returns_nonempty_chunks(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"video")
        def runner(command, **kwargs):
            output_dir = Path(command[-1]).parent
            (output_dir / "chunk_00000.ts").write_bytes(b"chunk")
            return subprocess.CompletedProcess(command, 0, "", "")

        chunks = split_video(source, self.root / "chunks", chunk_seconds=30, runner=runner)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].read_bytes(), b"chunk")

    def test_split_video_fails_when_ffmpeg_produces_no_chunks(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"video")
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        with self.assertRaisesRegex(RuntimeError, "no usable video chunks"):
            split_video(source, self.root / "chunks", runner=runner)


if __name__ == "__main__":
    unittest.main()
