from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from .api_client import MerchantApiClient, MerchantApiError


LOGGER = logging.getLogger("merchant_profile_agent.feishu_bot")
SAVE_COMMANDS = {"结束并保存", "/save"}
DISCARD_COMMANDS = {"放弃本次对话", "/discard"}
HELP_COMMANDS = {"/help", "帮助"}


class ReplyChannel(Protocol):
    async def reply(self, message: Any, content: Any, opts: Any = None) -> Any: ...


@dataclass
class Session:
    conversation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    messages: list[dict[str, str]] = field(default_factory=list)
    current_profile: dict[str, Any] = field(default_factory=dict)
    merchant: dict[str, Any] | None = None
    preview: dict[str, Any] | None = None
    touched_at: float = field(default_factory=time.monotonic)


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
                "\n- 结束并保存：确认写入"
                "\n- /discard：放弃草稿",
            )
            return
        if text in DISCARD_COMMANDS:
            self.sessions.discard(key)
            await self._reply(message, "已放弃，未写入知识库。")
            return

        session = self.sessions.get(key)
        if text in SAVE_COMMANDS:
            await self._save(message, key, session, text)
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

    async def _save(
        self, message: Any, key: str, session: Session, confirmation: str
    ) -> None:
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
            await self._reply(
                message,
                f"已写入知识库，更新 {document_count} 个页面。{duplicate}",
            )
            self.sessions.discard(key)
            return
        await self._reply(message, "提交未完成，草稿已保留。")
