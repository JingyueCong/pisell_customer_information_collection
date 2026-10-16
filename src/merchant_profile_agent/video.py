from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path


LOGGER = logging.getLogger("merchant_profile_agent.video")
OPENAI_TRANSCRIPT_MAX_BYTES = 25 * 1024 * 1024
SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}


class VideoProcessingError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoEvidence:
    transcript: str
    frames: list[str]


class OpenAIAudioTranscriber:
    endpoint = "https://api.openai.com/v1/audio/transcriptions"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-transcribe",
        timeout: int = 180,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def transcribe(self, data: bytes, *, file_name: str = "video-audio.mp3") -> str:
        if not data:
            return ""
        if len(data) > OPENAI_TRANSCRIPT_MAX_BYTES:
            raise VideoProcessingError("视频音频超过转写服务的 25 MB 限制。")
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(file_name).name) or "audio.mp3"
        boundary = "----pisell-" + uuid.uuid4().hex
        body = b"".join(
            [
                self._field(boundary, "model", self.model),
                self._file(boundary, "file", safe_name, "audio/mpeg", data),
                f"--{boundary}--\r\n".encode("ascii"),
            ]
        )
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise VideoProcessingError(
                f"视频语音转写服务返回 HTTP {exc.code}。"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise VideoProcessingError("视频语音转写服务暂不可用。") from exc
        try:
            result = json.loads(raw)
            text = result.get("text", "")
        except (json.JSONDecodeError, AttributeError) as exc:
            raise VideoProcessingError("视频语音转写结果无效。") from exc
        return str(text).strip()

    @staticmethod
    def _field(boundary: str, name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")

    @staticmethod
    def _file(
        boundary: str,
        name: str,
        file_name: str,
        content_type: str,
        data: bytes,
    ) -> bytes:
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{file_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        return header + data + b"\r\n"


class OpenAIVideoProcessor:
    def __init__(
        self,
        transcriber: OpenAIAudioTranscriber,
        *,
        maximum_bytes: int = 25 * 1024 * 1024,
        maximum_duration_seconds: int = 300,
        maximum_frames: int = 4,
        maximum_frame_bytes: int = 6 * 1024 * 1024,
        transcript_character_limit: int = 6_000,
    ) -> None:
        self.transcriber = transcriber
        self.maximum_bytes = maximum_bytes
        self.maximum_duration_seconds = maximum_duration_seconds
        self.maximum_frames = maximum_frames
        self.maximum_frame_bytes = maximum_frame_bytes
        self.transcript_character_limit = transcript_character_limit

    def process(
        self,
        data: bytes,
        *,
        file_name: str,
        duration_ms: int | None = None,
    ) -> VideoEvidence:
        if not data:
            raise VideoProcessingError("视频文件为空。")
        if len(data) > self.maximum_bytes:
            raise VideoProcessingError(
                f"视频不能超过 {self.maximum_bytes // (1024 * 1024)} MB。"
            )
        if duration_ms and duration_ms > self.maximum_duration_seconds * 1_000:
            raise VideoProcessingError(
                f"视频不能超过 {self.maximum_duration_seconds // 60} 分钟。"
            )
        try:
            import imageio_ffmpeg
        except ImportError as exc:
            raise VideoProcessingError("服务器尚未安装视频处理组件。") from exc

        suffix = Path(file_name).suffix.lower()
        if suffix not in SUPPORTED_VIDEO_SUFFIXES:
            suffix = ".mp4"
        try:
            with tempfile.TemporaryDirectory(prefix="pisell-video-") as directory:
                workdir = Path(directory)
                source = workdir / ("source" + suffix)
                source.write_bytes(data)
                executable = imageio_ffmpeg.get_ffmpeg_exe()
                seconds = duration_ms / 1_000 if duration_ms else self._probe_duration(
                    executable, source
                )
                if seconds and seconds > self.maximum_duration_seconds:
                    raise VideoProcessingError(
                        f"视频不能超过 {self.maximum_duration_seconds // 60} 分钟。"
                    )
                frames = self._extract_frames(executable, source, workdir, seconds)
                transcript = self._extract_and_transcribe_audio(
                    executable, source, workdir
                )
        except subprocess.TimeoutExpired as exc:
            raise VideoProcessingError("视频处理超时，请发送更短的视频。") from exc
        except OSError as exc:
            raise VideoProcessingError("服务器无法处理这个视频文件。") from exc

        transcript = transcript[: self.transcript_character_limit].strip()
        if not transcript and not frames:
            raise VideoProcessingError("没有从视频中识别到语音或可用画面。")
        return VideoEvidence(transcript=transcript, frames=frames)

    @staticmethod
    def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=90,
        )

    def _probe_duration(self, executable: str, source: Path) -> float:
        result = self._run([executable, "-hide_banner", "-i", str(source)])
        stderr = result.stderr.decode("utf-8", errors="replace")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
        if not match:
            return 0.0
        hours, minutes, seconds = match.groups()
        return int(hours) * 3_600 + int(minutes) * 60 + float(seconds)

    def _extract_frames(
        self,
        executable: str,
        source: Path,
        workdir: Path,
        seconds: float,
    ) -> list[str]:
        usable_seconds = max(seconds, 1.0)
        positions = [usable_seconds * fraction for fraction in (0.1, 0.35, 0.6, 0.85)]
        frames: list[str] = []
        total_bytes = 0
        for index, position in enumerate(positions[: self.maximum_frames]):
            output = workdir / f"frame-{index}.jpg"
            result = self._run(
                [
                    executable,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{position:.3f}",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=1280:-2:force_original_aspect_ratio=decrease",
                    "-q:v",
                    "5",
                    "-y",
                    str(output),
                ]
            )
            if result.returncode != 0 or not output.exists():
                continue
            frame = output.read_bytes()
            if not frame or total_bytes + len(frame) > self.maximum_frame_bytes:
                continue
            total_bytes += len(frame)
            frames.append("data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii"))
        return frames

    def _extract_and_transcribe_audio(
        self,
        executable: str,
        source: Path,
        workdir: Path,
    ) -> str:
        audio = workdir / "audio.mp3"
        result = self._run(
            [
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                "48k",
                "-y",
                str(audio),
            ]
        )
        if result.returncode != 0 or not audio.exists() or not audio.stat().st_size:
            return ""
        try:
            return self.transcriber.transcribe(audio.read_bytes(), file_name=audio.name)
        except VideoProcessingError:
            if not any(workdir.glob("frame-*.jpg")):
                raise
            LOGGER.warning("video audio transcription failed; continuing with key frames")
            return ""
