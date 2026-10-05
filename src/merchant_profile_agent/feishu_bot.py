from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from .api_client import MerchantApiClient, MerchantApiError


LOGGER = logging.getLogger("merchant_profile_agent.feishu_bot")
SAVE_COMMANDS = {"结束并保存", "/save"}
DISCARD_COMMANDS = {"放弃本次对话", "/discard"}
HELP_COMMANDS = {"/help", "帮助"}
NATURAL_SAVE_CONFIRMATIONS = {
    "结束并保存",
    "保存",
    "保存吧",
    "请保存",
    "帮我保存",
    "麻烦保存",
    "确认保存",
    "可以保存",
    "保存一下",
    "好的保存",
    "好的保存吧",
    "好的请保存",
    "没问题保存",
    "没问题保存吧",
    "没问题请保存",
    "写入",
    "写入吧",
    "请写入",
    "确认写入",
    "可以写入",
    "写入知识库",
    "保存到知识库",
    "提交",
    "提交吧",
    "请提交",
    "确认提交",
    "可以提交",
    "就这样保存",
    "以上内容保存",
    "把这些保存",
    "把这些写入",
    "存一下",
    "存进去",
    "存进去吧",
    "寫入",
    "寫入吧",
    "請寫入",
    "確認寫入",
    "可以寫入",
    "寫入知識庫",
}
DOCUMENT_LABELS = {
    "merchant-overview": "商户概况",
    "business-contacts": "业务与联系人",
    "product-menu": "产品与菜单",
    "system-configuration": "系统配置",
    "hardware-installation": "硬件与安装",
    "risk-compliance": "风险与合规",
    "commercial": "商务信息",
    "store-profile": "门店资料",
}


class ReplyChannel(Protocol):
    async def reply(self, message: Any, content: Any, opts: Any = None) -> Any: ...


@dataclass
class Session:
    conversation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    messages: list[dict[str, str]] = field(default_factory=list)
    current_profile: dict[str, Any] = field(default_factory=dict)
    merchant: dict[str, Any] | None = None
    preview: dict[str, Any] | None = None
    last_documents: list[dict[str, str]] = field(default_factory=list)
    touched_at: float = field(default_factory=time.monotonic)

    def reset_draft(self) -> None:
        self.conversation_id = str(uuid.uuid4())
        self.messages.clear()
        self.current_profile.clear()
        self.merchant = None
        self.preview = None
        self.touched_at = time.monotonic()


class SessionStore:
    def __init__(self, *, ttl_seconds: int = 14_400, maximum: int = 2_000) -> None:
        self.ttl_seconds = ttl_seconds
        self.maximum = maximum
        self._sessions: dict[str, Session] = {}

    def get(self, key: str) -> Session:
        self.prune()
        session = self._sessions.setdefault(key, Session())
        session.touched_at = time.monotonic()
        return session

    def discard(self, key: str) -> None:
        self._sessions.pop(key, None)

    def prune(self) -> None:
        now = time.monotonic()
        expired = [
            key
            for key, value in self._sessions.items()
            if now - value.touched_at > self.ttl_seconds
        ]
        for key in expired:
            self._sessions.pop(key, None)
        if len(self._sessions) > self.maximum:
            oldest = sorted(self._sessions, key=lambda key: self._sessions[key].touched_at)
            for key in oldest[: len(self._sessions) - self.maximum]:
                self._sessions.pop(key, None)


def _session_key(message: Any) -> str:
    conversation = getattr(message, "conversation", None)
    thread_id = getattr(conversation, "thread_id", None) or "root"
    return ":".join(
        [
            str(getattr(message, "chat_id", "unknown")),
            str(getattr(message, "sender_id", "unknown")),
            str(thread_id),
        ]
    )


def _source_ref(message: Any) -> str:
    return (
        f"feishu://chat/{getattr(message, 'chat_id', 'unknown')}"
        f"/message/{getattr(message, 'message_id', 'unknown')}"
    )


def _normalize_intent_text(text: str) -> str:
    return re.sub(r"[\s，,。.!！?？、:：;；\"'“”‘’]+", "", text).lower()


def _is_save_confirmation(text: str) -> bool:
    if text in SAVE_COMMANDS:
        return True
    return _normalize_intent_text(text) in NATURAL_SAVE_CONFIRMATIONS


def _is_link_request(text: str) -> bool:
    normalized = _normalize_intent_text(text)
    is_unambiguous_link = any(
        term in normalized for term in ("链接", "連結", "网址", "網址")
    )
    is_ambiguous_connection = any(term in normalized for term in ("连接", "連接"))
    if not is_unambiguous_link and not is_ambiguous_connection:
        return False
    has_request_context = any(
        cue in normalized
        for cue in ("给我", "給我", "发我", "發我", "刚才", "剛才", "刚刚", "剛剛", "页面", "頁面", "写入", "寫入", "保存", "知识库", "知識庫")
    )
    if is_ambiguous_connection and not is_unambiguous_link:
        return has_request_context
    return len(normalized) <= 12 or has_request_context


def _document_links(store_result: dict[str, Any]) -> list[dict[str, str]]:
    documents = store_result.get("documents")
    if not isinstance(documents, list):
        return []
    links: list[dict[str, str]] = []
    for item in documents:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        document_type = str(item.get("document_type") or "")
        links.append(
            {
                "label": DOCUMENT_LABELS.get(document_type, document_type or "知识库页面"),
                "url": url,
            }
        )
    return links


def _links_text(documents: list[dict[str, str]]) -> str:
    if not documents:
        return "暂无最近一次写入的页面链接。"
    return "最近写入：\n" + "\n".join(
        f"- {item['label']}：{item['url']}" for item in documents
    )


