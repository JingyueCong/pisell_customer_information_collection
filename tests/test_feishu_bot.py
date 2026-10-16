from __future__ import annotations

import asyncio
import base64
import copy
import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from merchant_profile_agent.feishu_bot import (
    FeishuBotController,
    SessionStore,
    _group_summary_text,
    _is_group_profile_candidate,
    _messages_for_analysis,
    _without_video_placeholders,
)
from merchant_profile_agent.video import VideoEvidence


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
    resources: list = field(default_factory=list)
    content: object | None = None
    raw: dict = field(default_factory=dict)
    conversation: Conversation = field(default_factory=Conversation)


class FakeChannel:
    def __init__(self) -> None:
        self.replies = []
        self.downloads = []

    async def reply(self, message, content, opts=None):
        self.replies.append((message, content, opts))

    async def download_resource(self, file_key, resource_type="image", message_id=None):
        self.downloads.append((file_key, resource_type, message_id))
        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )


class FakeVideoProcessor:
    def __init__(self) -> None:
        self.calls = []

    def process(self, data, *, file_name, duration_ms=None):
        self.calls.append((data, file_name, duration_ms))
        frame = (
            "data:image/jpeg;base64,"
            + base64.b64encode(b"\xff\xd8\xfftest-frame").decode("ascii")
        )
        return VideoEvidence(
            transcript="汪汪咖啡店店长是 Lily，电话 0451100323。",
            frames=[frame],
        )


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


class PronounContinuationApi(FakeApi):
    def post(self, path, payload):
        self.calls.append((path, copy.deepcopy(payload)))
        if path == "/v1/profile/analyze":
            content = "\n".join(item["content"] for item in payload["messages"])
            first_segment = "联系人" in content
            return {
                "merchant": {
                    "name": "汪汪咖啡店" if first_segment else None,
                    "id": None,
                },
                "reply": "已识别。",
                "profile_updates": [
                    {
                        "field_path": (
                            "contacts.primary.name" if first_segment else "business.sales_team"
                        ),
                        "value": "李经理" if first_segment else "有 POS 机业务员，规模不小",
                        "source_ref": payload["messages"][0]["source_ref"],
                        "replace_confirmed": False,
                        "state": "explicit",
                        "document_type": (
                            "business-contacts" if first_segment else "merchant-overview"
                        ),
                    }
                ],
                "conflicts": [],
                "save_readiness": "ready",
                "next_question": None,
                "summary_points": [
                    "联系人是李经理"
                    if first_segment
                    else "他们有 POS 机业务员，规模不小"
                ],
                "decisions": [],
                "action_items": [],
                "information_gaps": [],
            }
        if path == "/v1/profile/read":
            return {"profile": {"fields": {}, "event_count": 0}}
        if path == "/v1/profile/commit":
            return {
                "write_performed": True,
                "result": {"store_status": "stored", "documents": []},
            }
        raise AssertionError(path)


class ConflictThenConfirmationApi(FakeApi):
    def post(self, path, payload):
        self.calls.append((path, copy.deepcopy(payload)))
        if path == "/v1/profile/analyze":
            content = "\n".join(item["content"] for item in payload["messages"])
            confirmed = "确认替换" in content
            update = {
                "field_path": "contacts.phone",
                "value": "0451100323",
                "source_ref": payload["messages"][0]["source_ref"],
                "replace_confirmed": confirmed,
                "state": "explicit",
                "document_type": "business-contacts",
            }
            return {
                "merchant": {"name": "汪汪咖啡店", "id": None},
                "reply": "已确认替换。" if confirmed else "发现电话不同。",
                "profile_updates": [update] if confirmed else [],
                "conflicts": []
                if confirmed
                else [
                    {
                        "field_path": "contacts.phone",
                        "current_value": "045123455555",
                        "proposed_value": "0451100323",
                    }
                ],
                "save_readiness": "ready" if confirmed else "needs_confirmation",
                "next_question": None if confirmed else "是否替换旧电话？",
                "summary_points": ["店长 Lily，电话 0451100323"],
                "decisions": [],
                "action_items": [],
                "information_gaps": [],
            }
        if path == "/v1/profile/read":
            return {
                "profile": {
                    "fields": {"contacts.phone": {"value": "045123455555"}},
                    "event_count": 1,
                }
            }
        if path == "/v1/profile/commit":
            return {
                "write_performed": True,
                "result": {"store_status": "stored", "documents": []},
            }
        raise AssertionError(path)


