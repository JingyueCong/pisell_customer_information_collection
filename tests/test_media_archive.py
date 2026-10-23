from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from merchant_profile_agent.media_archive import FeishuDriveArchiver


class FeishuDriveArchiverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archiver = FeishuDriveArchiver(
            executable="lark-cli", origin="https://example.feishu.cn"
        )

    def _item(self, name: str, media_type: str) -> dict[str, object]:
        path = self.root / name
        path.write_bytes(b"test-media")
        return {
            "local_path": str(path),
            "file_name": name,
            "media_type": media_type,
        }

    @staticmethod
    def _completed(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "document_id": "doc_target",
                    "block_id": "block_media",
                    "file_token": "file_media",
                    "file_name": "media",
                }
            ),
            "",
        )

    def test_image_is_inserted_as_visible_document_image(self) -> None:
        item = self._item("menu photo.png", "image/png")
        with patch(
            "merchant_profile_agent.media_archive.subprocess.run",
            side_effect=lambda command, **_: self._completed(command),
        ) as run:
            result = self.archiver.archive(item, document_token="doc_target")

        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["lark-cli", "docs", "+media-insert"])
        self.assertEqual(command[command.index("--doc") + 1], "doc_target")
        self.assertEqual(command[command.index("--type") + 1], "image")
        self.assertEqual(command[command.index("--width") + 1], "720")
        self.assertEqual(result["file_token"], "file_media")
        self.assertEqual(result["block_id"], "block_media")

    def test_video_is_inserted_as_inline_preview(self) -> None:
        item = self._item("walkthrough.mp4", "video/mp4")
        with patch(
            "merchant_profile_agent.media_archive.subprocess.run",
            side_effect=lambda command, **_: self._completed(command),
        ) as run:
            self.archiver.archive(item, document_token="doc_target")

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--type") + 1], "file")
        self.assertEqual(command[command.index("--file-view") + 1], "preview")


if __name__ == "__main__":
    unittest.main()
