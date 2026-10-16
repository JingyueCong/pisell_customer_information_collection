from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from merchant_profile_agent.video import (
    OPENAI_TRANSCRIPT_MAX_BYTES,
    OpenAIAudioTranscriber,
    VideoProcessingError,
)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class OpenAIAudioTranscriberTests(unittest.TestCase):
    @patch("merchant_profile_agent.video.urllib.request.urlopen")
    def test_sends_audio_as_multipart_and_returns_text(self, urlopen) -> None:
        urlopen.return_value = FakeResponse({"text": "店长是 Lily。"})
        client = OpenAIAudioTranscriber("test-key", model="gpt-transcribe")

        result = client.transcribe(b"fake-mp3", file_name="customer.mp3")

        self.assertEqual(result, "店长是 Lily。")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, client.endpoint)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertIn(b'gpt-transcribe', request.data)
        self.assertIn(b'filename="customer.mp3"', request.data)
        self.assertIn(b"fake-mp3", request.data)

    def test_rejects_audio_larger_than_openai_limit(self) -> None:
        client = OpenAIAudioTranscriber("test-key")

        with self.assertRaisesRegex(VideoProcessingError, "25 MB"):
            client.transcribe(b"x" * (OPENAI_TRANSCRIPT_MAX_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