class CrmAwareApi(FakeApi):
    def __init__(self, status: str) -> None:
        super().__init__()
        self.status = status

    def post(self, path, payload):
        if path != "/v1/profile/read":
            return super().post(path, payload)
        self.calls.append((path, copy.deepcopy(payload)))
        candidates = []
        if self.status == "unique":
            candidates = [
                {
                    "work_item_id": "987654321",
                    "name": "青禾便当",
                    "url": "https://project.feishu.cn/v2qint/crm/detail/987654321",
                }
            ]
        elif self.status == "ambiguous":
            candidates = [
                {"work_item_id": "101", "name": "1001 青禾便当"},
                {"work_item_id": "102", "name": "1002 青禾便当"},
            ]
        return {
            "profile": {"fields": {}, "event_count": 0},
            "crm": {
                "status": self.status,
                "candidates": candidates,
                "read_only": True,
            },
        }


class RefreshingCrmApi(FakeApi):
    def __init__(self) -> None:
        super().__init__()
        self.read_count = 0

    def post(self, path, payload):
        if path != "/v1/profile/read":
            return super().post(path, payload)
        self.calls.append((path, copy.deepcopy(payload)))
        self.read_count += 1
        unique = self.read_count >= 2
        return {
            "profile": {"fields": {}, "event_count": 0},
            "crm": {
                "status": "unique" if unique else "not_found",
                "candidates": (
                    [
                        {
                            "work_item_id": "6856334446",
                            "name": "9262 U Bowl Melbourne Central （海底捞）",
                        }
                    ]
                    if unique
                    else []
                ),
                "read_only": True,
            },
        }


class EmptyThenUpdateApi(FakeApi):
    def __init__(self) -> None:
        super().__init__()
        self.analyze_count = 0

    def post(self, path, payload):
        if path != "/v1/profile/analyze":
            return super().post(path, payload)
        self.analyze_count += 1
        if self.analyze_count > 1:
            result = super().post(path, payload)
            result["merchant"] = {"name": "U Bowl Malatang", "id": None}
            return result
        self.calls.append((path, copy.deepcopy(payload)))
        return {
            "merchant": {"name": "U Bowl Malatang", "id": None},
            "reply": "已总结界面测试。",
            "profile_updates": [],
            "conflicts": [],
            "save_readiness": "needs_input",
            "next_question": None,
            "summary_points": ["发现点餐屏图片缺失"],
            "decisions": [],
            "action_items": [],
            "information_gaps": [],
        }