def _preview_text(result: dict[str, Any]) -> str:
    lines: list[str] = []
    updates = result.get("profile_updates")
    if isinstance(updates, list) and updates:
        lines.append("待保存：")
        for item in updates[:20]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('field_path')} → {item.get('value')}")
    conflicts = result.get("conflicts")
    if isinstance(conflicts, list) and conflicts:
        if lines:
            lines.append("")
        lines.append("有旧值冲突，请确认是否替换。")
    readiness = result.get("save_readiness")
    if readiness == "ready" and updates:
        lines.extend(["", "回复「结束并保存」确认；尚未写入知识库。"])
    elif result.get("next_question"):
        if lines:
            lines.append("")
        lines.extend([str(result["next_question"]), "尚未写入知识库。"])
    else:
        lines.append(str(result.get("reply") or "没有需要记录的信息。"))
    return "\n".join(lines)


class FeishuBotController:
    def __init__(
        self,
        *,
        api: MerchantApiClient,
        channel: ReplyChannel,
        sessions: SessionStore | None = None,
    ) -> None:
        self.api = api
        self.channel = channel
        self.sessions = sessions or SessionStore()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self.api.post, path, payload)

    async def _reply(self, message: Any, text: str) -> None:
        # Plain text prevents user-supplied values from becoming active markdown,
        # links, or mentions when a preview is echoed into a group chat.
        await self.channel.reply(message, {"text": text})

    async def on_message(self, message: Any) -> None:
        text = str(getattr(message, "body_text", "") or "").strip()
        if not text:
            return
        key = _session_key(message)
        if text in HELP_COMMANDS:
            await self._reply(
                message,
                "发送商户名称和要记录的资料。"
                "\n- 可说“没问题，保存吧”或“结束并保存”"
                "\n- /discard：放弃草稿",
            )
            return
        if text in DISCARD_COMMANDS:
            self.sessions.discard(key)
            await self._reply(message, "已放弃，未写入知识库。")
            return

        session = self.sessions.get(key)
        if _is_link_request(text):
            await self._reply(message, _links_text(session.last_documents))
            return
        if _is_save_confirmation(text):
            await self._save(message, session, "结束并保存")
            return

        session.messages.append(
            {
                "role": "user",
                "content": text,
                "source_ref": _source_ref(message),
            }
        )
        try:
            preview = await self._post(
                "/v1/profile/analyze",
                {"messages": session.messages, "current_profile": session.current_profile},
            )
            merchant = preview.get("merchant")
            if isinstance(merchant, dict) and merchant.get("name"):
                if session.merchant and session.merchant.get("name") != merchant.get("name"):
                    session.messages.pop()
                    await self._reply(
                        message,
                        f"当前草稿属于 {session.merchant.get('name')}。"
                        "如需切换商户，请先回复 /discard，再开始新的资料收集。",
                    )
                    return
                changed = not session.merchant or session.merchant.get("name") != merchant.get("name")
                session.merchant = {"name": merchant.get("name"), "id": merchant.get("id")}
                if changed:
                    read = await self._post(
                        "/v1/profile/read",
                        {
                            "merchant_name": session.merchant["name"],
                            "merchant_id": session.merchant.get("id"),
                        },
                    )
                    profile = read.get("profile")
                    session.current_profile = profile if isinstance(profile, dict) else {}
                    if session.current_profile.get("fields"):
                        preview = await self._post(
                            "/v1/profile/analyze",
                            {
                                "messages": session.messages,
                                "current_profile": session.current_profile,
                            },
                        )
            session.preview = preview
            reply_text = _preview_text(preview)
            session.messages.append(
                {
                    "role": "assistant",
                    "content": reply_text,
                    "source_ref": f"feishu-bot://reply/{getattr(message, 'message_id', 'unknown')}",
                }
            )
            await self._reply(message, reply_text)
        except MerchantApiError:
            LOGGER.exception("merchant API request failed")
            await self._reply(message, "服务暂不可用，未写入知识库。")

    async def _save(self, message: Any, session: Session, confirmation: str) -> None:
        preview = session.preview or {}
        merchant = session.merchant
        updates = preview.get("profile_updates")
        if (
            not merchant
            or preview.get("save_readiness") != "ready"
            or not isinstance(updates, list)
            or not updates
        ):
            await self._reply(message, "暂不能保存，请补充商户名称或解决冲突。")
            return
        try:
            result = await self._post(
                "/v1/profile/commit",
                {
                    "confirmation": confirmation,
                    "conversation_id": f"feishu:{session.conversation_id}",
                    "merchant": merchant,
                    "messages": session.messages,
                    "updates": updates,
                },
            )
        except MerchantApiError:
            LOGGER.exception("merchant profile commit failed")
            await self._reply(message, "保存失败，草稿已保留，请稍后重试。")
            return
        store_result = result.get("result") if isinstance(result.get("result"), dict) else {}
        if result.get("write_performed") or store_result.get("duplicate"):
            document_count = int(store_result.get("wiki_documents_updated") or 0)
            duplicate = "（此前已保存，本次未重复写入）" if store_result.get("duplicate") else ""
            documents = _document_links(store_result)
            if documents:
                session.last_documents = documents
            link_suffix = "\n" + _links_text(documents) if documents else ""
            await self._reply(
                message,
                f"已写入知识库，更新 {document_count} 个页面。{duplicate}{link_suffix}",
            )
            session.reset_draft()
            return
        await self._reply(message, "提交未完成，草稿已保留。")
