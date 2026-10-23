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
    ImageBatchLimitError,
    Session,
    SessionStore,
    _apply_explicit_module_to_preview,
    _group_summary_text,
    _is_group_save_confirmation,
    _is_media_status_request,
    _is_group_profile_candidate,
    _merge_analysis_previews,
    _message_batches_for_analysis,
    _merchant_hint_from_group_name,
    _preview_text,
    _profile_for_analysis,
    _save_request_evidence,
    _without_video_placeholders,
)
from merchant_profile_agent.codex_dialogue import DialogueDecision, DialogueTurn
from merchant_profile_agent.media_archive import MediaStagingStore
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


@dataclass
class ChatInfo:
    name: str | None = None


class FakeChannel:
    def __init__(self) -> None:
        self.replies = []
        self.downloads = []
        self.reactions = []
        self.typing_started = []
        self.typing_stopped = []
        self.chat_info: ChatInfo | None = None
        self.chat_info_calls: list[str] = []

    async def reply(self, message, content, opts=None):
        self.replies.append((message, content, opts))

    async def add_reaction(self, message_id, emoji_type):
        self.reactions.append((message_id, emoji_type))
        return {"success": True}

    async def add_typing_reaction(self, message_id):
        self.typing_started.append(message_id)
        return f"reaction_{message_id}"

    async def remove_typing_reaction(self, message_id, reaction_id):
        self.typing_stopped.append((message_id, reaction_id))
        return True

    async def get_chat_info(self, chat_id):
        self.chat_info_calls.append(chat_id)
        return self.chat_info

    async def download_resource(self, file_key, resource_type="image", message_id=None):
        self.downloads.append((file_key, resource_type, message_id))
        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )


class MissingImageChannel(FakeChannel):
    async def download_resource(self, file_key, resource_type="image", message_id=None):
        self.downloads.append((file_key, resource_type, message_id))
        return None


class RetriableImageChannel(MissingImageChannel):
    def __init__(self, original: Message) -> None:
        super().__init__()
        self.original = original
        self.retrying = False

    async def fetch_inbound_message(self, message_id):
        self.retrying = True
        return self.original if message_id == self.original.message_id else None

    async def download_resource(self, file_key, resource_type="image", message_id=None):
        if not self.retrying:
            return await super().download_resource(file_key, resource_type, message_id)
        return await FakeChannel.download_resource(self, file_key, resource_type, message_id)


class ReferencedImageChannel(FakeChannel):
    async def download_resource(self, file_key, resource_type="image", message_id=None):
        self.downloads.append((file_key, resource_type, message_id))
        if message_id != "om_parent":
            return None
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


class GroupNameCrmApi:
    def __init__(self, *, status: str = "unique") -> None:
        self.status = status
        self.calls = []

    def post(self, path, payload):
        self.calls.append((path, copy.deepcopy(payload)))
        if path != "/v1/profile/crm/resolve":
            raise AssertionError(path)
        candidates = []
        if self.status == "unique":
            candidates = [
                {
                    "work_item_id": "7140000123",
                    "name": "Little Amigos Fountain Gate",
                }
            ]
        elif self.status == "ambiguous":
            candidates = [
                {"work_item_id": "7140000123", "name": "Little Amigos Fountain Gate"},
                {"work_item_id": "7140000456", "name": "Little Amigos"},
            ]
        return {
            "crm": {
                "status": self.status,
                "query": {"merchant_name": payload["merchant_name"]},
                "candidates": candidates,
                "read_only": True,
            },
            "write_performed": False,
        }


class LiveDocumentApi(FakeApi):
    def post(self, path, payload):
        if path != "/v1/profile/read":
            return super().post(path, payload)
        self.calls.append((path, copy.deepcopy(payload)))
        return {
            "profile": {
                "merchant": {
                    "name": payload["merchant_name"],
                    "id": payload.get("merchant_id"),
                },
                "fields": {},
                "event_count": 2,
                "documents": [
                    {
                        "document_type": "merchant-directory",
                        "label": "商户资料目录",
                        "url": "https://example.feishu.cn/wiki/merchant",
                    },
                    {
                        "document_type": "business-contacts",
                        "label": "01｜业务与联系人",
                        "url": "https://example.feishu.cn/wiki/contacts-live",
                    },
                ],
            }
        }


class FakeDialogueRouter:
    def __init__(self, decisions: list[DialogueDecision]) -> None:
        self.decisions = list(decisions)
        self.calls = []

    def route(self, *, text, context, session_id=None):
        self.calls.append(
            {
                "text": text,
                "context": copy.deepcopy(dict(context)),
                "session_id": session_id,
            }
        )
        decision = self.decisions.pop(0)
        return DialogueTurn(
            session_id=session_id or "asess_test_dialogue",
            decision=decision,
        )


class FakeMediaArchiver:
    def __init__(self) -> None:
        self.calls = []

    def archive(self, item, *, wiki_token):
        self.calls.append((copy.deepcopy(item), wiki_token))
        return {
            "file_token": "file_archived_123",
            "url": "https://example.feishu.cn/file/file_archived_123",
            "name": item["file_name"],
        }


class ArchiveApi(FakeApi):
    def post(self, path, payload):
        if path != "/v1/profile/commit":
            return super().post(path, payload)
        self.calls.append((path, copy.deepcopy(payload)))
        return {
            "write_performed": True,
            "result": {
                "store_status": "stored",
                "wiki_documents_updated": 2,
                "documents": [
                    {
                        "document_type": "merchant-overview",
                        "node_token": "wik_overview",
                        "url": "https://example.feishu.cn/wiki/overview",
                    },
                    {
                        "document_type": "business-contacts",
                        "node_token": "wik_contacts",
                        "url": "https://example.feishu.cn/wiki/contacts",
                    },
                    {
                        "document_type": "store-profile",
                        "node_token": "wik_store",
                        "url": "https://example.feishu.cn/wiki/store",
                    },
                    {
                        "document_type": "package-commercial",
                        "node_token": "wik_package",
                        "url": "https://example.feishu.cn/wiki/package",
                    },
                    {
                        "document_type": "document-archive",
                        "node_token": "wik_archive",
                        "url": "https://example.feishu.cn/wiki/archive",
                    },
                ],
            },
        }