class NamedMerchantApi(FakeApi):
    def post(self, path, payload):
        if path != "/v1/profile/analyze":
            return super().post(path, payload)
        self.calls.append((path, copy.deepcopy(payload)))
        content = "\n".join(item["content"] for item in payload["messages"])
        name = "Dandong咖啡厅" if "Dandong" in content else "青禾便当"
        return {
            "merchant": {"name": name, "id": None},
            "reply": "已识别。",
            "profile_updates": [
                {
                    "field_path": "stores.primary.address",
                    "value": "123 Collins Street" if "Dandong" in content else "1 青禾路",
                    "source_ref": payload["messages"][0]["source_ref"],
                    "replace_confirmed": False,
                    "state": "explicit",
                    "document_type": "merchant-overview",
                }
            ],
            "conflicts": [],
            "save_readiness": "ready",
            "next_question": None,
            "summary_points": ["已识别地址"],
            "decisions": [],
            "action_items": [],
            "information_gaps": [],
        }


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

    def test_analysis_image_budget_is_shared_across_group_messages(self) -> None:
        png = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        messages = [
            {
                "role": "user",
                "content": f"视频 {index}",
                "source_ref": f"feishu://message/{index}",
                "images": [png, png, png, png],
            }
            for index in range(3)
        ]

        bounded = _messages_for_analysis(messages)

        self.assertEqual(sum(len(item.get("images", [])) for item in bounded), 4)
        self.assertEqual(len(bounded[2]["images"]), 2)
        self.assertEqual(len(bounded[1]["images"]), 1)
        self.assertEqual(len(bounded[0]["images"]), 1)
        self.assertEqual(len(messages[0]["images"]), 4)

    def test_html_escaped_video_placeholders_are_removed(self) -> None:
        text = (
            '成员：&lt;video key=&quot;file_test&quot; name=&quot;demo.mp4&quot;/&gt;\n'
            "视频语音转写：图片缺失"
        )

        cleaned = _without_video_placeholders(text)

        self.assertNotIn("file_test", cleaned)
        self.assertNotIn("&lt;video", cleaned)
        self.assertIn("图片缺失", cleaned)

    def test_group_summary_explains_crm_result_and_specific_next_step(self) -> None:
        text = _group_summary_text(
            {
                "merchant": {"name": "U Bowl Malatang"},
                "summary_points": ["识别到点餐屏资料"],
                "information_gaps": ["CRM 客户唯一匹配"],
                "next_question": "CRM 中未找到“U Bowl Malatang”，请补充店铺 ID。",
                "profile_updates": [],
                "conflicts": [],
                "save_readiness": "missing_info",
            }
        )

        self.assertIn("CRM 中未找到", text)
        self.assertIn("请补充店铺 ID", text)

    def test_group_summary_uses_business_sections_and_chinese_labels(self) -> None:
        text = _group_summary_text(
            {
                "merchant": {"name": "9262 U Bowl Melbourne Central （海底捞）"},
                "summary_points": ["点餐系统测试完成"],
                "profile_updates": [
                    {
                        "field_path": "system.customer_flow.member_login",
                        "value": "使用手机号验证，也可跳过继续点餐。",
                    },
                    {
                        "field_path": "configuration.menu.item_images",
                        "value": "部分菜单项图片缺失。",
                    },
                ],
                "conflicts": [],
                "save_readiness": "ready",
            }
        )

        self.assertIn("当前点餐流程", text)
        self.assertIn("会员登录：使用手机号验证，也可跳过继续点餐。", text)
        self.assertIn("已发现的问题", text)
        self.assertIn("菜单图片：部分菜单项图片缺失。", text)
        self.assertNotIn("阶段总结", text)
        self.assertNotIn("可写入知识库", text)
        self.assertNotIn("member login", text)

    def test_image_message_is_downloaded_analyzed_and_not_persisted_as_base64(self) -> None:
        message = Message(
            "<image key=wrong_resource_key />",
            message_id="om_image",
            content={"kind": "image", "image_key": "img_real_body_key"},
            resources=[{"type": "image", "file_key": "wrong_resource_key"}],
        )

        asyncio.run(self.bot.on_message(message))

        self.assertEqual(
            self.channel.downloads,
            [("img_real_body_key", "image", "om_image")],
        )
        analyze = next(payload for path, payload in self.api.calls if path == "/v1/profile/analyze")
        image_message = copy.deepcopy(analyze["messages"][0])
        self.assertIn("截图", image_message["content"])
        self.assertTrue(image_message["images"][0].startswith("data:image/png;base64,"))

        asyncio.run(self.bot.on_message(Message("结束并保存", message_id="om_save")))
        commit = next(payload for path, payload in self.api.calls if path == "/v1/profile/commit")
        self.assertNotIn("images", commit["messages"][0])
        self.assertIn("feishu://chat/oc_test/message/om_image", commit["messages"][0]["source_ref"])

    def test_image_key_can_be_recovered_from_raw_feishu_body(self) -> None:
        message = Message(
            "<image key=wrong_resource_key />",
            message_id="om_raw_image",
            raw={
                "event": {
                    "message": {
                        "content": json.dumps({"image_key": "img_real_raw_key"})
                    }
                }
            },
            resources=[{"type": "image", "file_key": "wrong_resource_key"}],
        )

        asyncio.run(self.bot.on_message(message))

        self.assertEqual(
            self.channel.downloads,
            [("img_real_raw_key", "image", "om_raw_image")],
        )

    def test_video_is_transcribed_and_key_frames_are_analyzed_but_not_committed(self) -> None:
        processor = FakeVideoProcessor()
        bot = FeishuBotController(
            api=self.api,  # type: ignore[arg-type]
            channel=self.channel,
            sessions=SessionStore(),
            video_processor=processor,
        )
        message = Message(
            "<media />",
            message_id="om_video",
            content={
                "kind": "media",
                "file_key": "file_video",
                "file_name": "merchant-tour.mp4",
                "duration_ms": 45_000,
            },
        )

        asyncio.run(bot.on_message(message))

        self.assertEqual(self.channel.downloads, [("file_video", "file", "om_video")])
        self.assertEqual(processor.calls[0][1:], ("merchant-tour.mp4", 45_000))
        analyze = next(payload for path, payload in self.api.calls if path == "/v1/profile/analyze")
        analyzed_message = analyze["messages"][0]
        self.assertIn("视频语音转写", analyzed_message["content"])
        self.assertIn("0451100323", analyzed_message["content"])
        self.assertTrue(analyzed_message["images"][0].startswith("data:image/jpeg;base64,"))

        asyncio.run(bot.on_message(Message("保存吧", message_id="om_save")))
        commit = next(payload for path, payload in self.api.calls if path == "/v1/profile/commit")
        self.assertNotIn("images", commit["messages"][0])
        self.assertIn(
            "feishu://chat/oc_test/message/om_video",
            commit["messages"][0]["source_ref"],
        )

    def test_video_over_five_minutes_is_rejected_before_download(self) -> None:
        processor = FakeVideoProcessor()
        bot = FeishuBotController(
            api=self.api,  # type: ignore[arg-type]
            channel=self.channel,
            sessions=SessionStore(),
            video_processor=processor,
        )

        asyncio.run(
            bot.on_message(
                Message(
                    "<media />",
                    message_id="om_long_video",
                    content={
                        "kind": "media",
                        "file_key": "file_long_video",
                        "file_name": "long.mp4",
                        "duration_ms": 301_000,
                    },
                )
            )
        )

        self.assertEqual(self.channel.downloads, [])
        self.assertEqual(processor.calls, [])
        self.assertIn("不能超过 5 分钟", self.channel.replies[-1][1]["text"])

    def test_group_video_is_not_downloaded_while_recording_is_paused(self) -> None:
        processor = FakeVideoProcessor()
        bot = FeishuBotController(
            api=self.api,  # type: ignore[arg-type]
            channel=self.channel,
            sessions=SessionStore(),
            video_processor=processor,
        )

        asyncio.run(
            bot.on_message(
                Message(
                    "暂停自动总结",
                    message_id="om_pause",
                    chat_id="oc_video_group",
                    chat_type="group",
                    mentioned_bot=True,
                )
            )
        )
        asyncio.run(
            bot.on_message(
                Message(
                    "<media />",
                    message_id="om_group_video",
                    chat_id="oc_video_group",
                    chat_type="group",
                    content={
                        "kind": "media",
                        "file_key": "file_group_video",
                        "file_name": "demo.mp4",
                        "duration_ms": 20_000,
                    },
                )
            )
        )

        self.assertEqual(self.channel.downloads, [])
        self.assertEqual(processor.calls, [])

    def test_session_store_restores_group_recording_and_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "sessions.json"
            store = SessionStore(state_path=state_path)
            session = store.get("oc_group:group:root")
            session.group_summary_enabled = True
            session.group_buffer.append(
                {
                    "role": "user",
                    "content": "测试成员：青禾便当联系人是林经理",
                    "source_ref": "feishu://chat/oc_group/message/om_one",
                }
            )
            session.group_pending_count = 1
            store.persist()

            restored = SessionStore(state_path=state_path).get("oc_group:group:root")

            self.assertTrue(restored.group_summary_enabled)
            self.assertEqual(restored.group_pending_count, 1)
            self.assertEqual(len(restored.group_buffer), 1)

    def test_session_store_migrates_old_groups_to_default_on(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "sessions.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "sessions": {
                            "oc_old:group:root": {"group_summary_enabled": False}
                        },
                    }
                ),
                encoding="utf-8",
            )

            restored = SessionStore(state_path=state_path).get("oc_old:group:root")

            self.assertTrue(restored.group_summary_enabled)

    def test_long_direct_chat_keeps_api_request_within_message_limit(self) -> None:
        for index in range(45):
            self.run_message(f"青禾便当补充资料 {index}")

        analyze_payloads = [
            payload for path, payload in self.api.calls if path == "/v1/profile/analyze"
        ]
        self.assertTrue(analyze_payloads)
        self.assertLessEqual(len(analyze_payloads[-1]["messages"]), 40)
        self.assertTrue(
            all(item["role"] == "user" for item in analyze_payloads[-1]["messages"])
        )

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

    def test_unique_crm_match_is_bound_to_commit(self) -> None:
        api = CrmAwareApi("unique")
        bot = FeishuBotController(api=api, channel=self.channel, sessions=SessionStore())

        async def scenario() -> None:
            await bot.on_message(Message("青禾便当的联系人是林经理"))
            await bot.on_message(Message("保存吧", message_id="om_save"))

        asyncio.run(scenario())

        commit = next(payload for path, payload in api.calls if path == "/v1/profile/commit")
        self.assertEqual(commit["merchant"]["id"], "987654321")

    def test_missing_crm_match_blocks_save_and_prompts_for_identifier(self) -> None:
        api = CrmAwareApi("not_found")
        bot = FeishuBotController(api=api, channel=self.channel, sessions=SessionStore())

        async def scenario() -> None:
            await bot.on_message(Message("青禾便当的联系人是林经理"))
            self.assertIn("CRM 中未找到", self.channel.replies[-1][1]["text"])
            await bot.on_message(Message("保存吧", message_id="om_save"))

        asyncio.run(scenario())

        self.assertNotIn("/v1/profile/commit", [path for path, _ in api.calls])
        self.assertIn("不会自动新建 CRM", self.channel.replies[-1][1]["text"])

    def test_save_refreshes_a_previous_crm_miss_and_uses_new_unique_match(self) -> None:
        api = RefreshingCrmApi()
        bot = FeishuBotController(api=api, channel=self.channel, sessions=SessionStore())

        async def scenario() -> None:
            await bot.on_message(Message("U Bowl Malatang 的联系人是林经理"))
            self.assertIn("CRM 中未找到", self.channel.replies[-1][1]["text"])
            await bot.on_message(Message("保存吧", message_id="om_save"))

        asyncio.run(scenario())

        commit = next(payload for path, payload in api.calls if path == "/v1/profile/commit")
        self.assertEqual(commit["merchant"]["id"], "6856334446")
        self.assertIn("已写入", self.channel.replies[-1][1]["text"])

    def test_save_reanalyzes_an_empty_preview_before_saying_there_is_nothing_new(self) -> None:
        api = EmptyThenUpdateApi()
        bot = FeishuBotController(api=api, channel=self.channel, sessions=SessionStore())

        async def scenario() -> None:
            await bot.on_message(Message("U Bowl Malatang 点餐屏有图片缺失"))
            await bot.on_message(Message("保存吧", message_id="om_save"))

        asyncio.run(scenario())

        self.assertEqual(api.analyze_count, 2)
        self.assertIn("/v1/profile/commit", [path for path, _ in api.calls])
        self.assertIn("已写入", self.channel.replies[-1][1]["text"])

    def test_ambiguous_crm_match_lists_candidates(self) -> None:
        api = CrmAwareApi("ambiguous")
        bot = FeishuBotController(api=api, channel=self.channel, sessions=SessionStore())

        asyncio.run(bot.on_message(Message("青禾便当的联系人是林经理")))

        reply = self.channel.replies[-1][1]["text"]
        self.assertIn("多个候选", reply)
        self.assertIn("CRM 101", reply)
        self.assertIn("CRM 102", reply)

    def test_group_messages_are_collected_without_explicit_enable(self) -> None:
        self.run_group("青禾便当的联系人是林经理")

        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.channel.replies, [])
        session = self.sessions.get("oc_group:group:root")
        self.assertTrue(session.group_summary_enabled)
        self.assertEqual(session.group_pending_count, 1)

    def test_group_meta_discussion_is_not_collected(self) -> None:
        examples = [
            "机器人加入群聊后默认长期记录",
            "特别注意切换商户前要执行 /discard",
            "@Josie 这个改成拉进群就开始自动监听",
            "我们明确一下客户信息录入的边界",
        ]
        for index, text in enumerate(examples):
            with self.subTest(text=text):
                self.run_group(text, message_id=f"om_meta_{index}")

        session = self.sessions.get("oc_group:group:root")
        self.assertEqual(session.group_pending_count, 0)
        self.assertEqual(session.group_buffer, [])
        self.assertEqual(self.api.calls, [])

    def test_group_profile_candidate_accepts_pronoun_address(self) -> None:
        self.assertTrue(_is_group_profile_candidate("他的地址是 123 Collins Street"))

    def test_group_casual_phone_chat_is_not_collected(self) -> None:
        self.run_group("你有空吗我们电话聊", message_id="om_phone_chat")

        session = self.sessions.get("oc_group:group:root")
        self.assertEqual(session.group_pending_count, 0)
        self.assertEqual(session.group_buffer, [])
        self.assertEqual(self.api.calls, [])

    def test_group_phone_number_is_still_collected(self) -> None:
        self.run_group("他的电话是 0451100323", message_id="om_phone_fact")

        session = self.sessions.get("oc_group:group:root")
        self.assertEqual(session.group_pending_count, 1)

    def test_group_rechecks_a_stale_crm_miss_before_analysis(self) -> None:
        api = CrmAwareApi("unique")
        sessions = SessionStore()
        session = sessions.get("oc_refresh:group:root")
        session.merchant = {
            "name": "9262 U Bowl Melbourne Central（海底捞）",
            "id": "9262",
        }
        session.current_profile_loaded = True
        session.crm_resolution = {"status": "not_found", "candidates": []}
        bot = FeishuBotController(
            api=api,  # type: ignore[arg-type]
            channel=self.channel,
            sessions=sessions,
            group_message_threshold=1,
            group_idle_seconds=600,
            group_idle_min_messages=1,
        )

        asyncio.run(
            bot.on_message(
                Message(
                    "他的地址是 123 Collins Street",
                    message_id="om_refresh",
                    chat_id="oc_refresh",
                    chat_type="group",
                )
            )
        )

        self.assertEqual(api.calls[0][0], "/v1/profile/read")
        self.assertEqual(session.merchant["id"], "987654321")
        self.assertEqual(session.crm_resolution["status"], "unique")

    def test_group_save_prunes_old_meta_discussion_from_buffer(self) -> None:
        session = self.sessions.get("oc_group:group:root")
        session.group_buffer = [
            {
                "role": "user",
                "content": "成员：机器人加入群后默认自动监听，切换商户前 /discard",
                "source_ref": "feishu://chat/oc_group/message/om_meta",
            },
            {
                "role": "user",
                "content": "成员：青禾便当地址是 1 青禾路",
                "source_ref": "feishu://chat/oc_group/message/om_address",
            },
        ]
        session.group_pending_count = 2

        self.run_group("保存吧", mentioned=True, message_id="om_save")

        analyze = next(payload for path, payload in self.api.calls if path == "/v1/profile/analyze")
        self.assertEqual(len(analyze["messages"]), 1)
        self.assertIn("1 青禾路", analyze["messages"][0]["content"])
        self.assertNotIn("自动监听", analyze["messages"][0]["content"])
        self.assertEqual(self.api.calls[-1][0], "/v1/profile/commit")

    def test_group_explicit_new_merchant_after_save_switches_automatically(self) -> None:
        api = NamedMerchantApi()
        bot = FeishuBotController(
            api=api,  # type: ignore[arg-type]
            channel=self.channel,
            sessions=SessionStore(),
            group_message_threshold=20,
            group_idle_seconds=600,
            group_idle_min_messages=5,
        )

        async def scenario() -> None:
            base = {"chat_id": "oc_switch", "chat_type": "group", "sender_name": "测试成员"}
            await bot.on_message(Message("青禾便当地址是 1 青禾路", message_id="om_one", **base))
            await bot.on_message(Message("保存吧", message_id="om_save_one", mentioned_bot=True, **base))
            await bot.on_message(
                Message("Dandong咖啡厅地址是 123 Collins Street", message_id="om_two", **base)
            )
            await bot.on_message(Message("保存吧", message_id="om_save_two", mentioned_bot=True, **base))

        asyncio.run(scenario())

        commits = [payload for path, payload in api.calls if path == "/v1/profile/commit"]
        self.assertEqual([item["merchant"]["name"] for item in commits], ["青禾便当", "Dandong咖啡厅"])

    def test_group_mentioned_save_without_data_does_not_write(self) -> None:
        self.run_group("保存吧", mentioned=True)

        self.assertEqual(self.api.calls, [])
        self.assertIn("未识别到商户名称", self.channel.replies[-1][1]["text"])

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
                self.assertIn("长期记录", channel.replies[-1][1]["text"])

    def test_group_negative_recording_language_pauses_collection(self) -> None:
        examples = ["不要开始记录", "请停止记录"]
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
                self.assertIn("已暂停", channel.replies[-1][1]["text"])
                session = bot.sessions.get(f"oc_negative_{index}:group:root")
                self.assertFalse(session.group_summary_enabled)

    def test_incidental_recording_language_is_collected_as_normal_content(self) -> None:
        self.run_group("门店开始记录每日销售额")

        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.channel.replies, [])
        session = self.sessions.get("oc_group:group:root")
        self.assertEqual(session.group_pending_count, 1)

    def test_group_pause_persists_and_continue_resumes(self) -> None:
        self.run_group("暂停自动总结", mentioned=True, message_id="om_pause")
        self.run_group("这条不应记录", message_id="om_ignored")

        session = self.sessions.get("oc_group:group:root")
        self.assertFalse(session.group_summary_enabled)
        self.assertEqual(session.group_pending_count, 0)

        self.run_group("继续记录", mentioned=True, message_id="om_resume")
        self.run_group("青禾便当联系人是林经理", message_id="om_contact")

        self.assertTrue(session.group_summary_enabled)
        self.assertEqual(session.group_pending_count, 1)
        self.assertIn("长期记录", self.channel.replies[-1][1]["text"])

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
        self.assertIn("业务与联系人", reply)
        self.assertIn("已确认决定", reply)
        self.assertIn("以上内容尚未写入", reply)
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
            self.assertIn("已写入", self.channel.replies[-1][1]["text"])

        asyncio.run(scenario())

        analyzes = [payload for path, payload in api.calls if path == "/v1/profile/analyze"]
        corrected_messages = analyzes[-1]["messages"]
        self.assertTrue(
            any("主要联系人是林经理" in item["content"] for item in corrected_messages)
        )
        self.assertTrue(any("Dandong咖啡厅" in item["content"] for item in corrected_messages))
        commit = next(payload for path, payload in api.calls if path == "/v1/profile/commit")
        self.assertEqual(commit["merchant"]["name"], "Dandong咖啡厅")

    def test_group_pronoun_after_save_reuses_last_merchant(self) -> None:
        api = PronounContinuationApi()
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
                "chat_id": "oc_pronoun",
                "chat_type": "group",
                "sender_name": "测试成员",
            }
            await bot.on_message(Message("开始记录", mentioned_bot=True, **base))
            await bot.on_message(
                Message("汪汪咖啡店联系人是李经理", message_id="om_first", **base)
            )
            await bot.on_message(
                Message("保存吧", message_id="om_save_first", mentioned_bot=True, **base)
            )
            await bot.on_message(
                Message("他们有 POS 机业务员，规模不小", message_id="om_pronoun", **base)
            )
            self.assertIn("商户：汪汪咖啡店", self.channel.replies[-1][1]["text"])
            await bot.on_message(
                Message("保存吧", message_id="om_save_second", mentioned_bot=True, **base)
            )

        asyncio.run(scenario())

        commits = [payload for path, payload in api.calls if path == "/v1/profile/commit"]
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[1]["merchant"]["name"], "汪汪咖啡店")
        reads = [payload for path, payload in api.calls if path == "/v1/profile/read"]
        self.assertGreaterEqual(len(reads), 2)
        self.assertEqual(reads[-1]["merchant_name"], "汪汪咖啡店")

    def test_group_conflict_lists_values_and_confirmation_finishes_pending_save(self) -> None:
        api = ConflictThenConfirmationApi()
        bot = FeishuBotController(
            api=api,  # type: ignore[arg-type]
            channel=self.channel,
            sessions=SessionStore(),
            group_message_threshold=20,
            group_idle_seconds=600,
            group_idle_min_messages=5,
        )

        async def scenario() -> None:
            base = {
                "chat_id": "oc_conflict",
                "chat_type": "group",
                "sender_name": "测试成员",
            }
            await bot.on_message(Message("开始记录", mentioned_bot=True, **base))
            await bot.on_message(
                Message(
                    "汪汪咖啡店店长 Lily，电话 0451100323",
                    message_id="om_new_phone",
                    **base,
                )
            )
            await bot.on_message(
                Message("保存吧", message_id="om_save", mentioned_bot=True, **base)
            )
            conflict_reply = self.channel.replies[-1][1]["text"]
            self.assertIn("电话：045123455555 → 0451100323", conflict_reply)
            self.assertIn("确认替换", conflict_reply)

            replies_before_confirmation = len(self.channel.replies)
            await bot.on_message(
                Message(
                    "确认替换",
                    message_id="om_confirm",
                    mentioned_bot=True,
                    **base,
                )
            )
            self.assertEqual(len(self.channel.replies), replies_before_confirmation + 1)
            self.assertIn("已写入", self.channel.replies[-1][1]["text"])

        asyncio.run(scenario())

        commits = [payload for path, payload in api.calls if path == "/v1/profile/commit"]
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["updates"][0]["value"], "0451100323")

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
        self.assertIn("业务与联系人", self.channel.replies[-1][1]["text"])

        call_count = len(self.api.calls)
        self.run_group("暂停自动总结", mentioned=True, message_id="om_pause")
        self.run_group("这条消息不应进入模型", message_id="om_after")
        self.assertEqual(len(self.api.calls), call_count)

    def test_group_idle_summary_after_one_effective_message(self) -> None:
        async def scenario() -> None:
            bot = FeishuBotController(
                api=self.api,  # type: ignore[arg-type]
                channel=self.channel,
                sessions=SessionStore(),
                group_message_threshold=20,
                group_idle_seconds=0.01,  # type: ignore[arg-type]
                group_idle_min_messages=1,
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
            await asyncio.sleep(0.03)

        asyncio.run(scenario())

        self.assertIn("业务与联系人", self.channel.replies[-1][1]["text"])


if __name__ == "__main__":
    unittest.main()
