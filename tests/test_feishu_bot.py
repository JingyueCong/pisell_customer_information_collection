from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field

from merchant_profile_agent.feishu_bot import FeishuBotController, SessionStore


@dataclass
class Conversation:
    thread_id: str | None = None


@dataclass
class Message:
    body_text: str
    message_id: str = "om_test"
    chat_id: str = "oc_test"
    sender_id: str = "ou_test"
    conversation: Conversation = field(default_factory=Conversation)


class FakeChannel:
    def __init__(self) -> None:
        self.replies = []

    async def reply(self, message, content, opts=None):
        self.replies.append((message, content, opts))


class FakeApi:
    def __init__(self) -> None:
        self.calls = []

    def post(self, path, payload):
        self.calls.append((path, payload))
        if path == "/v1/profile/analyze":
            return {
                "merchant": {"name": "青禾便当", "id": None},
                "reply": "已识别联系人。",
                "profile_updates": [
                    {
                        "field_path": "contacts.primary.name",
                        "value": "林经理",
                        "source_ref": payload["messages"][0]["source_ref"],
                        "replace_confirmed": False,
                        "state": "explicit",
                        "document_type": "business-contacts",
                    }
                ],
                "conflicts": [],
                "save_readiness": "ready",
                "next_question": None,
            }
        if path == "/v1/profile/read":
            return {"profile": {"fields": {}, "event_count": 0}}
        if path == "/v1/profile/commit":
            return {
                "write_performed": True,
                "result": {
                    "store_status": "stored",
                    "wiki_documents_updated": 2,
                    "documents": [
                        {
                            "document_type": "merchant-overview",
                            "url": "https://example.feishu.cn/wiki/overview",
                        },
                        {
                            "document_type": "business-contacts",
                            "url": "https://example.feishu.cn/wiki/contacts",
                        },
                    ],
                },
            }
        raise AssertionError(path)


class FeishuBotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeApi()
        self.channel = FakeChannel()
        self.sessions = SessionStore()
        self.bot = FeishuBotController(
            api=self.api, channel=self.channel, sessions=self.sessions  # type: ignore[arg-type]
        )

    def run_message(self, text: str) -> None:
        asyncio.run(self.bot.on_message(Message(text)))

    def test_normal_message_only_previews(self) -> None:
        self.run_message("青禾便当的联系人是林经理")

        self.assertEqual(
            [path for path, _ in self.api.calls],
            ["/v1/profile/analyze", "/v1/profile/read"],
        )
        reply = self.channel.replies[-1][1]["text"]
        self.assertIn("尚未写入知识库", reply)
        self.assertNotIn("已识别联系人", reply)

    def test_exact_save_command_commits_latest_preview(self) -> None:
        self.run_message("青禾便当的联系人是林经理")
        self.run_message("结束并保存")

        path, payload = self.api.calls[-1]
        self.assertEqual(path, "/v1/profile/commit")
        self.assertEqual(payload["confirmation"], "结束并保存")
        self.assertEqual(payload["merchant"]["name"], "青禾便当")
        self.assertIn("已写入知识库", self.channel.replies[-1][1]["text"])
        self.assertIn("https://example.feishu.cn/wiki/contacts", self.channel.replies[-1][1]["text"])

    def test_natural_save_confirmation_and_follow_up_links(self) -> None:
        self.run_message("青禾便当的联系人是林经理")
        self.run_message("没问题，保存吧")

        path, payload = self.api.calls[-1]
        self.assertEqual(path, "/v1/profile/commit")
        self.assertEqual(payload["confirmation"], "结束并保存")

        call_count = len(self.api.calls)
        self.run_message("刚才写入的两个页面链接")
        self.assertEqual(len(self.api.calls), call_count)
        reply = self.channel.replies[-1][1]["text"]
        self.assertIn("商户概况", reply)
        self.assertIn("https://example.feishu.cn/wiki/overview", reply)

    def test_merchant_sentence_containing_save_is_not_confirmation(self) -> None:
        self.run_message("青禾便当的资料保存周期是每周一次")

        self.assertEqual(self.api.calls[0][0], "/v1/profile/analyze")
        self.assertNotIn("/v1/profile/commit", [path for path, _ in self.api.calls])

    def test_device_connection_sentence_is_not_link_request(self) -> None:
        self.run_message("青禾便当的设备连接方式为 USB")

        self.assertEqual(self.api.calls[0][0], "/v1/profile/analyze")

    def test_discard_never_commits(self) -> None:
        self.run_message("青禾便当的联系人是林经理")
        self.run_message("/discard")

        self.assertNotIn("/v1/profile/commit", [path for path, _ in self.api.calls])
        self.assertIn("未写入知识库", self.channel.replies[-1][1]["text"])


if __name__ == "__main__":
    unittest.main()
