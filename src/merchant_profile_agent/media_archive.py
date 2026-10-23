from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import subprocess
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


class MediaArchiveError(RuntimeError):
    """Raised when a staged Feishu attachment cannot be archived safely."""


@dataclass(frozen=True)
class DownloadedMedia:
    data: bytes
    file_name: str
    media_type: str
    source_ref: str


def _safe_file_name(value: str, *, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKC", Path(value).name).strip()
    normalized = re.sub(r"[\x00-\x1f\x7f/:\\]+", "_", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    if not normalized:
        normalized = fallback
    suffix = Path(normalized).suffix[:12]
    stem = Path(normalized).stem[:180].strip(" .") or Path(fallback).stem
    return f"{stem}{suffix}"[:200]


def _walk(value: Any):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


class MediaStagingStore:
    def __init__(self, root: Path, *, ttl_seconds: int = 604_800) -> None:
        self.root = root.expanduser().resolve()
        self.ttl_seconds = ttl_seconds
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.prune()

    def stage(self, media: DownloadedMedia) -> dict[str, Any]:
        self.prune()
        if not media.data:
            raise MediaArchiveError("附件为空，无法归档。")
        digest = hashlib.sha256(media.data).hexdigest()
        suffix = Path(media.file_name).suffix
        if not suffix:
            suffix = mimetypes.guess_extension(media.media_type) or ".bin"
        safe_name = _safe_file_name(media.file_name, fallback=f"attachment{suffix}")
        target = self.root / f"{uuid.uuid4().hex}-{safe_name}"
        temporary = target.with_suffix(target.suffix + ".tmp")
        try:
            try:
                with temporary.open("xb") as handle:
                    handle.write(media.data)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.chmod(0o600)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise MediaArchiveError("服务器无法暂存附件，请稍后重试。") from exc
        return {
            "archive_id": "media_" + digest[:16],
            "local_path": str(target),
            "file_name": safe_name,
            "media_type": media.media_type,
            "size_bytes": len(media.data),
            "sha256": digest,
            "source_ref": media.source_ref,
        }

    def discard(self, items: Sequence[Mapping[str, Any]]) -> None:
        for item in items:
            raw_path = item.get("local_path")
            if not isinstance(raw_path, str) or not raw_path:
                continue
            try:
                path = Path(raw_path).expanduser().resolve()
                path.relative_to(self.root)
            except (OSError, ValueError):
                continue
            path.unlink(missing_ok=True)

    def prune(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        for path in self.root.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue


class FeishuDriveArchiver:
    """Insert staged media into its resolved merchant module document."""

    def __init__(
        self,
        *,
        executable: str,
        origin: str,
        timeout_seconds: int = 240,
    ) -> None:
        self.executable = executable
        self.origin = origin.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def archive(
        self, item: Mapping[str, Any], *, document_token: str
    ) -> dict[str, str]:
        path = Path(str(item.get("local_path") or "")).expanduser().resolve()
        if not path.is_file():
            raise MediaArchiveError("待保存附件已不存在，请重新发送原文件。")
        if not document_token:
            raise MediaArchiveError("未找到附件对应的知识库模块，附件尚未上传。")
        name = _safe_file_name(
            str(item.get("file_name") or path.name), fallback="attachment.bin"
        )
        media_type = str(item.get("media_type") or "application/octet-stream").lower()
        command = [
            self.executable,
            "docs",
            "+media-insert",
            "--doc",
            document_token,
            "--file",
            str(path),
            "--as",
            "user",
            "--format",
            "json",
        ]
        if media_type.startswith("image/"):
            command.extend(["--type", "image", "--width", "720", "--caption", name])
        else:
            view = "preview" if media_type.startswith(("video/", "audio/")) else "card"
            command.extend(["--type", "file", "--file-view", view])
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise MediaArchiveError("飞书附件上传服务暂不可用。") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise MediaArchiveError(f"飞书附件上传失败：{detail[:200]}")
        raw_output = result.stdout.strip()
        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError:
            payload = None
            for opener in ("{", "["):
                offset = raw_output.find(opener)
                if offset < 0:
                    continue
                try:
                    payload = json.loads(raw_output[offset:])
                    break
                except json.JSONDecodeError:
                    continue
            if payload is None:
                raise MediaArchiveError("飞书附件上传返回了无效结果。")
        file_token = ""
        file_url = ""
        block_id = ""
        for value in _walk(payload):
            if not isinstance(value, Mapping):
                continue
            candidate = value.get("file_token")
            if isinstance(candidate, str) and candidate:
                file_token = candidate
            candidate_url = value.get("url") or value.get("file_url")
            if isinstance(candidate_url, str) and candidate_url.startswith("https://"):
                file_url = candidate_url
            candidate_block = value.get("block_id")
            if isinstance(candidate_block, str) and candidate_block:
                block_id = candidate_block
            if file_token and file_url:
                break
        if not file_token:
            raise MediaArchiveError("飞书附件上传成功但未返回文件标识。")
        if not file_url:
            file_url = f"{self.origin}/file/{file_token}"
        return {
            "file_token": file_token,
            "url": file_url,
            "name": name,
            "block_id": block_id,
        }
