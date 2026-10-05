from __future__ import annotations

import asyncio
import copy
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
    sender_name: str = ""
    chat_type: str = "p2p"
    mentioned_bot: bool = False
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
                "summary_points": ["已确认主要联系人"],
                "decisions": ["由林经理负责后续沟通"],
                "action_items": ["发送开店资料"],
                "information_gaps": [],
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


class MissingMerchantThenCorrectionApi(FakeApi):
    def post(self, path, payload):
        self.calls.append((path, copy.deepcopy(payload)))
        if path == "/v1/profile/analyze":
            content = "\n".join(item["content"] for item in payload["messages"])
            has_merchant = "Dandong" in content
            return {
                "merchant": {"name": "Dandong咖啡厅" if has_merchant else None, "id": None},
                "reply": "已补充商户名称。" if has_merchant else "请补充商户名称。",
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
                "save_readiness": "ready" if has_merchant else "missing_info",
                "next_question": None if has_merchant else "商户名称是什么？",
                "summary_points": ["主要联系人是林经理"],
                "decisions": [],
                "action_items": [],
                "information_gaps": [] if has_merchant else ["商户名称"],
            }
        if path == "/v1/profile/read":
            return {"profile": {"fields": {}, "event_count": 0}}
        if path == "/v1/profile/commit":
            return {
                "write_performed": True,
                "result": {"store_status": "stored", "documents": []},
            }
        raise AssertionError(path)


class FeishuBotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeApi()
        self.channel = FakeChannel()
        self.sessions = SessionStore()
        self.bot = FeishuBotController(
            api=self.api,
            channel=self.channel,
            sessions=self.sessions,  # type: ignore[arg-type]
            group_message_threshold=2,
            group_idle_seconds=600,
            group_idle_min_messages=2,
        )

    def run_message(self, text: str) -> None:
        asyncio.run(self.bot.on_message(Message(text)))

    def run_group(
        self,
        text: str,
        *,
        mentioned: bool = False,
        sender_id: str = "ou_test",
        message_id: str = "om_group",
    ) -> None:
        asyncio.run(
            self.bot.on_message(
                Message(
                    text,
                    message_id=message_id,
                    chat_id="oc_group",
                    sender_id=sender_id,
                    sender_name="测试成员",
                    chat_type="group",
                    mentioned_bot=mentioned,
                )
            )
        )

    def test_normal_message_only_previews(self) -> None:
        self.run_message("青禾便当的联系人是林经理")

        self.assertEqual(
            [path for path, _ in self.api.calls],
            ["/v1/profile/analyze", "/v1/profile/read"],
        )
        reply = self.channel.replies[-1][1]["text"]
        self.assertIn("主要联系人：林经理", reply)
        self.assertNotIn("contacts.primary.name", reply)
        self.assertIn("保存吧", reply)
        self.assertNotIn("已识别联系人", reply)

    def test_exact_save_command_commits_latest_preview(self) -> None:
        self.run_message("青禾便当的联系人是林经理")
        self.run_message("结束并保存")

        path, payload = self.api.calls[-1]
        self.assertEqual(path, "/v1/profile/commit")
        self.assertEqual(payload["confirmation"], "结束并保存")
        self.assertEqual(payload["merchant"]["name"], "青禾便当")
        self.assertIn("已写入：", self.channel.replies[-1][1]["text"])
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

    def test_group_messages_are_ignored_until_explicit_enable(self) -> None:
        self.run_group("青禾便当的联系人是林经理")

        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.channel.replies, [])

    def test_group_mentioned_message_before_enable_explains_how_to_start(self) -> None:
        self.run_group("保存吧", mentioned=True)

        self.assertEqual(self.api.calls, [])
        self.assertIn("开始记录", self.channel.replies[-1][1]["text"])

    def test_group_start_recording_alias_and_save_flush_pending_messages(self) -> None:
        self.bot = FeishuBotController(
            api=self.api,  # type: ignore[arg-type]
            channel=self.channel,
            sessions=self.sessions,
            group_message_threshold=20,
            group_idle_seconds=600,
            group_idle_min_messages=5,
        )
        self.run_group("开始记录", mentioned=True, message_id="om_start")
        self.run_group("青禾便当联系人是林经理", message_id="om_contact")
        self.run_group("电话是040304020402", message_id="om_phone")

        calls_before_save = len(self.api.calls)
        replies_before_save = len(self.channel.replies)
        self.run_group("保存吧", mentioned=True, message_id="om_save")

        paths = [path for path, _ in self.api.calls[calls_before_save:]]
        self.assertEqual(paths, ["/v1/profile/analyze", "/v1/profile/read", "/v1/profile/commit"])
        self.assertEqual(len(self.channel.replies), replies_before_save + 1)
        self.assertIn("已写入", self.channel.replies[-1][1]["text"])

    def test_group_semantic_start_intents_are_supported(self) -> None:
        examples = [
            "接下来请帮我收集群里的商户资料",
            "从现在开始帮忙整理客户信息",
            "我们可以开始了，帮我记一下",
            "麻烦启动群聊总结",
        ]
        for index, text in enumerate(examples):
            with self.subTest(text=text):
                channel = FakeChannel()
                bot = FeishuBotController(
                    api=self.api,  # type: ignore[arg-type]
                    channel=channel,
                    sessions=SessionStore(),
                )
                asyncio.run(
                    bot.on_message(
                        Message(
                            text,
                            chat_id=f"oc_semantic_{index}",
                            chat_type="group",
                            mentioned_bot=True,
                        )
                    )
                )
                self.assertIn("自动总结已开启", channel.replies[-1][1]["text"])

    def test_group_negative_or_incidental_recording_language_does_not_start(self) -> None:
        examples = ["不要开始记录", "请停止记录", "门店开始记录每日销售额"]
        for index, text in enumerate(examples):
            with self.subTest(text=text):
                channel = FakeChannel()
                bot = FeishuBotController(
                    api=self.api,  # type: ignore[arg-type]
                    channel=channel,
                    sessions=SessionStore(),
                )
                asyncio.run(
                    bot.on_message(
                        Message(
                            text,
                            chat_id=f"oc_negative_{index}",
                            chat_type="group",
                            mentioned_bot=True,
                        )
                    )
                )
                self.assertIn("尚未开始记录", channel.replies[-1][1]["text"])

    def test_group_summarizes_shared_chat_after_threshold(self) -> None:
        self.run_group("开启自动总结", mentioned=True)
        self.run_group(
            "青禾便当的联系人是林经理",
            sender_id="ou_one",
            message_id="om_one",
        )
        self.run_group(
            "后续由林经理负责沟通",
            sender_id="ou_two",
            message_id="om_two",
        )

        reply = self.channel.replies[-1][1]["text"]
        self.assertIn("阶段总结", reply)
        self.assertIn("已决定", reply)
        self.assertIn("可写入知识库", reply)
        self.assertIn("@机器人", reply)
        analyzed = next(payload for path, payload in self.api.calls if path == "/v1/profile/analyze")
        self.assertEqual(len(analyzed["messages"]), 2)

    def test_group_save_requires_explicit_bot_mention(self) -> None:
        self.run_group("开启自动总结", mentioned=True)
        self.run_group("青禾便当的联系人是林经理", message_id="om_one")
        self.run_group("后续由林经理负责沟通", message_id="om_two")

        calls_before = len(self.api.calls)
        self.run_group("保存吧", mentioned=False, message_id="om_three")
        self.assertNotIn(
            "/v1/profile/commit",
            [path for path, _ in self.api.calls[calls_before:]],
        )

        self.run_group("保存吧", mentioned=True, message_id="om_four")
        self.assertEqual(self.api.calls[-1][0], "/v1/profile/commit")
        self.assertIn("已写入", self.channel.replies[-1][1]["text"])

    def test_group_missing_merchant_followup_is_reanalyzed_immediately(self) -> None:
        api = MissingMerchantThenCorrectionApi()
        bot = FeishuBotController(
            api=api,  # type: ignore[arg-type]
            channel=self.channel,
            sessions=SessionStore(),
            group_message_threshold=1,
            group_idle_seconds=600,
            group_idle_min_messages=1,
        )

        async def scenario() -> None:
            base = {
                "chat_id": "oc_correction",
                "chat_type": "group",
                "sender_name": "测试成员",
            }
            await bot.on_message(Message("开启自动总结", mentioned_bot=True, **base))
            await bot.on_message(Message("主要联系人是林经理", message_id="om_detail", **base))
            await bot.on_message(
                Message("保存吧", message_id="om_save_one", mentioned_bot=True, **base)
            )
            await bot.on_message(
                Message("商户名 Dandong咖啡厅", message_id="om_name_one", **base)
            )
            replies_before_mention = len(self.channel.replies)
            await bot.on_message(
                Message(
                    "Dandong咖啡厅",
                    message_id="om_name_two",
                    mentioned_bot=True,
                    **base,
                )
            )
            self.assertEqual(len(self.channel.replies), replies_before_mention + 1)
            self.assertIn("可写入知识库", self.channel.replies[-1][1]["text"])
            await bot.on_message(
                Message("保存吧", message_id="om_save_two", mentioned_bot=True, **base)
            )

        asyncio.run(scenario())

        analyzes = [payload for path, payload in api.calls if path == "/v1/profile/analyze"]
        corrected_messages = analyzes[-1]["messages"]
        self.assertTrue(
            any("主要联系人是林经理" in item["content"] for item in corrected_messages)
        )
        self.assertTrue(any("Dandong咖啡厅" in item["content"] for item in corrected_messages))
        commit = next(payload for path, payload in api.calls if path == "/v1/profile/commit")
        self.assertEqual(commit["merchant"]["name"], "Dandong咖啡厅")

    def test_group_mentioned_sentence_is_not_save_confirmation(self) -> None:
        self.run_group("开启自动总结", mentioned=True)
        self.run_group("青禾便当的联系人是林经理", message_id="om_one")
        self.run_group("后续由林经理负责沟通", message_id="om_two")

        call_count = len(self.api.calls)
        self.run_group("这个资料以前已经保存", mentioned=True, message_id="om_status")

        self.assertNotIn(
            "/v1/profile/commit",
            [path for path, _ in self.api.calls[call_count:]],
        )

    def test_group_manual_summary_and_pause(self) -> None:
        self.run_group("开启群总结", mentioned=True)
        self.run_group("青禾便当的联系人是林经理", message_id="om_one")
        self.run_group("立即总结", mentioned=True, message_id="om_now")
        self.assertIn("阶段总结", self.channel.replies[-1][1]["text"])

        call_count = len(self.api.calls)
        self.run_group("暂停自动总结", mentioned=True, message_id="om_pause")
        self.run_group("这条消息不应进入模型", message_id="om_after")
        self.assertEqual(len(self.api.calls), call_count)

    def test_group_idle_summary_after_minimum_messages(self) -> None:
        async def scenario() -> None:
            bot = FeishuBotController(
                api=self.api,  # type: ignore[arg-type]
                channel=self.channel,
                sessions=SessionStore(),
                group_message_threshold=20,
                group_idle_seconds=0.01,  # type: ignore[arg-type]
                group_idle_min_messages=2,
            )
            base = {
                "chat_id": "oc_idle",
                "chat_type": "group",
                "sender_name": "测试成员",
            }
            await bot.on_message(
                Message("开启自动总结", mentioned_bot=True, **base)
            )
            await bot.on_message(Message("青禾便当联系人是林经理", **base))
            await bot.on_message(Message("电话稍后补充", **base))
            await asyncio.sleep(0.03)

        asyncio.run(scenario())

        self.assertIn("阶段总结", self.channel.replies[-1][1]["text"])


if __name__ == "__main__":
    unittest.main()
