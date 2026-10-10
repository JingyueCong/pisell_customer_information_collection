from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from merchant_profile_agent.ticket_client import TicketAgentClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class TicketAgentClientTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_posts_authenticated_customer_service_handoff(self, urlopen) -> None:
        urlopen.return_value = FakeResponse(
            {
                "ok": True,
                "reply": "已创建客服工单：https://example.invalid/123",
                "draft_open": False,
                "work_item_ids": ["123"],
                "cached": False,
            }
        )
        client = TicketAgentClient(
            base_url="http://127.0.0.1:8787",
            token="test-token-at-least-24-characters",
            timeout=45,
        )

        result = client.create_customer_service_ticket(
            {
                "request_id": "om_1",
                "conversation_id": "oc_1:ou_1",
                "source_chat_id": "oc_1",
                "source_message_id": "om_1",
                "content": "无法打印小票",
                "context": [],
            }
        )

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8787/internal/customer-service-tickets",
        )
        self.assertEqual(request.headers["Authorization"], "Bearer test-token-at-least-24-characters")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 45)
        self.assertEqual(result["work_item_ids"], ["123"])


if __name__ == "__main__":
    unittest.main()
