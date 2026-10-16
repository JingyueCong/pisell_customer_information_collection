from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from .api_client import MerchantApiClient, MerchantApiError
from .video import VideoEvidence, VideoProcessingError


LOGGER = logging.getLogger("merchant_profile_agent.feishu_bot")
MAX_IMAGES_PER_MESSAGE = 4
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 6 * 1024 * 1024
SAVE_COMMANDS = {"结束并保存", "/save"}
DISCARD_COMMANDS = {"放弃本次对话", "/discard"}
HELP_COMMANDS = {"/help", "帮助"}
GROUP_ENABLE_COMMANDS = {
    "开始记录",
    "开始收集",
    "开始群记录",
    "开启自动总结",
    "开启群总结",
    "启用自动总结",
}
GROUP_PAUSE_COMMANDS = {"暂停自动总结", "暂停群总结", "关闭自动总结"}
GROUP_SUMMARIZE_COMMANDS = {"立即总结", "现在总结", "总结本段"}
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
FIELD_LABELS = {
    "business.name": "商户名称",
    "business.type": "业务类型",
    "business.description": "简介",
    "contacts.primary.name": "主要联系人",
    "contacts.primary.role": "联系人职位",
    "contacts.primary.phone": "联系电话",
    "contacts.primary.email": "联系邮箱",
    "contacts.secondary.name": "备用联系人",
    "contacts.secondary.phone": "备用电话",
    "contacts.secondary.email": "备用邮箱",
    "menu.url": "菜单链接",
    "menu.link": "菜单链接",
    "content.url": "内容链接",
    "content.link": "内容链接",
    "stores.primary.name": "门店名称",
    "stores.primary.address": "门店地址",
    "stores.primary.phone": "门店电话",
}
FIELD_SUFFIX_LABELS = {
    "name": "名称",
    "type": "类型",
    "description": "说明",
    "role": "职位",
    "phone": "电话",
    "email": "邮箱",
    "address": "地址",
    "url": "链接",
    "link": "链接",
    "status": "状态",
    "hours": "营业时间",
    "notes": "备注",
}


class ReplyChannel(Protocol):
    async def reply(self, message: Any, content: Any, opts: Any = None) -> Any: ...

    async def download_resource(
        self,
        file_key: str,
        resource_type: str = "image",
        message_id: str | None = None,
    ) -> bytes | None: ...


class VideoProcessor(Protocol):
    def process(
        self,
        data: bytes,
        *,
        file_name: str,
        duration_ms: int | None = None,
    ) -> VideoEvidence: ...


@dataclass(frozen=True)
class VideoResource:
    file_key: str
    file_name: str
    duration_ms: int | None


@dataclass
class Session:
    conversation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    messages: list[dict[str, Any]] = field(default_factory=list)
    current_profile: dict[str, Any] = field(default_factory=dict)
    current_profile_loaded: bool = False
    merchant: dict[str, Any] | None = None
    crm_resolution: dict[str, Any] | None = None
    preview: dict[str, Any] | None = None
    last_documents: list[dict[str, str]] = field(default_factory=list)
    group_summary_enabled: bool = False
    group_buffer: list[dict[str, Any]] = field(default_factory=list)
    group_pending_count: int = 0
    group_generation: int = 0
    group_last_message: Any = None
    group_followup_pending: bool = False
    group_save_pending: bool = False
    group_idle_task: asyncio.Task[Any] | None = field(default=None, repr=False)
    touched_at: float = field(default_factory=time.time)

    def reset_draft(self, *, preserve_merchant: bool = False) -> None:
        merchant = self.merchant if preserve_merchant else None
        self.conversation_id = str(uuid.uuid4())
        self.messages.clear()
        self.current_profile.clear()
        self.current_profile_loaded = False
        self.merchant = merchant
        self.crm_resolution = None
        self.preview = None
        self.group_followup_pending = False
        self.group_save_pending = False
        self.touched_at = time.time()

    def clear_group_buffer(self) -> None:
        self.group_buffer.clear()
        self.group_pending_count = 0
        self.group_generation += 1
        self.group_last_message = None
        self.cancel_idle_task()

    def cancel_idle_task(self) -> None:
        task = self.group_idle_task
        self.group_idle_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()