class ReadbackVerifiedArchiveApi(ArchiveApi):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, dict[str, object]] = {}

    def post(self, path, payload):
        if path == "/v1/profile/read":
            self.calls.append((path, copy.deepcopy(payload)))
            return {
                "profile": {
                    "fields": copy.deepcopy(self.fields),
                    "event_count": 1 if self.fields else 0,
                }
            }
        if path != "/v1/profile/commit":
            return super().post(path, payload)
        result = super().post(path, payload)
        for update in payload["updates"]:
            self.fields[update["field_path"]] = {"value": copy.deepcopy(update["value"])}
        result["write_performed"] = False
        result["result"].update(
            {
                "store_status": "partial_failure",
                "duplicate": False,
                "records_written": 1,
            }
        )
        return result


class UnverifiedIncompleteApi(FakeApi):
    def post(self, path, payload):
        if path != "/v1/profile/commit":
            return super().post(path, payload)
        self.calls.append((path, copy.deepcopy(payload)))
        return {
            "write_performed": False,
            "result": {
                "store_status": "partial_failure",
                "duplicate": False,
                "records_written": 1,
                "documents": [],
            },
        }


class MediaOnlyArchiveApi(ArchiveApi):
    def post(self, path, payload):
        if path == "/v1/profile/analyze":
            self.calls.append((path, copy.deepcopy(payload)))
            return {
                "merchant": {"name": "青禾便当", "id": None},
                "reply": "未从画面提取到新的文字资料。",
                "profile_updates": [],
                "conflicts": [],
                "save_readiness": "needs_input",
                "next_question": None,
                "summary_points": [],
                "decisions": [],
                "action_items": [],
                "information_gaps": [],
            }
        return super().post(path, payload)


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


