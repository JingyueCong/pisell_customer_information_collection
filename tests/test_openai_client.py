from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from merchant_profile_agent.openai_client import OpenAIResponsesClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class OpenAIClientTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_uses_responses_structured_outputs_without_storage(self, urlopen) -> None:
        urlopen.return_value = FakeResponse(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": '{"answer":"ok"}'}
                        ],
                    }
                ]
            }
        )
        client = OpenAIResponsesClient("test-key", "gpt-6-luna", timeout=12)

        result = client.create_structured(
            system_prompt="system",
            user_input="input",
            schema={"type": "object"},
        )

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(result, {"answer": "ok"})
        self.assertEqual(payload["model"], "gpt-6-luna")
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 12)

    @patch("urllib.request.urlopen")
    def test_sends_screenshot_as_high_detail_image_input(self, urlopen) -> None:
        urlopen.return_value = FakeResponse({"output_text": '{"answer":"ok"}'})
        client = OpenAIResponsesClient("test-key", "gpt-6-luna")

        client.create_structured(
            system_prompt="system",
            user_input='{"image_evidence":[{"image_ordinal":0,"source_message_index":0}]}',
            image_data_urls=["data:image/png;base64,iVBORw0KGgo="],
            schema={"type": "object"},
        )

        payload = json.loads(urlopen.call_args.args[0].data)
        content = payload["input"][1]["content"]
        self.assertEqual(content[0]["type"], "input_text")
        self.assertEqual(content[1]["type"], "input_image")
        self.assertEqual(content[1]["detail"], "high")
        self.assertTrue(content[1]["image_url"].startswith("data:image/png;base64,"))


if __name__ == "__main__":
    unittest.main()