class SessionStore:
    _SERIALIZED_FIELDS = (
        "conversation_id",
        "messages",
        "current_profile",
        "current_profile_loaded",
        "merchant",
        "crm_resolution",
        "preview",
        "last_documents",
        "group_summary_enabled",
        "group_buffer",
        "group_pending_count",
        "group_generation",
        "group_followup_pending",
        "group_save_pending",
        "touched_at",
    )

    def __init__(
        self,
        *,
        ttl_seconds: int = 14_400,
        maximum: int = 2_000,
        state_path: Path | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.maximum = maximum
        self.state_path = state_path
        self._sessions: dict[str, Session] = {}
        self._load()

    def get(self, key: str) -> Session:
        self.prune()
        session = self._sessions.setdefault(key, Session())
        session.touched_at = time.time()
        return session

    def discard(self, key: str) -> None:
        session = self._sessions.pop(key, None)
        if session is not None:
            session.cancel_idle_task()
            self.persist()

    def prune(self) -> None:
        now = time.time()
        expired = [
            key
            for key, value in self._sessions.items()
            if now - value.touched_at > self.ttl_seconds
        ]
        for key in expired:
            self.discard(key)
        if len(self._sessions) > self.maximum:
            oldest = sorted(self._sessions, key=lambda key: self._sessions[key].touched_at)
            for key in oldest[: len(self._sessions) - self.maximum]:
                session = self._sessions.pop(key, None)
                if session is not None:
                    session.cancel_idle_task()

    def persist(self) -> None:
        if self.state_path is None:
            return
        payload = {
            "version": 1,
            "sessions": {
                key: {
                    name: getattr(session, name)
                    for name in self._SERIALIZED_FIELDS
                }
                for key, session in self._sessions.items()
            },
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(self.state_path)

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            raw_sessions = payload.get("sessions", {})
            if not isinstance(raw_sessions, Mapping):
                raise ValueError("sessions must be an object")
            for key, raw in raw_sessions.items():
                if not isinstance(key, str) or not isinstance(raw, Mapping):
                    continue
                values = {
                    name: raw[name]
                    for name in self._SERIALIZED_FIELDS
                    if name in raw
                }
                self._sessions[key] = Session(**values)
            self.prune()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            LOGGER.exception("could not restore Feishu session state; starting empty")
            self._sessions.clear()


def _session_key(message: Any) -> str:
    conversation = getattr(message, "conversation", None)
    thread_id = getattr(conversation, "thread_id", None) or "root"
    if _is_group_message(message):
        return ":".join(
            [str(getattr(message, "chat_id", "unknown")), "group", str(thread_id)]
        )
    return ":".join(
        [
            str(getattr(message, "chat_id", "unknown")),
            str(getattr(message, "sender_id", "unknown")),
            str(thread_id),
        ]
    )


def _is_group_message(message: Any) -> bool:
    return str(getattr(message, "chat_type", "")).lower() in {"group", "topic"}


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


def _matches_command(text: str, commands: set[str]) -> bool:
    normalized = _normalize_intent_text(text)
    return normalized in commands


def _is_group_enable_intent(text: str) -> bool:
    normalized = _normalize_intent_text(text)
    if normalized in GROUP_ENABLE_COMMANDS:
        return True
    if not normalized or len(normalized) > 36:
        return False
    if any(
        term in normalized
        for term in ("不要", "不用", "不需要", "别", "无需", "停止", "暂停", "关闭", "取消")
    ):
        return False

    start_terms = ("开始", "开启", "启动", "启用", "打开", "进入")
    record_terms = ("记录", "收集", "整理", "总结", "归纳", "录入", "记一下", "记下来")
    request_terms = ("请", "帮我", "帮忙", "麻烦", "我们", "可以", "现在", "接下来", "从现在")
    context_terms = ("群", "群聊", "聊天", "对话", "商户", "客户", "资料", "信息", "内容", "后面", "接下来")

    has_start = any(term in normalized for term in start_terms)
    has_record = any(term in normalized for term in record_terms)
    has_request = any(term in normalized for term in request_terms)
    has_context = any(term in normalized for term in context_terms)
    starts_like_command = normalized.startswith(start_terms)

    if has_start and has_record and (starts_like_command or has_request):
        return True
    if has_record and has_request and has_context:
        return True
    return any(
        phrase in normalized
        for phrase in ("可以开始了", "现在开始吧", "准备好了开始吧", "开始工作吧")
    )


def _is_group_save_confirmation(text: str) -> bool:
    normalized = _normalize_intent_text(text)
    return normalized == "/save" or normalized in NATURAL_SAVE_CONFIRMATIONS


def _is_effective_group_message(text: str) -> bool:
    compact = _normalize_intent_text(text)
    return len(compact) >= 2 and bool(re.search(r"[0-9A-Za-z\u3400-\u9fff]{2}", compact))


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


def _field_label(field_path: Any) -> str:
    path = str(field_path or "")
    if path in FIELD_LABELS:
        return FIELD_LABELS[path]
    suffix = path.rsplit(".", 1)[-1]
    return FIELD_SUFFIX_LABELS.get(suffix, suffix.replace("_", " ") or "资料")


def _preview_text(result: dict[str, Any]) -> str:
    lines: list[str] = []
    updates = result.get("profile_updates")
    if isinstance(updates, list) and updates:
        lines.append("待保存：")
        for item in updates[:20]:
            if isinstance(item, dict):
                lines.append(f"- {_field_label(item.get('field_path'))}：{item.get('value')}")
    conflicts = result.get("conflicts")
    if isinstance(conflicts, list) and conflicts:
        if lines:
            lines.append("")
        lines.append("有旧值冲突，请确认是否替换。")
    readiness = result.get("save_readiness")
    if readiness == "ready" and updates:
        lines.extend(["", "回复“保存吧”写入。"])
    elif result.get("next_question"):
        if lines:
            lines.append("")
        lines.append(str(result["next_question"]))
    else:
        lines.append(str(result.get("reply") or "没有需要记录的信息。"))
    return "\n".join(lines)


def _crm_question(resolution: dict[str, Any], merchant_name: str) -> str | None:
    status = resolution.get("status")
    if status == "not_found":
        return (
            f"CRM 中未找到“{merchant_name}”。请补充准确客户名称或店铺 ID；"
            "我不会自动新建 CRM。"
        )
    if status != "ambiguous":
        return None
    candidates = resolution.get("candidates")
    labels: list[str] = []
    if isinstance(candidates, list):
        for item in candidates[:5]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "未命名")
            identifier = str(item.get("work_item_id") or "")
            labels.append(f"{name}（CRM {identifier}）" if identifier else name)
    suffix = "、".join(labels)
    if suffix:
        return f"CRM 中有多个候选：{suffix}。请补充店铺 ID 或更准确的客户名称。"
    return "CRM 中有多个候选，请补充店铺 ID 或更准确的客户名称。"


def _group_summary_text(result: dict[str, Any]) -> str:
    sections: list[str] = []

    merchant = result.get("merchant")
    if isinstance(merchant, dict) and merchant.get("name"):
        sections.append(f"商户：{merchant['name']}")

    def add_section(title: str, raw_items: Any) -> None:
        if not isinstance(raw_items, list):
            return
        items = [str(item).strip() for item in raw_items if str(item).strip()]
        if not items:
            return
        sections.append(title + "：\n" + "\n".join(f"- {item}" for item in items[:5]))

    add_section("阶段总结", result.get("summary_points"))
    add_section("已决定", result.get("decisions"))
    add_section("待办", result.get("action_items"))

    updates = result.get("profile_updates")
    if isinstance(updates, list) and updates:
        lines = [
            f"- {_field_label(item.get('field_path'))}：{item.get('value')}"
            for item in updates[:20]
            if isinstance(item, dict)
        ]
        if lines:
            sections.append("可写入知识库：\n" + "\n".join(lines))

    conflicts = result.get("conflicts")
    if isinstance(conflicts, list) and conflicts:
        lines = [
            f"- {_field_label(item.get('field_path'))}："
            f"{item.get('current_value')} → {item.get('proposed_value')}"
            for item in conflicts[:10]
            if isinstance(item, dict)
        ]
        if lines:
            sections.append("资料冲突：\n" + "\n".join(lines))

    gaps = result.get("information_gaps")
    add_section("待确认", gaps)
    if result.get("next_question"):
        sections.append("下一步：\n- " + str(result["next_question"]))

    crm = result.get("crm")
    if isinstance(crm, dict) and crm.get("status") == "unique":
        candidates = crm.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates else None
        if isinstance(candidate, dict):
            label = str(candidate.get("name") or "已找到客户")
            identifier = str(candidate.get("work_item_id") or "").strip()
            sections.append(
                f"CRM：已匹配 {label}" + (f"（{identifier}）" if identifier else "")
            )

    if not sections:
        sections.append("本段没有需要记录的商户信息。")
    if result.get("save_readiness") == "ready" and updates:
        sections.append("如需写入，请 @机器人 回复“保存吧”。")
    return "\n\n".join(sections)


class ImageInputError(RuntimeError):
    pass


def _resource_value(resource: Any, name: str) -> Any:
    if isinstance(resource, Mapping):
        return resource.get(name)
    return getattr(resource, name, None)


def _image_resource_keys(message: Any) -> list[str]:
    content = getattr(message, "content", None)
    content_kind = _resource_value(content, "kind")
    content_key = _resource_value(content, "image_key")
    if content_kind == "image" and isinstance(content_key, str) and content_key:
        return [content_key]

    raw_keys: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            image_key = value.get("image_key")
            if isinstance(image_key, str) and image_key and image_key not in raw_keys:
                raw_keys.append(image_key)
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)
        elif isinstance(value, str) and "image_key" in value:
            try:
                collect(json.loads(value))
            except (TypeError, ValueError, json.JSONDecodeError):
                return

    collect(getattr(message, "raw", None))
    if raw_keys:
        return raw_keys

    keys: list[str] = []
    for resource in getattr(message, "resources", []) or []:
        if _resource_value(resource, "type") != "image":
            continue
        file_key = _resource_value(resource, "file_key")
        if isinstance(file_key, str) and file_key and file_key not in keys:
            keys.append(file_key)
    return keys