class UnanchoredSwitchApi(FakeApi):
    def post(self, path, payload):
        if path != "/v1/profile/analyze":
            return super().post(path, payload)
        self.calls.append((path, copy.deepcopy(payload)))
        content = "\n".join(item["content"] for item in payload["messages"])
        proposed_name = "Dandong咖啡厅" if "他的地址" in content else "青禾便当"
        return {
            "merchant": {"name": proposed_name, "id": None},
            "reply": "已识别。",
            "profile_updates": [
                {
                    "field_path": "stores.primary.address",
                    "value": "123 Collins Street" if "他的地址" in content else "1 青禾路",
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
    def test_group_name_is_cleaned_for_crm_lookup(self) -> None:
        self.assertEqual(
            _merchant_hint_from_group_name(
                "Fluffy Soufflé Newtown-[客户管理CRM group] 10.26开业"
            ),
            "Fluffy Soufflé Newtown",
        )
        self.assertEqual(
            _merchant_hint_from_group_name("Little Amigos Fountain Gate"),
            "Little Amigos Fountain Gate",
        )
        self.assertIsNone(_merchant_hint_from_group_name("客户信息录入测试群"))

    def test_natural_save_command_can_explicitly_include_text_and_images(self) -> None:
        self.assertTrue(_is_group_save_confirmation("保存吧 文字和图片都要"))
        self.assertTrue(_is_group_save_confirmation("请保存以上文字、图片和附件"))
        self.assertFalse(_is_group_save_confirmation("商户希望保存会员手机号"))

    def test_trailing_save_command_preserves_preceding_evidence(self) -> None:
        self.assertEqual(
            _save_request_evidence("Little Amigos Fountain Gate 保存吧"),
            "Little Amigos Fountain Gate",
        )
        self.assertEqual(
            _save_request_evidence("Little Amigos Fountain Gate 保存吧 文字和图片都要"),
            "Little Amigos Fountain Gate",
        )
        self.assertIsNone(_save_request_evidence("保存吧 文字和图片都要"))
        self.assertIsNone(_save_request_evidence("商户希望保存会员手机号"))

    def test_group_name_unique_crm_match_binds_merchant_once(self) -> None:
        api = GroupNameCrmApi()
        self.channel.chat_info = ChatInfo(
            "Little Amigos Fountain Gate-[客户管理CRM group] 10.26开业"
        )
        sessions = SessionStore()
        bot = FeishuBotController(
            api=api,  # type: ignore[arg-type]
            channel=self.channel,
            sessions=sessions,
        )

        async def scenario() -> None:
            base = {
                "chat_id": "oc_named_group",
                "chat_type": "group",
                "mentioned_bot": True,
            }
            await bot.on_message(Message("帮助", message_id="om_help_one", **base))
            await bot.on_message(Message("帮助", message_id="om_help_two", **base))

        asyncio.run(scenario())

        session = sessions.get("oc_named_group:group:root")
        self.assertEqual(
            session.merchant,
            {"name": "Little Amigos Fountain Gate", "id": "7140000123"},
        )
        self.assertEqual(session.group_name, self.channel.chat_info.name)
        self.assertEqual(session.group_merchant_hint, "Little Amigos Fountain Gate")
        self.assertEqual(self.channel.chat_info_calls, ["oc_named_group"])
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(api.calls[0][1]["merchant_name"], "Little Amigos Fountain Gate")

    def test_ambiguous_group_name_does_not_bind_merchant(self) -> None:
        api = GroupNameCrmApi(status="ambiguous")
        self.channel.chat_info = ChatInfo("Little Amigos Fountain Gate CRM group")
        sessions = SessionStore()
        bot = FeishuBotController(
            api=api,  # type: ignore[arg-type]
            channel=self.channel,
            sessions=sessions,
        )

        asyncio.run(
            bot.on_message(
                Message(
                    "保存吧",
                    message_id="om_ambiguous_save",
                    chat_id="oc_ambiguous_group",
                    chat_type="group",
                    mentioned_bot=True,
                )
            )
        )

        session = sessions.get("oc_ambiguous_group:group:root")
        self.assertIsNone(session.merchant)
        self.assertIn("CRM 匹配到多个客户", self.channel.replies[-1][1]["text"])

    def test_media_failure_detail_question_is_recognized(self) -> None:
        self.assertTrue(_is_media_status_request("哪个图片未读取"))
        self.assertTrue(_is_media_status_request("告诉我哪些附件读取失败"))
        self.assertFalse(_is_media_status_request("图片里是什么"))

    def test_user_can_confirm_a_low_confidence_fact_module(self) -> None:
        session = Session(
            preview={
                "merchant": {"name": "MCKINLY Eatery", "id": None},
                "profile_updates": [
                    {
                        "field_path": "system.network_topology",
                        "value": "NBN 路由器通过墙内网线连接打印机。",
                        "source_ref": "feishu://message/network",
                        "replace_confirmed": False,
                        "state": "explicit",
                        "document_type": "system-configuration",
                    }
                ],
                "module_classifications": [
                    {
                        "field_path": "system.network_topology",
                        "document_type": "system-configuration",
                        "confidence": 0.6,
                        "reason": "边界不明确",
                        "status": "needs_confirmation",
                    }
                ],
                "classification_review_required": True,
                "save_readiness": "needs_confirmation",
            }
        )

        changed = _apply_explicit_module_to_preview(session, "hardware-installation")

        self.assertEqual(changed, 1)
        self.assertEqual(
            session.preview["profile_updates"][0]["field_path"],
            "hardware.network_topology",
        )
        self.assertFalse(session.preview["classification_review_required"])
        self.assertEqual(session.preview["save_readiness"], "ready")

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

        batches = _message_batches_for_analysis(messages)

        self.assertEqual(len(batches), 2)
        self.assertEqual(
            [sum(len(item.get("images", [])) for item in batch) for batch in batches],
            [8, 4],
        )
        self.assertEqual(
            sum(
                len(item.get("images", []))
                for batch in batches
                for item in batch
            ),
            12,
        )
        self.assertEqual(len(messages[0]["images"]), 4)

    def test_analysis_image_budget_has_clear_session_limit(self) -> None:
        png = "data:image/png;base64," + base64.b64encode(b"png").decode("ascii")
        messages = [
            {
                "role": "user",
                "content": "many images",
                "source_ref": "feishu://message/many",
                "images": [png] * 41,
            }
        ]

        with self.assertRaisesRegex(ImageBatchLimitError, "单轮最多自动处理 40 张"):
            _message_batches_for_analysis(messages)

    def test_analysis_batches_merge_into_one_preview(self) -> None:
        first = {
            "merchant": {"name": "青禾便当", "id": None},
            "profile_updates": [
                {
                    "field_path": "contacts.primary.name",
                    "value": "林经理",
                    "source_ref": "feishu://message/1",
                    "document_type": "business-contacts",
                }
            ],
            "conflicts": [],
            "module_classifications": [],
            "summary_points": ["联系人是林经理"],
        }
        second = {
            "merchant": {"name": "青禾便当", "id": None},
            "profile_updates": [
                {
                    "field_path": "hardware.pos.model",
                    "value": "P2 Pro",
                    "source_ref": "feishu://message/2",
                    "document_type": "hardware-equipment",
                }
            ],
            "conflicts": [],
            "module_classifications": [],
            "summary_points": ["POS 型号是 P2 Pro"],
        }

        merged = _merge_analysis_previews([first, second])

        self.assertEqual(merged["analysis_batches"], 2)
        self.assertEqual(len(merged["profile_updates"]), 2)
        self.assertEqual(merged["save_readiness"], "ready")
        self.assertEqual(
            merged["affected_modules"],
            ["business-contacts", "hardware-equipment"],
        )

    def test_controller_calls_analyzer_for_every_image_batch(self) -> None:
        png = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        messages = [
            {
                "role": "user",
                "content": "青禾便当的图片资料",
                "source_ref": "feishu://message/images",
                "images": [png] * 12,
            }
        ]

        preview = asyncio.run(self.bot._analyze_in_batches(messages, {}))

        calls = [payload for path, payload in self.api.calls if path == "/v1/profile/analyze"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [
                sum(len(item.get("images", [])) for item in payload["messages"])
                for payload in calls
            ],
            [8, 4],
        )
        self.assertEqual(preview["analysis_batches"], 2)

    def test_html_escaped_video_placeholders_are_removed(self) -> None:
        text = (
            '成员：&lt;video key=&quot;file_test&quot; name=&quot;demo.mp4&quot;/&gt;\n'
            "视频语音转写：图片缺失"
        )

        cleaned = _without_video_placeholders(text)

        self.assertNotIn("file_test", cleaned)
        self.assertNotIn("&lt;video", cleaned)
        self.assertIn("图片缺失", cleaned)

    def test_product_capability_question_is_not_profile_evidence(self) -> None:
        self.assertFalse(
            _is_group_profile_candidate("C260M 能设置没有打印纸后出声音提醒吗？")
        )
        self.assertFalse(
            _is_group_profile_candidate(
                "C260M 有三种设置方式：方式1使用拨码开关；方式2下载调试工具，"
                "点击设置后重启打印机。补充说明：缺纸时会蜂鸣。"
            )
        )
        self.assertTrue(
            _is_group_profile_candidate("C260M 缺纸时没有声音提醒，客户已反馈")
        )
        self.assertTrue(
            _is_group_profile_candidate("https://u26197356.ct.sendgrid.net/click")
        )

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

    def test_password_is_hidden_from_previews_conflicts_and_model_context(self) -> None:
        example_password = "Example-Only-Password-123"
        result = {
            "profile_updates": [
                {
                    "field_path": "credentials.pisell_admin.password",
                    "value": example_password,
                }
            ],
            "conflicts": [
                {
                    "field_path": "credentials.pisell_admin.password",
                    "current_value": "Old-Example-Password",
                    "proposed_value": example_password,
                }
            ],
            "decisions": [f"使用密码 {example_password}"],
            "save_readiness": "needs_confirmation",
        }

        for rendered in (_preview_text(result), _group_summary_text(result)):
            self.assertIn("密码：••••••", rendered)
            self.assertNotIn(example_password, rendered)
            self.assertNotIn("Old-Example-Password", rendered)

        profile = {
            "fields": {
                "credentials.pisell_admin.password": {"value": example_password},
                "credentials.pisell_admin.username": {"value": "demo@example.invalid"},
            }
        }
        sanitized = _profile_for_analysis(profile)
        self.assertEqual(
            sanitized["fields"]["credentials.pisell_admin.password"]["value"],
            "[已保存，未显示]",
        )
        self.assertEqual(
            sanitized["fields"]["credentials.pisell_admin.username"]["value"],
            "demo@example.invalid",
        )
        self.assertEqual(
            profile["fields"]["credentials.pisell_admin.password"]["value"],
            example_password,
        )

    def test_account_and_password_are_group_profile_candidates(self) -> None:
        self.assertTrue(
            _is_group_profile_candidate(
                "商户后台账号 demo@example.invalid，密码 Example-Only-Password-123"
            )
        )

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

    def test_image_is_uploaded_and_linked_in_matching_module_on_save(self) -> None:
        api = ArchiveApi()
        archiver = FakeMediaArchiver()
        with tempfile.TemporaryDirectory() as directory:
            staging = MediaStagingStore(Path(directory))
            bot = FeishuBotController(
                api=api,  # type: ignore[arg-type]
                channel=self.channel,
                sessions=SessionStore(),
                media_staging=staging,
                media_archiver=archiver,  # type: ignore[arg-type]
            )
            message = Message(
                "<image />",
                message_id="om_archive_image",
                content={"kind": "image", "image_key": "img_archive"},
            )

            asyncio.run(bot.on_message(message))
            self.assertIn("待保存附件", self.channel.replies[-1][1]["text"])
            self.assertIn("业务与联系人", self.channel.replies[-1][1]["text"])
            session = bot.sessions.get("oc_test:ou_test:root")
            staged_path = Path(session.pending_media[0]["local_path"])
            self.assertTrue(staged_path.is_file())

            asyncio.run(bot.on_message(Message("保存吧", message_id="om_save_archive")))

            commits = [payload for path, payload in api.calls if path == "/v1/profile/commit"]
            self.assertEqual(len(commits), 2)
            first_paths = {item["field_path"] for item in commits[0]["updates"]}
            self.assertTrue(any(path.startswith("business.evidence.media_") for path in first_paths))
            self.assertIn("business.evidence.media_", " ".join(first_paths))
            second_values = {
                item["field_path"].rsplit(".", 1)[-1]: item["value"]
                for item in commits[1]["updates"]
            }
            self.assertEqual(second_values["url"], "https://example.feishu.cn/file/file_archived_123")
            self.assertEqual(second_values["status"], "已保存")
            self.assertEqual(archiver.calls[0][1], "wik_contacts")
            self.assertFalse(staged_path.exists())
            self.assertIn("已保存到对应模块的附件：1 个", self.channel.replies[-1][1]["text"])

    def test_incomplete_commit_with_verified_readback_continues_media_save(self) -> None:
        api = ReadbackVerifiedArchiveApi()
        archiver = FakeMediaArchiver()
        with tempfile.TemporaryDirectory() as directory:
            bot = FeishuBotController(
                api=api,  # type: ignore[arg-type]
                channel=self.channel,
                sessions=SessionStore(),
                media_staging=MediaStagingStore(Path(directory)),
                media_archiver=archiver,  # type: ignore[arg-type]
            )
            asyncio.run(
                bot.on_message(
                    Message(
                        "<image />",
                        message_id="om_readback_image",
                        content={"kind": "image", "image_key": "img_readback"},
                    )
                )
            )
            session = bot.sessions.get("oc_test:ou_test:root")

            asyncio.run(bot.on_message(Message("保存吧", message_id="om_readback_save")))

            commits = [payload for path, payload in api.calls if path == "/v1/profile/commit"]
            self.assertEqual(len(commits), 2)
            self.assertEqual(len(archiver.calls), 1)
            self.assertEqual(session.pending_media, [])
            self.assertNotIn("提交未完成", self.channel.replies[-1][1]["text"])
            self.assertIn("已保存到对应模块的附件：1 个", self.channel.replies[-1][1]["text"])

    def test_unverified_incomplete_commit_gives_an_actionable_retry(self) -> None:
        api = UnverifiedIncompleteApi()
        bot = FeishuBotController(
            api=api,  # type: ignore[arg-type]
            channel=self.channel,
            sessions=SessionStore(),
        )

        asyncio.run(bot.on_message(Message("青禾便当的联系人是林经理")))
        asyncio.run(bot.on_message(Message("保存吧", message_id="om_incomplete_save")))

        reply = self.channel.replies[-1][1]["text"]
        self.assertIn("请稍后 @机器人 回复“保存吧”重试", reply)
        self.assertIn("无需重新发送资料或附件", reply)

    def test_media_only_message_falls_back_to_document_archive(self) -> None:
        api = MediaOnlyArchiveApi()
        archiver = FakeMediaArchiver()
        with tempfile.TemporaryDirectory() as directory:
            bot = FeishuBotController(
                api=api,  # type: ignore[arg-type]
                channel=self.channel,
                sessions=SessionStore(),
                media_staging=MediaStagingStore(Path(directory)),
                media_archiver=archiver,  # type: ignore[arg-type]
            )
            asyncio.run(
                bot.on_message(
                    Message(
                        "青禾便当现场截图",
                        message_id="om_media_only",
                        content={"kind": "image", "image_key": "img_media_only"},
                    )
                )
            )
            asyncio.run(bot.on_message(Message("保存吧", message_id="om_save_media")))

        commits = [payload for path, payload in api.calls if path == "/v1/profile/commit"]
        self.assertEqual(len(commits), 2)
        self.assertTrue(
            all(
                item["field_path"].startswith("documents.media_")
                for item in commits[0]["updates"]
            )
        )
        self.assertEqual(archiver.calls[0][1], "wik_archive")
        self.assertIn("已保存到对应模块的附件：1 个", self.channel.replies[-1][1]["text"])

    def test_user_can_override_fallback_archive_before_save(self) -> None:
        api = MediaOnlyArchiveApi()
        archiver = FakeMediaArchiver()
        with tempfile.TemporaryDirectory() as directory:
            bot = FeishuBotController(
                api=api,  # type: ignore[arg-type]
                channel=self.channel,
                sessions=SessionStore(),
                media_staging=MediaStagingStore(Path(directory)),
                media_archiver=archiver,  # type: ignore[arg-type]
            )
            asyncio.run(
                bot.on_message(
                    Message(
                        "青禾便当现场截图",
                        message_id="om_media_override",
                        content={"kind": "image", "image_key": "img_media_override"},
                    )
                )
            )
            self.assertIn("资料归档（未识别明确归属，兜底）", self.channel.replies[-1][1]["text"])
            asyncio.run(
                bot.on_message(
                    Message("附件归到门店档案", message_id="om_route_media")
                )
            )
            asyncio.run(bot.on_message(Message("保存吧", message_id="om_save_media")))

        commits = [payload for path, payload in api.calls if path == "/v1/profile/commit"]
        self.assertTrue(
            all(
                item["field_path"].startswith("stores.evidence.media_")
                for item in commits[0]["updates"]
            )
        )
        self.assertEqual(archiver.calls[0][1], "wik_store")

    def test_pdf_file_uses_the_same_document_archive_flow(self) -> None:
        api = MediaOnlyArchiveApi()
        archiver = FakeMediaArchiver()
        with tempfile.TemporaryDirectory() as directory:
            bot = FeishuBotController(
                api=api,  # type: ignore[arg-type]
                channel=self.channel,
                sessions=SessionStore(),
                media_staging=MediaStagingStore(Path(directory)),
                media_archiver=archiver,  # type: ignore[arg-type]
            )
            asyncio.run(
                bot.on_message(
                    Message(
                        "<file />",
                        message_id="om_pdf",
                        content={
                            "kind": "file",
                            "file_key": "file_pdf",
                            "file_name": "NAB_Statement.pdf",
                        },
                    )
                )
            )
            self.assertEqual(self.channel.downloads, [("file_pdf", "file", "om_pdf")])
            analyze = next(payload for path, payload in api.calls if path == "/v1/profile/analyze")
            self.assertIn("NAB_Statement.pdf", analyze["messages"][0]["content"])
            self.assertNotIn("has_media", analyze["messages"][0])
            self.assertIn("NAB_Statement.pdf", self.channel.replies[-1][1]["text"])

            asyncio.run(bot.on_message(Message("保存吧", message_id="om_save_pdf")))

        self.assertEqual(archiver.calls[0][0]["file_name"], "NAB_Statement.pdf")
        self.assertEqual(archiver.calls[0][1], "wik_archive")
        self.assertIn("已保存到对应模块的附件：1 个", self.channel.replies[-1][1]["text"])

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

    def test_image_download_retries_referenced_message_owner(self) -> None:
        channel = ReferencedImageChannel()
        bot = FeishuBotController(
            api=self.api,  # type: ignore[arg-type]
            channel=channel,
            sessions=SessionStore(),
        )
        message = Message(
            "青禾便当的联系人是林经理\n![image](img_quoted)",
            message_id="om_wrapper",
            content={"kind": "post"},
            raw={
                "event": {
                    "message": {
                        "message_id": "om_wrapper",
                        "parent_id": "om_parent",
                        "content": json.dumps({"image_key": "img_quoted"}),
                    }
                }
            },
        )

        asyncio.run(bot.on_message(message))

        self.assertEqual(
            channel.downloads,
            [
                ("img_quoted", "image", "om_wrapper"),
                ("img_quoted", "image", "om_parent"),
            ],
        )
        analyze = next(payload for path, payload in self.api.calls if path == "/v1/profile/analyze")
        self.assertIn("images", analyze["messages"][0])

    def test_unavailable_link_preview_image_does_not_block_text(self) -> None:
        channel = MissingImageChannel()
        bot = FeishuBotController(
            api=self.api,  # type: ignore[arg-type]
            channel=channel,
            sessions=SessionStore(),
        )
        message = Message(
            "青禾便当的联系人是林经理\n![image](img_link_preview)",
            message_id="om_link_preview",
            content={"kind": "post"},
            resources=[{"type": "image", "file_key": "img_link_preview"}],
        )

        asyncio.run(bot.on_message(message))

        analyze = next(payload for path, payload in self.api.calls if path == "/v1/profile/analyze")
        self.assertIn("青禾便当的联系人是林经理", analyze["messages"][0]["content"])
        self.assertNotIn("images", analyze["messages"][0])
        self.assertNotIn("图片下载失败", channel.replies[-1][1]["text"])

    def test_unavailable_image_only_message_still_reports_failure(self) -> None:
        channel = MissingImageChannel()
        bot = FeishuBotController(
            api=self.api,  # type: ignore[arg-type]
            channel=channel,
            sessions=SessionStore(),
        )
        message = Message(
            "![image](img_missing)",
            message_id="om_missing_image",
            content={"kind": "image", "image_key": "img_missing"},
        )

        asyncio.run(bot.on_message(message))

        self.assertEqual(self.api.calls, [])
        self.assertIn("图片下载失败", channel.replies[-1][1]["text"])

    def test_group_support_question_with_broken_preview_is_silent(self) -> None:
        channel = MissingImageChannel()
        sessions = SessionStore()
        bot = FeishuBotController(
            api=self.api,  # type: ignore[arg-type]
            channel=channel,
            sessions=sessions,
        )
        message = Message(
            "C260M 能设置没有打印纸后出声音提醒吗？\n![image](img_link_preview)",
            message_id="om_support_question",
            chat_id="oc_support",
            chat_type="group",
            resources=[{"type": "image", "file_key": "img_link_preview"}],
        )

        asyncio.run(bot.on_message(message))

        self.assertEqual(self.api.calls, [])
        self.assertEqual(channel.replies, [])
        self.assertEqual(channel.reactions, [])
        self.assertEqual(sessions.get("oc_support:group:root").group_buffer, [])

    def test_group_profile_message_is_buffered_without_ok_reaction(self) -> None:
        self.run_group("青禾便当联系人是林经理", message_id="om_status")

        self.assertEqual(self.channel.typing_started, ["om_status"])
        self.assertEqual(
            self.channel.typing_stopped,
            [("om_status", "reaction_om_status")],
        )
        self.assertEqual(self.channel.reactions, [])
        self.assertEqual(self.channel.replies, [])

    def test_group_image_is_retained_without_ok_or_preflight_analysis(self) -> None:
        message = Message(
            "青禾便当现场配置截图",
            message_id="om_recordable_image",
            chat_id="oc_recordable_image",
            chat_type="group",
            content={"kind": "image", "image_key": "img_recordable"},
        )

        asyncio.run(self.bot.on_message(message))

        self.assertEqual(self.channel.reactions, [])
        self.assertNotIn("/v1/profile/analyze", [path for path, _ in self.api.calls])
        self.assertEqual(
            len(self.sessions.get("oc_recordable_image:group:root").group_buffer),
            1,
        )

    def test_group_image_without_extractable_facts_is_retained_silently(self) -> None:
        api = MediaOnlyArchiveApi()
        sessions = SessionStore()
        bot = FeishuBotController(
            api=api,  # type: ignore[arg-type]
            channel=self.channel,
            sessions=sessions,
        )
        message = Message(
            "@Macaw",
            message_id="om_empty_image",
            chat_id="oc_empty_image",
            chat_type="group",
            content={"kind": "image", "image_key": "img_empty"},
        )

        asyncio.run(bot.on_message(message))

        self.assertEqual(self.channel.reactions, [])
        self.assertEqual(self.channel.replies, [])
        self.assertEqual(
            len(sessions.get("oc_empty_image:group:root").group_buffer),
            1,
        )

    def test_unrecognized_mentioned_command_returns_help_instead_of_silence(self) -> None:
        self.run_group("芝麻开门", mentioned=True, message_id="om_unknown")

        self.assertEqual(self.channel.typing_started, ["om_unknown"])
        self.assertEqual(
            self.channel.typing_stopped,
            [("om_unknown", "reaction_om_unknown")],
        )
        self.assertIn("我没理解这句话", self.channel.replies[-1][1]["text"])

    def test_codex_dialogue_answers_natural_group_question(self) -> None:
        router = FakeDialogueRouter(
            [
                DialogueDecision(
                    action="reply",
                    reply="我会先形成草稿；只有你明确要求保存时才写入知识库。",
                )
            ]
        )
        bot = FeishuBotController(
            api=self.api,
            channel=self.channel,
            sessions=self.sessions,
            dialogue_router=router,
        )

        asyncio.run(
            bot.on_message(
                Message(
                    "你现在会怎么处理我刚才发的资料？",
                    message_id="om_natural_question",
                    chat_id="oc_dialogue",
                    chat_type="group",
                    mentioned_bot=True,
                )
            )
        )

        self.assertEqual(self.api.calls, [])
        self.assertIn("只有你明确要求保存", self.channel.replies[-1][1]["text"])
        self.assertEqual(router.calls[0]["context"]["channel"], "group")

    def test_codex_dialogue_can_route_natural_group_fact_to_draft(self) -> None:
        router = FakeDialogueRouter([DialogueDecision(action="record")])
        bot = FeishuBotController(
            api=self.api,
            channel=self.channel,
            sessions=self.sessions,
            dialogue_router=router,
        )

        asyncio.run(
            bot.on_message(
                Message(
                    "这个客户更喜欢微信沟通，下午联系比较好。",
                    message_id="om_natural_record",
                    chat_id="oc_dialogue_record",
                    sender_name="测试成员",
                    chat_type="group",
                    mentioned_bot=True,
                )
            )
        )

        session = self.sessions.get("oc_dialogue_record:group:root")
        self.assertEqual(len(session.group_buffer), 1)
        self.assertEqual(self.channel.reactions, [])
        self.assertEqual(session.codex_dialogue_session_id, "asess_test_dialogue")

    def test_codex_dialogue_resolves_business_language_module_and_finishes_save(self) -> None:
        router = FakeDialogueRouter(
            [
                DialogueDecision(
                    action="route_module",
                    document_type="system-configuration",
                )
            ]
        )
        bot = FeishuBotController(
            api=self.api,
            channel=self.channel,
            sessions=self.sessions,
            dialogue_router=router,
        )
        session = self.sessions.get("oc_module_language:group:root")
        session.merchant = {"name": "小熊鲜生", "id": "75288"}
        session.messages = [
            {
                "role": "user",
                "content": "小熊鲜生小程序绑定成功，包含对接参数和文档。",
                "source_ref": "feishu://message/module_source",
            }
        ]
        session.preview = {
            "merchant": dict(session.merchant),
            "profile_updates": [
                {
                    "field_path": "documents.integration_notes",
                    "value": "小程序已绑定，MID 75288。",
                    "source_ref": "feishu://message/module_source",
                    "replace_confirmed": False,
                    "state": "explicit",
                    "document_type": "document-archive",
                }
            ],
            "conflicts": [],
            "module_classifications": [
                {
                    "field_path": "documents.integration_notes",
                    "document_type": "document-archive",
                    "confidence": 0.55,
                    "reason": "需要确认业务主题",
                    "status": "needs_confirmation",
                }
            ],
            "classification_review_required": True,
            "save_readiness": "needs_confirmation",
            "next_question": "这些资料应归入哪个模块？",
        }
        session.group_followup_pending = True
        session.group_save_pending = True

        asyncio.run(
            bot.on_message(
                Message(
                    "这是我们自己的小程序",
                    message_id="om_module_language",
                    chat_id="oc_module_language",
                    chat_type="group",
                    mentioned_bot=True,
                )
            )
        )

        self.assertEqual(
            router.calls[0]["context"]["unresolved_module_classifications"],
            [
                {
                    "fact": "integration notes",
                    "suggested_document_type": "document-archive",
                    "reason": "需要确认业务主题",
                }
            ],
        )
        self.assertIn("/v1/profile/commit", [path for path, _ in self.api.calls])
        self.assertIn("已写入", self.channel.replies[-1][1]["text"])

    def test_codex_dialogue_session_continues_across_group_turns(self) -> None:
        router = FakeDialogueRouter(
            [
                DialogueDecision(action="reply", reply="第一轮回复"),
                DialogueDecision(action="reply", reply="第二轮回复"),
            ]
        )
        bot = FeishuBotController(
            api=self.api,
            channel=self.channel,
            sessions=self.sessions,
            dialogue_router=router,
        )
        base = {
            "chat_id": "oc_dialogue_continue",
            "chat_type": "group",
            "mentioned_bot": True,
        }

        asyncio.run(bot.on_message(Message("第一问", message_id="om_first", **base)))
        asyncio.run(bot.on_message(Message("那第二问呢", message_id="om_second", **base)))

        self.assertIsNone(router.calls[0]["session_id"])
        self.assertEqual(router.calls[1]["session_id"], "asess_test_dialogue")
        self.assertIn("第二轮回复", self.channel.replies[-1][1]["text"])

    def test_codex_dialogue_routes_explicit_natural_save_through_guarded_commit(self) -> None:
        router = FakeDialogueRouter([DialogueDecision(action="save")])
        bot = FeishuBotController(
            api=self.api,
            channel=self.channel,
            sessions=self.sessions,
            dialogue_router=router,
        )
        base = {
            "chat_id": "oc_dialogue_save",
            "chat_type": "group",
            "sender_name": "测试成员",
        }
        asyncio.run(
            bot.on_message(
                Message(
                    "青禾便当联系人是林经理",
                    message_id="om_save_fact",
                    **base,
                )
            )
        )
        asyncio.run(
            bot.on_message(
                Message(
                    "把前面整理好的文字和附件都写进知识库",
                    message_id="om_save_natural",
                    mentioned_bot=True,
                    **base,
                )
            )
        )

        self.assertIn("已写入", self.channel.replies[-1][1]["text"])
        self.assertIn("/v1/profile/commit", [path for path, _ in self.api.calls])

    def test_codex_dialogue_answers_direct_question_without_polluting_profile(self) -> None:
        router = FakeDialogueRouter(
            [DialogueDecision(action="reply", reply="目前还没有待保存的商户资料。")]
        )
        bot = FeishuBotController(
            api=self.api,
            channel=self.channel,
            sessions=self.sessions,
            dialogue_router=router,
        )

        asyncio.run(bot.on_message(Message("我刚才有保存成功吗？")))

        self.assertEqual(self.api.calls, [])
        self.assertIn("还没有待保存", self.channel.replies[-1][1]["text"])
        self.assertEqual(self.sessions.get("oc_test:ou_test:root").messages, [])

    def test_natural_retry_recovers_original_group_image_and_reanalyzes(self) -> None:
        original = Message(
            "青禾便当现场截图",
            message_id="om_retry_original",
            chat_id="oc_retry",
            sender_id="ou_retry",
            sender_name="测试成员",
            chat_type="group",
            content={"kind": "image", "image_key": "img_retry"},
        )
        channel = RetriableImageChannel(original)
        sessions = SessionStore()
        bot = FeishuBotController(
            api=self.api,  # type: ignore[arg-type]
            channel=channel,
            sessions=sessions,
        )

        asyncio.run(bot.on_message(original))
        session = sessions.get("oc_retry:group:root")
        self.assertEqual(len(session.media_failures), 1)

        asyncio.run(
            bot.on_message(
                Message(
                    "你再试一下",
                    message_id="om_retry_command",
                    chat_id="oc_retry",
                    sender_id="ou_retry",
                    sender_name="测试成员",
                    chat_type="group",
                    mentioned_bot=True,
                )
            )
        )

        self.assertEqual(session.media_failures, [])
        self.assertEqual(len(session.messages[0]["images"]), 1)
        self.assertIn("已重新读取 1 张图片", channel.replies[-1][1]["text"])
        self.assertIn("商户：青禾便当", channel.replies[-1][1]["text"])
        self.assertIn("/v1/profile/analyze", [path for path, _ in self.api.calls])

    def test_group_broken_merchant_image_blocks_save_until_user_chooses_text_only(self) -> None:
        channel = MissingImageChannel()
        sessions = SessionStore()
        bot = FeishuBotController(
            api=self.api,  # type: ignore[arg-type]
            channel=channel,
            sessions=sessions,
        )
        base = {
            "chat_id": "oc_failed_media",
            "chat_type": "group",
            "sender_name": "测试成员",
        }

        asyncio.run(
            bot.on_message(
                Message(
                    "青禾便当的联系人是林经理\n![image](img_missing)",
                    message_id="om_failed_media",
                    content={"kind": "post"},
                    resources=[{"type": "image", "file_key": "img_missing"}],
                    **base,
                )
            )
        )
        session = sessions.get("oc_failed_media:group:root")
        self.assertEqual(len(session.media_failures), 1)
        self.assertIn("这条图片消息未能完整读取", channel.replies[-1][1]["text"])

        asyncio.run(
            bot.on_message(
                Message(
                    "保存吧",
                    message_id="om_blocked_save",
                    mentioned_bot=True,
                    **base,
                )
            )
        )

        self.assertNotIn("/v1/profile/commit", [path for path, _ in self.api.calls])
        self.assertIn("尚未进入总结", channel.replies[-1][1]["text"])
        self.assertIn("om_failed_media"[-8:], channel.replies[-1][1]["text"])

        asyncio.run(
            bot.on_message(
                Message(
                    "仅保存文字",
                    message_id="om_text_only_save",
                    mentioned_bot=True,
                    **base,
                )
            )
        )

        self.assertIn("/v1/profile/commit", [path for path, _ in self.api.calls])
        self.assertEqual(session.media_failures, [])

    def test_successful_group_image_resend_clears_failed_media_state(self) -> None:
        channel = MissingImageChannel()
        sessions = SessionStore()
        bot = FeishuBotController(
            api=self.api,  # type: ignore[arg-type]
            channel=channel,
            sessions=sessions,
        )
        base = {
            "chat_id": "oc_resend",
            "chat_type": "group",
            "sender_name": "测试成员",
        }
        failed = Message(
            "![image](img_failed)",
            message_id="om_failed",
            content={"kind": "image", "image_key": "img_failed"},
            **base,
        )
        asyncio.run(bot.on_message(failed))
        session = sessions.get("oc_resend:group:root")
        self.assertEqual(len(session.media_failures), 1)

        bot.channel = FakeChannel()
        resent = Message(
            "青禾便当现场截图",
            message_id="om_resent",
            content={"kind": "image", "image_key": "img_resent"},
            **base,
        )
        asyncio.run(bot.on_message(resent))

        self.assertEqual(session.media_failures, [])

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
            raw={
                "event": {
                    "message": {
                        "content": json.dumps(
                            {
                                "file_key": "file_video",
                                "image_key": "img_video_poster",
                            }
                        )
                    }
                }
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
            session.group_name = "Little Amigos Fountain Gate CRM group"
            session.group_merchant_hint = "Little Amigos Fountain Gate"
            store.persist()

            restored = SessionStore(state_path=state_path).get("oc_group:group:root")

            self.assertTrue(restored.group_summary_enabled)
            self.assertEqual(restored.group_pending_count, 1)
            self.assertEqual(len(restored.group_buffer), 1)
            self.assertEqual(restored.group_name, "Little Amigos Fountain Gate CRM group")
            self.assertEqual(
                restored.group_merchant_hint,
                "Little Amigos Fountain Gate",
            )

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

    def test_group_link_request_without_repeat_mention_reads_current_pages(self) -> None:
        api = LiveDocumentApi()
        sessions = SessionStore()
        session = sessions.get("oc_live_links:group:root")
        session.merchant = {"name": "青禾便当", "id": "crm-1"}
        session.last_documents = [
            {
                "label": "旧链接",
                "url": "https://example.feishu.cn/wiki/stale",
            }
        ]
        bot = FeishuBotController(
            api=api,  # type: ignore[arg-type]
            channel=self.channel,
            sessions=sessions,
        )

        asyncio.run(
            bot.on_message(
                Message(
                    "给我这个商户的资料文档链接",
                    message_id="om_live_links",
                    chat_id="oc_live_links",
                    chat_type="group",
                    mentioned_bot=False,
                )
            )
        )

        self.assertEqual(api.calls[-1][0], "/v1/profile/read")
        self.assertEqual(api.calls[-1][1]["merchant_id"], "crm-1")
        reply = self.channel.replies[-1][1]["text"]
        self.assertIn("实时查询", reply)
        self.assertIn("商户资料目录", reply)
        self.assertIn("https://example.feishu.cn/wiki/contacts-live", reply)
        self.assertNotIn("/wiki/stale", reply)
        self.assertEqual(session.group_buffer, [])

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
        asyncio.run(
            bot.on_message(
                Message(
                    "总结吧",
                    message_id="om_refresh_summary",
                    chat_id="oc_refresh",
                    chat_type="group",
                    mentioned_bot=True,
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

    def test_group_unanchored_new_merchant_is_not_routed_to_active_merchant(self) -> None:
        api = UnanchoredSwitchApi()
        bot = FeishuBotController(
            api=api,  # type: ignore[arg-type]
            channel=self.channel,
            sessions=SessionStore(),
        )

        async def scenario() -> None:
            base = {"chat_id": "oc_guard", "chat_type": "group", "sender_name": "测试成员"}
            await bot.on_message(Message("青禾便当地址是 1 青禾路", message_id="om_one", **base))
            await bot.on_message(
                Message("保存吧", message_id="om_save_one", mentioned_bot=True, **base)
            )
            await bot.on_message(
                Message("他的地址是 123 Collins Street", message_id="om_ambiguous", **base)
            )
            await bot.on_message(
                Message("保存吧", message_id="om_save_two", mentioned_bot=True, **base)
            )

        asyncio.run(scenario())

        commits = [payload for path, payload in api.calls if path == "/v1/profile/commit"]
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["merchant"]["name"], "青禾便当")
        self.assertIn("切换商户前", self.channel.replies[-1][1]["text"])

    def test_group_mentioned_save_without_data_does_not_write(self) -> None:
        self.run_group("保存吧", mentioned=True)

        self.assertEqual(self.api.calls, [])
        self.assertIn("未识别到商户名称", self.channel.replies[-1][1]["text"])

    def test_group_merchant_name_and_save_in_one_message_is_analyzed_then_saved(self) -> None:
        self.run_group(
            "Little Amigos Fountain Gate 保存吧",
            mentioned=True,
            message_id="om_named_save",
        )

        paths = [path for path, _ in self.api.calls]
        self.assertEqual(
            paths,
            ["/v1/profile/analyze", "/v1/profile/read", "/v1/profile/commit"],
        )
        analyze_payload = self.api.calls[0][1]
        self.assertEqual(len(analyze_payload["messages"]), 1)
        self.assertIn(
            "Little Amigos Fountain Gate",
            analyze_payload["messages"][0]["content"],
        )
        self.assertNotIn("保存吧", analyze_payload["messages"][0]["content"])
        self.assertIn("已写入", self.channel.replies[-1][1]["text"])

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
                self.assertIn("静默记录", channel.replies[-1][1]["text"])

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
        self.assertIn("静默记录", self.channel.replies[-1][1]["text"])

    def test_group_threshold_stays_silent_until_manual_summary(self) -> None:
        self.run_group("开启自动总结", mentioned=True)
        replies_before_messages = len(self.channel.replies)
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

        self.assertEqual(len(self.channel.replies), replies_before_messages)
        self.assertEqual(self.api.calls, [])

        self.run_group("总结吧", mentioned=True, message_id="om_summary")

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

    def test_group_missing_merchant_can_be_supplied_with_save_in_same_message(self) -> None:
        api = MissingMerchantThenCorrectionApi()
        bot = FeishuBotController(
            api=api,  # type: ignore[arg-type]
            channel=self.channel,
            sessions=SessionStore(),
        )

        async def scenario() -> None:
            base = {
                "chat_id": "oc_combined_correction",
                "chat_type": "group",
                "sender_name": "测试成员",
            }
            await bot.on_message(Message("主要联系人是林经理", message_id="om_detail", **base))
            await bot.on_message(
                Message("保存吧", message_id="om_save_one", mentioned_bot=True, **base)
            )
            await bot.on_message(
                Message(
                    "Dandong咖啡厅 保存吧",
                    message_id="om_named_save",
                    mentioned_bot=True,
                    **base,
                )
            )

        asyncio.run(scenario())

        analyzes = [payload for path, payload in api.calls if path == "/v1/profile/analyze"]
        corrected_messages = analyzes[-1]["messages"]
        self.assertTrue(
            any("Dandong咖啡厅" in item["content"] for item in corrected_messages)
        )
        self.assertFalse(any("保存吧" in item["content"] for item in corrected_messages))
        commit = next(payload for path, payload in api.calls if path == "/v1/profile/commit")
        self.assertEqual(commit["merchant"]["name"], "Dandong咖啡厅")
        self.assertIn("已写入", self.channel.replies[-1][1]["text"])

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
            replies_before_pronoun = len(self.channel.replies)
            await bot.on_message(
                Message("他们有 POS 机业务员，规模不小", message_id="om_pronoun", **base)
            )
            self.assertEqual(len(self.channel.replies), replies_before_pronoun)
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

    def test_group_idle_time_stays_silent_until_manual_summary(self) -> None:
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
            replies_before_message = len(self.channel.replies)
            await bot.on_message(Message("青禾便当联系人是林经理", **base))
            await asyncio.sleep(0.03)
            self.assertEqual(len(self.channel.replies), replies_before_message)
            self.assertEqual(self.api.calls, [])
            await bot.on_message(
                Message("总结吧", mentioned_bot=True, message_id="om_summary", **base)
            )

        asyncio.run(scenario())

        self.assertIn("业务与联系人", self.channel.replies[-1][1]["text"])


if __name__ == "__main__":
    unittest.main()