def _without_image_placeholders(text: str) -> str:
    text = re.sub(r"!\[image\]\([^)]+\)", " ", text)
    text = re.sub(r"<image\b[^>]*?/?>", " ", text, flags=re.IGNORECASE)
    return re.sub(r"[ \t]+", " ", text).strip()


def _video_resource(message: Any) -> VideoResource | None:
    content = getattr(message, "content", None)
    kind = str(_resource_value(content, "kind") or "").lower()
    if kind == "media":
        file_key = _resource_value(content, "file_key")
        if isinstance(file_key, str) and file_key:
            return VideoResource(
                file_key=file_key,
                file_name=str(_resource_value(content, "file_name") or "video.mp4"),
                duration_ms=_positive_int(_resource_value(content, "duration_ms")),
            )

    for resource in getattr(message, "resources", []) or []:
        resource_type = str(_resource_value(resource, "type") or "").lower()
        file_name = str(
            _resource_value(resource, "file_name")
            or _resource_value(resource, "name")
            or "video.mp4"
        )
        is_video_file = Path(file_name).suffix.lower() in {".mp4", ".mov", ".m4v", ".webm"}
        if resource_type not in {"video", "media"} and not (
            resource_type == "file" and is_video_file
        ):
            continue
        file_key = _resource_value(resource, "file_key")
        if isinstance(file_key, str) and file_key:
            return VideoResource(
                file_key=file_key,
                file_name=file_name,
                duration_ms=_positive_int(_resource_value(resource, "duration_ms")),
            )
    return None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _without_video_placeholders(text: str) -> str:
    text = re.sub(r"<(?:media|video)\b[^>]*?/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"&lt;(?:media|video)\b.*?/?&gt;",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\[(?:video|视频)\](?:\([^)]*\))?", " ", text, flags=re.IGNORECASE)
    return re.sub(r"[ \t]+", " ", text).strip()


def _image_mime_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _image_data_url_bytes(value: Any) -> int:
    if not isinstance(value, str) or ";base64," not in value:
        return 0
    try:
        return len(base64.b64decode(value.split(",", 1)[1], validate=True))
    except (ValueError, TypeError):
        return 0


def _messages_for_analysis(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy messages while enforcing the API's conversation-wide image budget.

    Group buffers can contain several screenshot or video messages. Allocate the
    four available image slots round-robin from newest to oldest so one older
    media message cannot make the whole segment fail validation.
    """
    copied = [
        {key: value for key, value in message.items() if key != "images"}
        for message in messages
    ]
    selected: dict[int, list[str]] = {}
    image_count = 0
    image_bytes = 0
    maximum_ordinal = max(
        (
            len(message.get("images", []))
            for message in messages
            if isinstance(message.get("images"), list)
        ),
        default=0,
    )
    for ordinal in range(maximum_ordinal):
        for index in range(len(messages) - 1, -1, -1):
            raw_images = messages[index].get("images", [])
            if not isinstance(raw_images, list) or ordinal >= len(raw_images):
                continue
            image = raw_images[ordinal]
            size = _image_data_url_bytes(image)
            if size <= 0 or size > MAX_IMAGE_BYTES:
                continue
            if image_bytes + size > MAX_TOTAL_IMAGE_BYTES:
                continue
            selected.setdefault(index, []).append(image)
            image_count += 1
            image_bytes += size
            if image_count >= MAX_IMAGES_PER_MESSAGE:
                break
        if image_count >= MAX_IMAGES_PER_MESSAGE:
            break
    for index, images in selected.items():
        copied[index]["images"] = images
    return copied


class MerchantSwitchError(RuntimeError):
    pass


class FeishuBotController:
    def __init__(
        self,
        *,
        api: MerchantApiClient,
        channel: ReplyChannel,
        sessions: SessionStore | None = None,
        group_message_threshold: int = 20,
        group_idle_seconds: int = 600,
        group_idle_min_messages: int = 1,
        video_processor: VideoProcessor | None = None,
        maximum_video_bytes: int = 25 * 1024 * 1024,
        maximum_video_duration_seconds: int = 300,
    ) -> None:
        self.api = api
        self.channel = channel
        self.sessions = sessions or SessionStore()
        self.group_message_threshold = group_message_threshold
        self.group_idle_seconds = group_idle_seconds
        self.group_idle_min_messages = group_idle_min_messages
        self.video_processor = video_processor
        self.maximum_video_bytes = maximum_video_bytes
        self.maximum_video_duration_seconds = maximum_video_duration_seconds

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self.api.post, path, payload)

    async def _reply(self, message: Any, text: str) -> None:
        # Plain text prevents user-supplied values from becoming active markdown,
        # links, or mentions when a preview is echoed into a group chat.
        await self.channel.reply(message, {"text": text})

    async def _download_images(self, message: Any) -> list[str]:
        file_keys = _image_resource_keys(message)
        if not file_keys:
            return []
        if len(file_keys) > MAX_IMAGES_PER_MESSAGE:
            raise ImageInputError(f"每条消息最多支持 {MAX_IMAGES_PER_MESSAGE} 张图片。")
        image_data_urls: list[str] = []
        total_bytes = 0
        for file_key in file_keys:
            try:
                data = await self.channel.download_resource(
                    file_key,
                    resource_type="image",
                    message_id=str(getattr(message, "message_id", "") or "") or None,
                )
            except Exception as exc:
                LOGGER.warning(
                    "image resource download raised for message=%s key=%s: %s",
                    str(getattr(message, "message_id", "") or "unknown"),
                    file_key,
                    exc,
                )
                raise ImageInputError("图片下载失败，请重新发送截图。") from exc
            if not isinstance(data, bytes) or not data:
                LOGGER.warning(
                    "image resource download returned no bytes for message=%s key=%s",
                    str(getattr(message, "message_id", "") or "unknown"),
                    file_key,
                )
                raise ImageInputError("图片下载失败，请重新发送截图。")
            if len(data) > MAX_IMAGE_BYTES:
                raise ImageInputError("单张图片不能超过 4 MB。")
            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_IMAGE_BYTES:
                raise ImageInputError("本条消息中的图片总大小不能超过 6 MB。")
            mime_type = _image_mime_type(data)
            if mime_type is None:
                raise ImageInputError("仅支持 PNG、JPEG 或 WEBP 图片。")
            encoded = base64.b64encode(data).decode("ascii")
            image_data_urls.append(f"data:{mime_type};base64,{encoded}")
        return image_data_urls

    async def _download_video(self, message: Any) -> VideoEvidence | None:
        resource = _video_resource(message)
        if resource is None:
            return None
        if self.video_processor is None:
            raise VideoProcessingError("当前服务尚未启用视频处理。")
        if (
            resource.duration_ms is not None
            and resource.duration_ms > self.maximum_video_duration_seconds * 1_000
        ):
            raise VideoProcessingError(
                f"视频不能超过 {self.maximum_video_duration_seconds // 60} 分钟。"
            )
        try:
            data = await self.channel.download_resource(
                resource.file_key,
                resource_type="file",
                message_id=str(getattr(message, "message_id", "") or "") or None,
            )
        except Exception as exc:
            LOGGER.warning(
                "video resource download raised for message=%s key=%s: %s",
                str(getattr(message, "message_id", "") or "unknown"),
                resource.file_key,
                exc,
            )
            raise VideoProcessingError("视频下载失败，请重新发送。") from exc
        if not isinstance(data, bytes) or not data:
            raise VideoProcessingError("视频下载失败，请重新发送。")
        if len(data) > self.maximum_video_bytes:
            raise VideoProcessingError(
                f"视频不能超过 {self.maximum_video_bytes // (1024 * 1024)} MB。"
            )
        try:
            return await asyncio.to_thread(
                self.video_processor.process,
                data,
                file_name=resource.file_name,
                duration_ms=resource.duration_ms,
            )
        except VideoProcessingError:
            raise
        except Exception as exc:
            LOGGER.exception("unexpected video processing failure")
            raise VideoProcessingError("服务器暂时无法处理视频，请稍后重试。") from exc

    async def on_message(self, message: Any) -> None:
        raw_text = str(getattr(message, "body_text", "") or "").strip()
        has_video = _video_resource(message) is not None
        image_keys = _image_resource_keys(message)
        if not raw_text and not image_keys and not has_video:
            return
        try:
            if _is_group_message(message) and (image_keys or has_video):
                group_session = self.sessions.get(_session_key(message))
                if not group_session.group_summary_enabled:
                    if bool(getattr(message, "mentioned_bot", False)):
                        await self._reply(
                            message,
                            "本群尚未开始记录。请先 @机器人 回复“开始记录”。",
                        )
                    return
            try:
                images = await self._download_images(message)
                video = await self._download_video(message) if has_video else None
            except ImageInputError as exc:
                await self._reply(message, str(exc))
                return
            except VideoProcessingError as exc:
                LOGGER.warning(
                    "video processing rejected message=%s: %s",
                    str(getattr(message, "message_id", "") or "unknown"),
                    exc,
                )
                await self._reply(message, f"视频处理失败：{exc}")
                return
            text = _without_video_placeholders(_without_image_placeholders(raw_text))
            if video is not None:
                if video.transcript:
                    text = "\n".join(
                        part
                        for part in (
                            text,
                            "视频语音转写（请只提取明确事实）：\n" + video.transcript,
                        )
                        if part
                    )
                if video.frames:
                    images.extend(video.frames)
                    images = images[:MAX_IMAGES_PER_MESSAGE]
                    text = "\n".join(
                        part
                        for part in (
                            text,
                            "已附视频关键帧，请结合画面中明确可见的信息进行提取。",
                        )
                        if part
                    )
            if images and not text:
                text = "用户上传了截图，请提取其中明确可见且适合客户档案的有用信息。"
            if not text:
                return
            if _is_group_message(message):
                await self._on_group_message(message, text, images)
                return
            await self._on_direct_message(message, text, images)
        finally:
            try:
                self.sessions.persist()
            except OSError:
                LOGGER.exception("could not persist Feishu session state")

    async def _on_direct_message(
        self, message: Any, text: str, images: list[str]
    ) -> None:
        key = _session_key(message)
        if text in HELP_COMMANDS:
            await self._reply(
                message,
                "发送商户名称和要记录的资料。"
                "\n- 可直接发送截图，我会提取其中明确可见的有用信息"
                "\n- 可发送 5 分钟、25 MB 以内的视频，我会转写语音并读取关键帧"
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

        user_message: dict[str, Any] = {
            "role": "user",
            "content": text,
            "source_ref": _source_ref(message),
        }
        if images:
            user_message["images"] = images
        session.messages.append(user_message)
        # Assistant previews are not needed by the extractor. Keeping only user
        # turns doubles useful conversation capacity and avoids the API's
        # 40-message limit during longer staff tests.
        if len(session.messages) > 40:
            session.messages = session.messages[-40:]
        try:
            preview = await self._analyze_session(session)
            reply_text = _preview_text(preview)
            await self._reply(message, reply_text)
        except MerchantSwitchError:
            session.messages.pop()
            await self._reply(
                message,
                f"当前草稿属于 {session.merchant.get('name') if session.merchant else '其他商户'}。"
                "如需切换，请先回复 /discard。",
            )
        except MerchantApiError:
            LOGGER.exception("merchant API request failed")
            await self._reply(message, "服务暂不可用，未写入知识库。")

    async def _analyze_session(self, session: Session) -> dict[str, Any]:
        if session.merchant and not session.current_profile_loaded:
            read = await self._post(
                "/v1/profile/read",
                {
                    "merchant_name": session.merchant["name"],
                    "merchant_id": session.merchant.get("id"),
                },
            )
            profile = read.get("profile")
            session.current_profile = profile if isinstance(profile, dict) else {}
            self._apply_crm_resolution(session, read.get("crm"))
            session.current_profile_loaded = True
        preview = await self._post(
            "/v1/profile/analyze",
            {
                "messages": _messages_for_analysis(session.messages),
                "current_profile": session.current_profile,
            },
        )
        merchant = preview.get("merchant")
        if isinstance(merchant, dict) and merchant.get("name"):
            if session.merchant and session.merchant.get("name") != merchant.get("name"):
                raise MerchantSwitchError
            previous_name = session.merchant.get("name") if session.merchant else None
            previous_id = session.merchant.get("id") if session.merchant else None
            proposed_id = merchant.get("id") or previous_id
            changed = previous_name != merchant.get("name") or previous_id != proposed_id
            session.merchant = {"name": merchant.get("name"), "id": proposed_id}
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
                self._apply_crm_resolution(session, read.get("crm"))
                session.current_profile_loaded = True
                if session.current_profile.get("fields"):
                    preview = await self._post(
                        "/v1/profile/analyze",
                        {
                            "messages": _messages_for_analysis(session.messages),
                            "current_profile": session.current_profile,
                        },
                    )
        if session.merchant:
            preview["merchant"] = dict(session.merchant)
        self._apply_crm_gate(session, preview)
        session.preview = preview
        return preview

    @staticmethod
    def _apply_crm_resolution(session: Session, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        session.crm_resolution = raw
        candidates = raw.get("candidates")
        if raw.get("status") != "unique" or not isinstance(candidates, list) or not candidates:
            return
        candidate = candidates[0]
        if not isinstance(candidate, dict) or not candidate.get("work_item_id"):
            return
        if session.merchant:
            session.merchant["id"] = str(candidate["work_item_id"])

    @staticmethod
    def _apply_crm_gate(session: Session, preview: dict[str, Any]) -> None:
        resolution = session.crm_resolution
        merchant = session.merchant
        if not isinstance(resolution, dict) or not merchant:
            return
        preview["crm"] = resolution
        if resolution.get("status") == "unique":
            preview["merchant"] = dict(merchant)
            return
        question = _crm_question(resolution, str(merchant.get("name") or "该商户"))
        if question:
            preview["save_readiness"] = "missing_info"
            preview["next_question"] = question
            gaps = preview.get("information_gaps")
            if not isinstance(gaps, list):
                gaps = []
            if "CRM 客户唯一匹配" not in gaps:
                gaps.append("CRM 客户唯一匹配")
            preview["information_gaps"] = gaps

    async def _on_group_message(
        self, message: Any, text: str, images: list[str]
    ) -> None:
        key = _session_key(message)
        session = self.sessions.get(key)
        mentioned = bool(getattr(message, "mentioned_bot", False))

        if mentioned and _is_group_enable_intent(text):
            session.group_summary_enabled = True
            await self._reply(
                message,
                "自动总结已开启：20 条有效消息，或有新资料后安静 10 分钟。"
                "群消息会发送给 AI 生成摘要；不会自动写知识库。",
            )
            return
        if not session.group_summary_enabled:
            if mentioned and _matches_command(text, HELP_COMMANDS):
                await self._reply(message, "请 @机器人 回复“开始记录”。")
            elif mentioned:
                await self._reply(
                    message,
                    "本群尚未开始记录。请先 @机器人 回复“开始记录”。",
                )
            return
        if mentioned and _matches_command(text, GROUP_PAUSE_COMMANDS):
            session.group_summary_enabled = False
            session.clear_group_buffer()
            await self._reply(message, "自动总结已暂停。")
            return
        if mentioned and _matches_command(text, GROUP_SUMMARIZE_COMMANDS):
            if session.group_followup_pending:
                save_pending = session.group_save_pending
                analyzed = await self._summarize_group_followup(
                    session,
                    message,
                    reply_on_success=not save_pending,
                )
                if analyzed and save_pending and not session.group_followup_pending:
                    await self._save(message, session, "结束并保存")
            else:
                await self._summarize_group(session, message)
            return
        if mentioned and _matches_command(text, HELP_COMMANDS):
            await self._reply(
                message,
                "群总结已开启。可用：立即总结、暂停自动总结、保存吧、/discard。",
            )
            return
        if mentioned and _matches_command(text, DISCARD_COMMANDS):
            session.reset_draft()
            session.clear_group_buffer()
            await self._reply(message, "已放弃当前摘要草稿，自动总结仍开启。")
            return
        if mentioned and _is_link_request(text):
            await self._reply(message, _links_text(session.last_documents))
            return
        if mentioned and _is_group_save_confirmation(text):
            if session.group_buffer:
                if session.group_followup_pending:
                    ready = await self._summarize_group_followup(
                        session, message, reply_on_success=False
                    )
                else:
                    ready = await self._summarize_group(
                        session, message, reply_on_success=False
                    )
                if not ready:
                    return
            await self._save(message, session, "结束并保存")
            return
        if not mentioned and (
            _is_group_save_confirmation(text)
            or _matches_command(text, GROUP_ENABLE_COMMANDS)
            or _matches_command(text, GROUP_PAUSE_COMMANDS)
            or _matches_command(text, GROUP_SUMMARIZE_COMMANDS)
            or _matches_command(text, DISCARD_COMMANDS)
        ):
            return
        if not _is_effective_group_message(text):
            return

        sender_name = str(getattr(message, "sender_name", "") or "").strip()
        if not sender_name:
            sender_id = str(getattr(message, "sender_id", "") or "").strip()
            if sender_id:
                sender_name = f"成员[{sender_id[-8:]}]"
        content = f"{sender_name}：{text}" if sender_name else text
        user_message: dict[str, Any] = {
            "role": "user",
            "content": content,
            "source_ref": _source_ref(message),
        }
        if images:
            user_message["images"] = images
        session.group_buffer.append(user_message)
        session.group_pending_count += 1
        session.group_generation += 1
        session.group_last_message = message
        if mentioned:
            session.cancel_idle_task()
            if session.group_followup_pending:
                save_pending = session.group_save_pending
                analyzed = await self._summarize_group_followup(
                    session,
                    message,
                    reply_on_success=not save_pending,
                )
                if analyzed and save_pending and not session.group_followup_pending:
                    await self._save(message, session, "结束并保存")
            else:
                await self._summarize_group(session, message)
            return
        if session.group_followup_pending:
            # Keep nearby, unmentioned corrections so the next explicit @bot
            # follow-up can reconcile them with the existing unsaved summary.
            return
        if session.group_pending_count >= self.group_message_threshold:
            session.cancel_idle_task()
            await self._summarize_group(session, message)
            return
        self._schedule_idle_summary(session)

    def _schedule_idle_summary(self, session: Session) -> None:
        session.cancel_idle_task()
        generation = session.group_generation
        session.group_idle_task = asyncio.create_task(
            self._summarize_after_idle(session, generation)
        )

    async def _summarize_after_idle(self, session: Session, generation: int) -> None:
        try:
            await asyncio.sleep(self.group_idle_seconds)
            if (
                session.group_summary_enabled
                and session.group_generation == generation
                and session.group_pending_count >= self.group_idle_min_messages
                and session.group_last_message is not None
            ):
                await self._summarize_group(session, session.group_last_message)
        except asyncio.CancelledError:
            return
        except Exception:
            LOGGER.exception("automatic group summary failed")
        finally:
            try:
                self.sessions.persist()
            except OSError:
                LOGGER.exception("could not persist Feishu session state after idle summary")

    async def _summarize_group(
        self,
        session: Session,
        message: Any,
        *,
        reply_on_success: bool = True,
    ) -> bool:
        if not session.group_buffer:
            await self._reply(message, "暂无新的群消息可总结。")
            return False
        segment = list(session.group_buffer)
        session.messages = segment
        try:
            preview = await self._analyze_session(session)
        except MerchantSwitchError:
            await self._reply(
                message,
                f"本群摘要已绑定 {session.merchant.get('name') if session.merchant else '其他商户'}；"
                "切换商户前请 @机器人 回复 /discard。",
            )
            return False
        except MerchantApiError:
            LOGGER.exception("group summary API request failed")
            await self._reply(message, "本段总结失败，消息仍保留，将稍后重试。")
            return False
        session.clear_group_buffer()
        if reply_on_success or session.group_followup_pending:
            await self._reply(message, _group_summary_text(preview))
        return True

    async def _summarize_group_followup(
        self,
        session: Session,
        message: Any,
        *,
        reply_on_success: bool = True,
    ) -> bool:
        if not session.group_buffer:
            await self._reply(message, "请 @机器人 补充商户名称或需要确认的内容。")
            return False
        previous_messages = list(session.messages)
        session.messages = [*previous_messages, *session.group_buffer]
        try:
            preview = await self._analyze_session(session)
        except MerchantSwitchError:
            session.messages = previous_messages
            await self._reply(
                message,
                f"当前草稿属于 {session.merchant.get('name') if session.merchant else '其他商户'}；"
                "如需切换，请先 @机器人 回复 /discard。",
            )
            return False
        except MerchantApiError:
            session.messages = previous_messages
            LOGGER.exception("group follow-up analysis failed")
            await self._reply(message, "补充信息处理失败，消息仍保留，请稍后重试。")
            return False
        session.clear_group_buffer()
        updates = preview.get("profile_updates")
        session.group_followup_pending = not (
            session.merchant
            and preview.get("save_readiness") == "ready"
            and isinstance(updates, list)
            and updates
        )
        if reply_on_success or session.group_followup_pending:
            await self._reply(message, _group_summary_text(preview))
        return True

    async def _save(self, message: Any, session: Session, confirmation: str) -> None:
        resolution = session.crm_resolution
        preview_updates = (session.preview or {}).get("profile_updates")
        unresolved_crm = (
            session.merchant
            and isinstance(resolution, dict)
            and resolution.get("status") in {"not_found", "ambiguous"}
        )
        if session.messages and (unresolved_crm or not preview_updates):
            # CRM data and matching rules may change after the preview. Refresh
            # unresolved matches and empty extraction results on an explicit save
            # attempt instead of trapping the user behind stale analysis.
            if unresolved_crm:
                session.current_profile_loaded = False
            try:
                await self._analyze_session(session)
            except MerchantSwitchError:
                await self._reply(
                    message,
                    "重新分析识别到了不同商户，暂未保存；请先确认商户名称。",
                )
                return
            except MerchantApiError:
                LOGGER.exception("CRM refresh before save failed")
                await self._reply(message, "CRM 查询暂不可用，草稿已保留，请稍后重试。")
                return
        preview = session.preview or {}
        merchant = session.merchant
        updates = preview.get("profile_updates")
        if (
            not merchant
            or preview.get("save_readiness") != "ready"
            or not isinstance(updates, list)
            or not updates
        ):
            if _is_group_message(message):
                session.group_followup_pending = True
                session.group_save_pending = True
                conflicts = preview.get("conflicts")
                if isinstance(conflicts, list) and conflicts:
                    lines = [
                        f"- {_field_label(item.get('field_path'))}："
                        f"{item.get('current_value')} → {item.get('proposed_value')}"
                        for item in conflicts[:10]
                        if isinstance(item, dict)
                    ]
                    await self._reply(
                        message,
                        "检测到知识库现有资料不同，暂未写入：\n"
                        + "\n".join(lines)
                        + "\n如需用新值替换，请 @机器人 回复“确认替换”。",
                    )
                elif not merchant:
                    await self._reply(
                        message,
                        "未识别到商户名称，暂未写入。请 @机器人 补充商户名称。",
                    )
                elif not isinstance(updates, list) or not updates:
                    session.group_followup_pending = False
                    session.group_save_pending = False
                    await self._reply(message, "没有需要写入的新资料。")
                else:
                    question = str(
                        preview.get("next_question") or "请补充缺少的资料。"
                    )
                    await self._reply(
                        message,
                        f"暂未写入：{question}\n请 @机器人 补充，我会继续处理。",
                    )
            else:
                question = str(
                    preview.get("next_question") or "请补充商户名称或解决冲突。"
                )
                await self._reply(message, f"暂不能保存：{question}")
            return
        try:
            result = await self._post(
                "/v1/profile/commit",
                {
                    "confirmation": confirmation,
                    "conversation_id": f"feishu:{session.conversation_id}",
                    "merchant": merchant,
                    # Image bytes are used only for the preview analysis. The
                    # durable event keeps the Feishu source reference, not a
                    # base64 copy of the screenshot.
                    "messages": [
                        {
                            key: item[key]
                            for key in ("role", "content", "source_ref")
                            if key in item
                        }
                        for item in session.messages
                    ],
                    "updates": updates,
                },
            )
        except MerchantApiError as exc:
            LOGGER.exception("merchant profile commit failed")
            if exc.code == "crm_resolution_required":
                await self._reply(
                    message,
                    "保存前 CRM 身份校验未通过，暂未写入；草稿已保留。"
                    "请重新发送“保存”，我会再次核对同一客户。",
                )
            else:
                await self._reply(message, "保存失败，草稿已保留，请稍后重试。")
            return
        store_result = result.get("result") if isinstance(result.get("result"), dict) else {}
        if result.get("write_performed") or store_result.get("duplicate"):
            duplicate = "（此前已保存，本次未重复写入）" if store_result.get("duplicate") else ""
            documents = _document_links(store_result)
            if documents:
                session.last_documents = documents
            if documents:
                reply_text = "已写入：\n" + "\n".join(
                    f"- {item['label']}：{item['url']}" for item in documents
                )
            else:
                reply_text = f"已写入知识库。{duplicate}"
            await self._reply(
                message,
                reply_text,
            )
            session.reset_draft(preserve_merchant=_is_group_message(message))
            return
        await self._reply(message, "提交未完成，草稿已保留。")
