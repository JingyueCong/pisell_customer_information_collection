from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import mimetypes
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .api_client import MerchantApiClient, MerchantApiError
from .codex_dialogue import (
    CodexDialogueError,
    DialogueDecision,
    DialogueRouter,
)
from .video import VideoEvidence, VideoProcessingError
from .media_archive import (
    DownloadedMedia,
    FeishuDriveArchiver,
    MediaArchiveError,
    MediaStagingStore,
)
from .validation import document_for_field, reroute_field_path, validate_update_set


LOGGER = logging.getLogger("merchant_profile_agent.feishu_bot")
MAX_IMAGES_PER_MESSAGE = 40
MAX_IMAGES_PER_MODEL_BATCH = 8
MAX_IMAGES_PER_ANALYSIS = 40
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES_PER_MODEL_BATCH = 6 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES_PER_ANALYSIS = 30 * 1024 * 1024
MAX_ARCHIVE_FILE_BYTES = 25 * 1024 * 1024
RESOURCE_DOWNLOAD_RETRY_DELAYS_SECONDS = (0.25, 1.0)
ARCHIVE_FILE_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".csv",
    ".txt",
    ".rtf",
    ".zip",
}
SAVE_COMMANDS = {"结束并保存", "/save"}
DISCARD_COMMANDS = {"放弃本次对话", "/discard"}
TEXT_ONLY_SAVE_COMMANDS = {
    "仅保存文字",
    "只保存文字",
    "忽略图片保存",
    "忽略附件保存",
}
HELP_COMMANDS = {"/help", "帮助"}
GROUP_ENABLE_COMMANDS = {
    "开始记录",
    "继续记录",
    "恢复记录",
    "开始收集",
    "开始群记录",
    "开启自动总结",
    "开启群总结",
    "启用自动总结",
}
GROUP_PAUSE_COMMANDS = {
    "暂停自动总结",
    "暂停群总结",
    "关闭自动总结",
    "暂停记录",
    "停止记录",
    "不要记录",
    "不要开始记录",
}
GROUP_SUMMARIZE_COMMANDS = {"立即总结", "现在总结", "总结本段", "总结吧", "帮我总结"}
RETRY_COMMANDS = {
    "重试",
    "重试一下",
    "再试",
    "再试一下",
    "你再试一下",
    "麻烦再试一下",
    "重新试一下",
    "重新读取",
    "重新读取一下",
    "再读取一次",
    "重新分析",
    "重新分析一下",
}
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
    "menu-content": "菜单与内容",
    "system-configuration": "系统配置与注意事项",
    "hardware-installation": "硬件与安装",
    "risk-compliance": "风控与合规",
    "commercial-information": "商务信息",
    "store-profile": "门店档案",
    "project-progress": "项目进度一览表",
    "data-collection": "资料收集",
    "package-commercial": "套餐与商务",
    "hardware-records": "硬件设备记录",
    "card-terminal": "刷卡机设备",
    "special-matters": "特殊事项",
    "menu-collection": "菜单收集",
    "document-archive": "资料归档",
    "communication-profile": "沟通特点",
    "content-settings": "内容设置注意事项",
    "credential-records": "账号与密码",
}
MEDIA_FIELD_PREFIXES = {
    "business-contacts": "business",
    "menu-content": "menu",
    "system-configuration": "system",
    "hardware-installation": "hardware",
    "risk-compliance": "risk",
    "commercial-information": "commercial",
    "store-profile": "stores",
    "project-progress": "project",
    "data-collection": "collection",
    "package-commercial": "package",
    "hardware-records": "hardware_record",
    "card-terminal": "card_terminal",
    "special-matters": "special",
    "menu-collection": "menu_collection",
    "document-archive": "documents",
    "communication-profile": "communication",
    "content-settings": "content_setup",
    "credential-records": "credentials",
}
MEDIA_ROUTE_PRIORITY = tuple(MEDIA_FIELD_PREFIXES)
FIELD_LABELS = {
    "project.feishu_link": "飞书项目链接",
    "project.status": "项目进度",
    "project.notes": "进度说明",
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
    "collection.merchant_form_url": "商家信息表",
    "collection.archive_url": "资料压缩包",
    "collection.basic_profile": "商家基础情况",
    "commercial.package_type": "套餐类型",
    "package.type": "套餐类型",
    "billing.payment_method": "付款方式",
    "billing.deduction_status": "扣款情况",
    "hardware_record.devices": "设备清单",
    "hardware_record.commercial_terms": "设备商务情况",
    "hardware_record.configuration_record_url": "硬件配置记录表",
    "card_terminal.requested_count": "刷卡机申请点数",
    "card_terminal.surcharge": "Surcharge",
    "card_terminal.applicant_company": "申请公司",
    "risk.materials": "风控资料",
    "special.communication_focus": "沟通重点",
    "menu.url": "菜单链接",
    "menu.link": "菜单链接",
    "menu_collection.url": "菜单收集链接",
    "menu_collection.status": "菜单收集状态",
    "archive.folder_url": "资料文件夹",
    "archive.document_links": "文档链接",
    "installation.network_topology": "网络拓扑",
    "hardware.network_topology": "网络拓扑",
    "communication.preferences": "沟通偏好",
    "communication.notes": "沟通特点",
    "content.url": "内容链接",
    "content.link": "内容链接",
    "content_setup.notes": "内容设置注意事项",
    "system.customer_flow.member_login": "会员登录",
    "system.customer_flow.start_screen": "点餐启动页",
    "configuration.menu.item_images": "菜单图片",
    "configuration.menu.options": "必选项配置",
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
    "system": "系统",
    "login_url": "登录入口",
    "username": "账号",
    "password": "密码",
}


class ReplyChannel(Protocol):
    async def reply(self, message: Any, content: Any, opts: Any = None) -> Any: ...

    async def add_reaction(self, message_id: str, emoji_type: str) -> Any: ...

    async def add_typing_reaction(self, message_id: str) -> str | None: ...

    async def remove_typing_reaction(
        self, message_id: str, reaction_id: str
    ) -> bool: ...

    async def download_resource(
        self,
        file_key: str,
        resource_type: str = "image",
        message_id: str | None = None,
    ) -> bytes | None: ...

    async def fetch_inbound_message(self, message_id: str) -> Any: ...

    async def get_chat_info(self, chat_id: str) -> Any: ...


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


@dataclass(frozen=True)
class ArchiveFileResource:
    file_key: str
    file_name: str


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
    pending_media: list[dict[str, Any]] = field(default_factory=list)
    media_failures: list[dict[str, str]] = field(default_factory=list)
    media_module_node_tokens: dict[str, str] = field(default_factory=dict)
    codex_dialogue_session_id: str | None = None
    # Group collection is long-running by default. It only writes after an
    # explicit @bot save command; this flag is the user's persisted pause state.
    group_summary_enabled: bool = True
    group_buffer: list[dict[str, Any]] = field(default_factory=list)
    group_pending_count: int = 0
    group_generation: int = 0
    group_last_message: Any = None
    group_followup_pending: bool = False
    group_save_pending: bool = False
    group_name: str | None = None
    group_merchant_hint: str | None = None
    group_name_lookup_attempted: bool = field(default=False, repr=False)
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
        self.pending_media.clear()
        self.media_failures.clear()
        self.media_module_node_tokens.clear()
        self.codex_dialogue_session_id = None
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
        "pending_media",
        "media_failures",
        "media_module_node_tokens",
        "codex_dialogue_session_id",
        "group_summary_enabled",
        "group_buffer",
        "group_pending_count",
        "group_generation",
        "group_followup_pending",
        "group_save_pending",
        "group_name",
        "group_merchant_hint",
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
            "version": 3,
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
            state_version = int(payload.get("version", 1))
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
                # Before version 2, false also meant "this group has never
                # opted in". Version 2 makes group collection default-on, so
                # old false values must not leave existing groups disabled.
                if state_version < 2:
                    values["group_summary_enabled"] = True
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
    normalized = _normalize_intent_text(text)
    if normalized in NATURAL_SAVE_CONFIRMATIONS:
        return True
    prefixes = ("结束并保存", "帮我保存", "麻烦保存", "请保存", "保存吧", "保存")
    modifiers = (
        "文字",
        "图片",
        "视频",
        "附件",
        "内容",
        "这些",
        "以上",
        "本次",
        "这次",
        "全部",
        "都要",
        "也要",
        "一起",
        "和",
        "跟",
    )
    for prefix in prefixes:
        if not normalized.startswith(prefix):
            continue
        remainder = normalized[len(prefix) :]
        previous = None
        while remainder and remainder != previous:
            previous = remainder
            for modifier in modifiers:
                remainder = remainder.replace(modifier, "")
        return not remainder
    return False


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


def _is_group_pause_intent(text: str) -> bool:
    normalized = _normalize_intent_text(text)
    if normalized in GROUP_PAUSE_COMMANDS:
        return True
    if not normalized or len(normalized) > 24:
        return False
    pause_terms = ("暂停", "停止", "关闭", "取消", "不要", "别", "无需", "不用")
    record_terms = ("记录", "收集", "整理", "总结", "归纳", "录入")
    return any(term in normalized for term in pause_terms) and any(
        term in normalized for term in record_terms
    )


def _is_group_save_confirmation(text: str) -> bool:
    return _is_save_confirmation(text)


def _merchant_hint_from_group_name(group_name: str) -> str | None:
    """Extract a conservative CRM lookup hint from a Feishu group title."""

    value = re.sub(r"\s+", " ", group_name).strip()
    if not value:
        return None
    value = re.split(
        r"\s*[-–—|｜]?\s*[\[【（(]?\s*(?:客户管理\s*)?CRM(?:\s*group|\s*同步群|\s*群)?",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    value = re.sub(
        r"\s*[-–—|｜]?\s*(?:预计|计划)?\s*"
        r"(?:\d{1,2}[./-]\d{1,2}(?:日|号)?|\d{1,2}月\d{1,2}日?)\s*"
        r"(?:开业|上线|开店).*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = value.strip(" \t\r\n-–—|｜_[]【】()（）")
    compact = _normalize_intent_text(value)
    generic_names = {
        "客户信息录入测试群",
        "客户资料录入测试群",
        "商户资料收集群",
        "客户管理群",
        "crm同步群",
        "测试群",
    }
    if len(compact) < 2 or compact in generic_names:
        return None
    return value


def _save_request_evidence(text: str) -> str | None:
    """Return useful evidence that precedes a trailing save instruction.

    Staff often combine the final missing detail with the action, for example
    ``Little Amigos Fountain Gate 保存吧``. Treating that entire sentence as
    only an action drops the merchant name before analysis. Reuse the strict
    save-confirmation parser for the suffix so ordinary facts containing the
    verb "保存" are not accidentally promoted to save requests.
    """

    stripped = text.strip()
    if not stripped or _is_save_confirmation(stripped):
        return None
    markers = ("结束并保存", "保存吧", "帮我保存", "麻烦保存", "请保存")
    for marker in markers:
        marker_index = stripped.rfind(marker)
        if marker_index <= 0:
            continue
        suffix = stripped[marker_index:]
        if not _is_save_confirmation(suffix):
            continue
        evidence = stripped[:marker_index].strip(" \t\r\n，,。.!！?？、:：;；\"'“”‘’")
        if evidence:
            return evidence
    return None


def _is_media_status_request(text: str) -> bool:
    normalized = _normalize_intent_text(text)
    media_terms = ("图片", "图", "附件", "文件", "视频")
    failure_terms = ("没读取", "未读取", "读取失败", "没成功", "失败", "有问题")
    question_terms = ("哪个", "哪张", "哪些", "哪一个", "什么", "详情", "告诉我")
    return (
        any(term in normalized for term in media_terms)
        and any(term in normalized for term in failure_terms)
        and any(term in normalized for term in question_terms)
    )


def _is_retry_request(text: str) -> bool:
    normalized = _normalize_intent_text(text)
    if normalized in RETRY_COMMANDS:
        return True
    if not normalized or len(normalized) > 24:
        return False
    retry_terms = ("重试", "再试", "重新读取", "再读取", "重新分析", "再分析")
    request_terms = ("请", "你", "帮我", "帮忙", "麻烦", "一下", "一次", "看看")
    return any(term in normalized for term in retry_terms) and any(
        term in normalized for term in request_terms
    )


def _is_effective_group_message(text: str) -> bool:
    compact = _normalize_intent_text(text)
    return len(compact) >= 2 and bool(re.search(r"[0-9A-Za-z\u3400-\u9fff]{2}", compact))


def _is_group_profile_candidate(text: str, *, has_media: bool = False) -> bool:
    """Keep durable merchant evidence while dropping discussion about the bot itself."""
    compact = _normalize_intent_text(text)
    if not compact:
        return has_media
    meta_terms = (
        "机器人",
        "自动监听",
        "客户信息录入",
        "客户资料录入",
        "怎么使用",
        "如何使用",
        "使用说明",
        "拉进群",
        "切换商户前",
        "/discard",
        "/save",
    )
    if any(term in compact for term in meta_terms):
        return False
    # Questions about product capabilities or troubleshooting belong to the
    # support flow, not the merchant profile. Rich link previews can carry an
    # image resource and make these questions look like profile data.
    if (
        "?" in compact
        or "？" in compact
        or re.search(r"(?:吗|么|呢)$", compact)
        or re.match(
            r"^(?:请问)?(?:怎么|如何|为什么|哪里|什么|是否|能否|可否|可不可以|有没有)",
            compact,
        )
    ):
        return False
    support_guide_terms = (
        "设置方式",
        "方式1",
        "方式2",
        "方式3",
        "操作步骤",
        "控制面板",
        "设备和打印机",
        "右键",
        "点击设置",
        "调试工具",
        "下载驱动",
        "重启打印机",
        "补充说明",
    )
    merchant_anchor_terms = (
        "商户",
        "客户",
        "门店",
        "店铺",
        "该店",
        "此店",
        "这家",
        "他们",
        "联系人",
        "店长",
        "地址",
    )
    if sum(term in compact for term in support_guide_terms) >= 2 and not any(
        term in compact for term in merchant_anchor_terms
    ):
        return False
    casual_phone_terms = (
        "有空吗",
        "电话聊",
        "打电话",
        "电话沟通",
        "电话会议",
        "电话说",
        "通个电话",
        "约个电话",
    )
    if not has_media and any(term in compact for term in casual_phone_terms):
        return False
    if has_media:
        return True
    # A standalone public link can be durable merchant evidence. Keep it in the
    # group draft so the URL reader can inspect it when the user asks for a
    # summary or save; unsupported or irrelevant pages are still filtered by
    # the analysis and explicit-confirmation boundary.
    if re.search(r"https://", text, re.IGNORECASE):
        return True
    profile_terms = (
        "项目进度",
        "飞书项目",
        "资料收集",
        "信息表",
        "压缩包",
        "商户",
        "客户",
        "门店",
        "店铺",
        "地址",
        "street",
        "联系人",
        "店长",
        "经理",
        "负责人",
        "手机",
        "邮箱",
        "email",
        "账号",
        "密码",
        "登录",
        "后台",
        "业务",
        "规模",
        "员工",
        "pos",
        "产品",
        "菜单",
        "菜品",
        "价格",
        "营业",
        "系统",
        "配置",
        "设备",
        "硬件",
        "打印",
        "收银",
        "点餐",
        "支付",
        "配送",
        "安装",
        "网络",
        "wifi",
        "故障",
        "问题",
        "缺失",
        "报错",
        "无法",
        "不能",
        "异常",
        "风险",
        "合规",
        "合同",
        "套餐",
        "付款方式",
        "扣款",
        "刷卡机",
        "eftpos",
        "surcharge",
        "申请公司",
        "风控资料",
        "沟通重点",
        "沟通特点",
        "资料归档",
        "文件夹",
        "文档链接",
        "内容设置",
        "费用",
        "佣金",
        "折扣",
        "优惠",
        "会员",
        "库存",
        "订单",
        "发票",
        "gst",
        "abn",
        "咖啡",
        "餐厅",
        "饭店",
        "餐馆",
        "便当",
        "火锅",
        "麻辣烫",
        "奶茶",
        "restaurant",
        "cafe",
    )
    if any(term in compact for term in profile_terms):
        return True
    if "电话" not in compact:
        return False
    phone_fact_cues = (
        "电话是",
        "联系电话",
        "电话号码",
        "商户电话",
        "门店电话",
        "客户电话",
        "他的电话",
        "她的电话",
        "他们电话",
    )
    return any(term in compact for term in phone_fact_cues) or bool(
        re.search(r"\d{6,}", compact)
    )


def _explicitly_mentions_merchant(
    messages: list[dict[str, Any]], merchant_name: str
) -> bool:
    merchant_key = _normalize_intent_text(merchant_name)
    if not merchant_key:
        return False
    for message in messages:
        if merchant_key in _normalize_intent_text(str(message.get("content") or "")):
            return True
        images = message.get("images")
        if isinstance(images, list) and images:
            return True
    return False


def _merchant_name_key(value: Any) -> str:
    return _normalize_intent_text(str(value or ""))


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
        explicit_label = item.get("label") or item.get("title")
        links.append(
            {
                "label": (
                    str(explicit_label)
                    if isinstance(explicit_label, str) and explicit_label.strip()
                    else DOCUMENT_LABELS.get(
                        document_type, document_type or "知识库页面"
                    )
                ),
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
    label = FIELD_SUFFIX_LABELS.get(suffix, suffix.replace("_", " ") or "资料")
    parts = path.split(".")
    if len(parts) >= 3 and parts[0] == "credentials":
        account = parts[1].replace("_", " ").replace("-", " ")
        return f"{account} · {label}"
    return label


def _is_password_field(field_path: Any) -> bool:
    path = str(field_path or "")
    return (
        path.startswith("credentials.")
        and path.endswith(".password")
        and len(path.split(".")) >= 3
    )


def _display_value(field_path: Any, value: Any) -> Any:
    if _is_password_field(field_path):
        return "••••••（已隐藏）"
    return value


def _profile_for_analysis(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Keep a committed password out of later model context and bot replies."""
    result = copy.deepcopy(dict(profile))
    fields = result.get("fields")
    if not isinstance(fields, dict):
        return result
    for field_path, password in fields.items():
        if not _is_password_field(field_path):
            continue
        if isinstance(password, dict) and "value" in password:
            password["value"] = "[已保存，未显示]"
        else:
            fields[field_path] = "[已保存，未显示]"
    return result


def _hide_password_values(text: str, result: Mapping[str, Any]) -> str:
    secrets: list[str] = []
    for item in result.get("profile_updates") or []:
        if isinstance(item, Mapping) and _is_password_field(item.get("field_path")):
            secrets.append(str(item.get("value") or ""))
    for item in result.get("conflicts") or []:
        if isinstance(item, Mapping) and _is_password_field(item.get("field_path")):
            secrets.extend(
                [
                    str(item.get("current_value") or ""),
                    str(item.get("proposed_value") or ""),
                ]
            )
    for secret in sorted({value for value in secrets if value}, key=len, reverse=True):
        text = text.replace(secret, "••••••")
    return re.sub(
        r"((?:password|passcode|密码|口令)\s*(?:[:：=]|是)\s*)([^\s,，;；]+)",
        r"\1••••••",
        text,
        flags=re.IGNORECASE,
    )


def _url_evidence_lines(result: dict[str, Any]) -> list[str]:
    raw = result.get("url_evidence")
    if not isinstance(raw, list):
        return []
    lines: list[str] = []
    for item in raw[:3]:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        title = str(item.get("title") or "").strip()
        final_url = str(item.get("final_url") or item.get("original_url") or "").strip()
        if status == "read":
            lines.append(f"已读取链接：{title or final_url}")
        elif status == "login_required":
            lines.append(f"链接需要登录，仅读取到公开登录页：{title or final_url}")
        elif status == "unavailable":
            reason = str(item.get("reason") or "无法读取").strip()
            lines.append(f"链接未读取：{final_url}（{reason}）")
    return lines


def _preview_text(result: dict[str, Any]) -> str:
    lines: list[str] = []
    analysis_batches = int(result.get("analysis_batches") or 1)
    if analysis_batches > 1:
        lines.append(f"已自动分 {analysis_batches} 批完成全部图片分析。")
    updates = result.get("profile_updates")
    if isinstance(updates, list) and updates:
        lines.append("待保存：")
        for item in updates[:20]:
            if isinstance(item, dict):
                lines.append(
                    f"- {_field_label(item.get('field_path'))}："
                    f"{_display_value(item.get('field_path'), item.get('value'))}"
                )
    conflicts = result.get("conflicts")
    if isinstance(conflicts, list) and conflicts:
        if lines:
            lines.append("")
        lines.append("有旧值冲突，请确认是否替换。")
    classifications = result.get("module_classifications")
    if isinstance(classifications, list):
        uncertain = [
            item
            for item in classifications
            if isinstance(item, Mapping) and item.get("status") == "needs_confirmation"
        ]
        if uncertain:
            if lines:
                lines.append("")
            lines.append("待确认归类：")
            for item in uncertain[:5]:
                document_type = str(item.get("document_type") or "")
                label = DOCUMENT_LABELS.get(document_type, document_type)
                confidence = float(item.get("confidence") or 0)
                lines.append(f"- {label}（{confidence:.0%}）：{item.get('reason')}")
            lines.append("可回复“这些资料归到硬件与安装”等明确模块。")
    url_lines = _url_evidence_lines(result)
    if url_lines:
        if lines:
            lines.append("")
        lines.extend(url_lines)
    readiness = result.get("save_readiness")
    if readiness == "ready" and updates:
        lines.extend(["", "回复“保存吧”写入。"])
    elif result.get("next_question"):
        if lines:
            lines.append("")
        lines.append(str(result["next_question"]))
    else:
        lines.append(str(result.get("reply") or "没有需要记录的信息。"))
    return _hide_password_values("\n".join(lines), result)


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

    analysis_batches = int(result.get("analysis_batches") or 1)
    if analysis_batches > 1:
        sections.append(f"已自动分 {analysis_batches} 批完成全部图片分析。")

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

    updates = result.get("profile_updates")
    if isinstance(updates, list) and updates:
        definitions = [
            (
                "当前点餐流程",
                lambda path: path.startswith("system.customer_flow."),
            ),
            (
                "已发现的问题",
                lambda path: path in {
                    "configuration.menu.item_images",
                    "configuration.menu.options",
                }
                or path.startswith("system.observed_issues."),
            ),
            (
                "业务与联系人",
                lambda path: path.startswith(("business.", "contacts.")),
            ),
            (
                "门店资料",
                lambda path: path.startswith("stores."),
            ),
            (
                "菜单与内容",
                lambda path: path.startswith(("menu.", "content.")),
            ),
            (
                "系统配置与处理方案",
                lambda path: path.startswith(
                    ("system.", "configuration.", "workarounds.")
                ),
            ),
            (
                "硬件与安装",
                lambda path: path.startswith(("hardware.", "installation.")),
            ),
            (
                "风险与合规",
                lambda path: path.startswith(("risk.", "compliance.")),
            ),
            (
                "商务信息",
                lambda path: path.startswith("commercial."),
            ),
            (
                "账号与密码",
                lambda path: path.startswith("credentials."),
            ),
        ]
        remaining = [item for item in updates[:20] if isinstance(item, dict)]
        for title, matches in definitions:
            selected = [
                item
                for item in remaining
                if matches(str(item.get("field_path") or ""))
            ]
            if not selected:
                continue
            sections.append(
                title
                + "：\n"
                + "\n".join(
                    f"- {_field_label(item.get('field_path'))}："
                    f"{_display_value(item.get('field_path'), item.get('value'))}"
                    for item in selected
                )
            )
            selected_ids = {id(item) for item in selected}
            remaining = [item for item in remaining if id(item) not in selected_ids]
        if remaining:
            sections.append(
                "其他商户资料：\n"
                + "\n".join(
                    f"- {_field_label(item.get('field_path'))}："
                    f"{_display_value(item.get('field_path'), item.get('value'))}"
                    for item in remaining
                )
            )
    else:
        add_section("本段摘要", result.get("summary_points"))

    add_section("已确认决定", result.get("decisions"))
    add_section("后续事项", result.get("action_items"))

    url_lines = _url_evidence_lines(result)
    if url_lines:
        sections.append("链接读取：\n" + "\n".join(f"- {line}" for line in url_lines))

    conflicts = result.get("conflicts")
    if isinstance(conflicts, list) and conflicts:
        lines = [
            f"- {_field_label(item.get('field_path'))}："
            f"{_display_value(item.get('field_path'), item.get('current_value'))} → "
            f"{_display_value(item.get('field_path'), item.get('proposed_value'))}"
            for item in conflicts[:10]
            if isinstance(item, dict)
        ]
        if lines:
            sections.append("资料冲突：\n" + "\n".join(lines))

    classifications = result.get("module_classifications")
    if isinstance(classifications, list):
        uncertain_lines = []
        for item in classifications:
            if not isinstance(item, Mapping) or item.get("status") != "needs_confirmation":
                continue
            document_type = str(item.get("document_type") or "")
            label = DOCUMENT_LABELS.get(document_type, document_type)
            confidence = float(item.get("confidence") or 0)
            uncertain_lines.append(
                f"- {label}（{confidence:.0%}）：{item.get('reason')}"
            )
        if uncertain_lines:
            sections.append(
                "待确认归类：\n"
                + "\n".join(uncertain_lines[:5])
                + "\n请 @机器人 回复“这些资料归到硬件与安装”等明确模块。"
            )

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
        sections.append("以上内容尚未写入。如需保存，请 @机器人 回复“保存吧”。")
    return _hide_password_values("\n\n".join(sections), result)


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


def _resource_message_ids(message: Any) -> list[str]:
    """Return plausible owners for resources embedded in quoted/rich messages."""

    message_ids: list[str] = []

    def append(value: Any) -> None:
        if (
            isinstance(value, str)
            and value.startswith(("om_", "omt_"))
            and value not in message_ids
        ):
            message_ids.append(value)

    append(getattr(message, "message_id", None))

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            # These are the common resource-owning IDs exposed by Feishu for
            # replies, quoted posts, threads and merged-forward children.
            for key in ("message_id", "parent_id", "root_id", "upper_message_id"):
                append(value.get(key))
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)
        elif isinstance(value, str) and any(
            token in value
            for token in ("message_id", "parent_id", "root_id", "upper_message_id")
        ):
            try:
                collect(json.loads(value))
            except (TypeError, ValueError, json.JSONDecodeError):
                return

    collect(getattr(message, "raw", None))
    return message_ids[:12]


def _is_resource_owner_mismatch(error: Exception) -> bool:
    """Return whether Feishu says the resource belongs to another message."""

    detail = f"{type(error).__name__}: {error}".lower()
    return "234003" in detail or "file not in msg" in detail


def _is_transient_resource_error(error: Exception) -> bool:
    """Recognise transport failures for which repeating a read is safe."""

    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    detail = f"{type(error).__name__}: {error}".lower()
    return any(
        marker in detail
        for marker in (
            "timeout",
            "timed out",
            "connecterror",
            "connection reset",
            "connection aborted",
            "temporarily unavailable",
            "remote protocol error",
            "too many requests",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
        )
    )


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


def _archive_file_resources(message: Any) -> list[ArchiveFileResource]:
    resources: list[ArchiveFileResource] = []

    def add(file_key: Any, file_name: Any) -> None:
        if not isinstance(file_key, str) or not file_key:
            return
        name = str(file_name or "attachment.bin")
        suffix = Path(name).suffix.lower()
        if suffix not in ARCHIVE_FILE_EXTENSIONS:
            return
        if any(item.file_key == file_key for item in resources):
            return
        resources.append(ArchiveFileResource(file_key=file_key, file_name=name))

    content = getattr(message, "content", None)
    kind = str(_resource_value(content, "kind") or "").lower()
    if kind == "file":
        add(
            _resource_value(content, "file_key"),
            _resource_value(content, "file_name") or _resource_value(content, "name"),
        )
    for resource in getattr(message, "resources", []) or []:
        if str(_resource_value(resource, "type") or "").lower() != "file":
            continue
        add(
            _resource_value(resource, "file_key"),
            _resource_value(resource, "file_name") or _resource_value(resource, "name"),
        )
    return resources


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


def _without_file_placeholders(text: str) -> str:
    text = re.sub(r"<file\b[^>]*?/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"&lt;file\b.*?/?&gt;", " ", text, flags=re.IGNORECASE)
    return re.sub(r"[ \t]+", " ", text).strip()


def _image_mime_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _image_file_name(message: Any, ordinal: int, media_type: str) -> str:
    content = getattr(message, "content", None)
    supplied = _resource_value(content, "file_name") or _resource_value(content, "name")
    extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[
        media_type
    ]
    if (
        isinstance(supplied, str)
        and supplied.strip()
        and Path(supplied.strip()).suffix.lower()
        in {".png", ".jpg", ".jpeg", ".webp"}
    ):
        return supplied.strip()
    message_id = re.sub(
        r"[^A-Za-z0-9_-]+", "_", str(getattr(message, "message_id", "image"))
    )[:80]
    suffix = f"-{ordinal + 1}" if ordinal else ""
    return f"screenshot-{message_id}{suffix}{extension}"


def _media_preview_suffix(items: Sequence[Mapping[str, Any]]) -> str:
    if not items:
        return ""
    routed: list[str] = []
    unresolved: list[str] = []
    for item in items[:5]:
        name = str(item.get("file_name") or "附件")
        document_type = str(item.get("target_document_type") or "")
        if document_type in MEDIA_FIELD_PREFIXES:
            suffix = "（未识别明确归属，兜底）" if document_type == "document-archive" else ""
            routed.append(f"- {name} → {DOCUMENT_LABELS[document_type]}{suffix}")
        else:
            unresolved.append(name)
    lines = ["待保存附件：", *routed]
    if unresolved:
        lines.append("- 待确认模块：" + "、".join(unresolved))
        lines.append("请回复“附件归到菜单/硬件设备/账号与密码”等明确模块后再保存。")
    else:
        lines.append("回复“保存吧”后会上传到上面的对应模块。")
    if len(items) > 5:
        lines.append(f"另有 {len(items) - 5} 个附件。")
    return "\n\n" + "\n".join(lines)


def _media_failure_suffix(items: Sequence[Mapping[str, Any]]) -> str:
    if not items:
        return ""
    details: list[str] = []
    for item in items[:5]:
        message_id = str(item.get("message_id") or "")
        short_id = message_id[-8:] if message_id else "未知消息"
        reason = str(item.get("reason") or "飞书资源读取失败")
        details.append(f"- 图片消息（编号尾号 {short_id}）：{reason}")
    if len(items) > 5:
        details.append(f"- 另有 {len(items) - 5} 条失败记录")
    return (
        "\n\n⚠️ 以下图片/附件尚未进入总结：\n"
        + "\n".join(details)
        + "\n这些失败项不会阻止其他已识别资料保存，保存后仍会保留为待重试项。"
        "请 @机器人 回复“再试一下”，或在对应原消息下重新发送原图；"
        "如确认无需图片内容，可回复“仅保存文字”。"
    )


def _partial_save_media_failure_suffix(items: Sequence[Mapping[str, Any]]) -> str:
    if not items:
        return ""
    count = len(items)
    return (
        f"\n\n⚠️ 已先保存可用内容；仍有 {count} 条图片/附件未能读取，"
        "失败记录和原消息位置已保留。"
        "请 @机器人 回复“再试一下”，或重新发送原图，成功后可再次保存补充内容。"
    )


def _current_profile_values(session: Session) -> dict[str, Any]:
    fields = session.current_profile.get("fields")
    if not isinstance(fields, Mapping):
        return {}
    values: dict[str, Any] = {}
    for path, raw in fields.items():
        if not isinstance(path, str):
            continue
        values[path] = raw.get("value") if isinstance(raw, Mapping) else raw
    return values


def _commit_acknowledged(result: Mapping[str, Any]) -> bool:
    store = result.get("result")
    store_result = store if isinstance(store, Mapping) else {}
    return bool(
        result.get("write_performed")
        or store_result.get("duplicate")
        or store_result.get("store_status") == "duplicate"
    )


def _profile_contains_updates(
    profile: Mapping[str, Any], updates: Sequence[Mapping[str, Any]]
) -> bool:
    fields = profile.get("fields")
    if not isinstance(fields, Mapping) or not updates:
        return False
    for update in updates:
        path = update.get("field_path")
        if not isinstance(path, str) or path not in fields:
            return False
        raw = fields[path]
        actual = raw.get("value") if isinstance(raw, Mapping) else raw
        expected = update.get("value")
        if actual == expected:
            continue
        if isinstance(actual, (str, int, float, bool)) and isinstance(
            expected, (str, int, float, bool)
        ):
            if str(actual).strip() == str(expected).strip():
                continue
        return False
    return True


def _apply_explicit_module_to_preview(session: Session, document_type: str) -> int:
    preview = session.preview
    if not isinstance(preview, dict):
        return 0
    raw_classifications = preview.get("module_classifications")
    if not isinstance(raw_classifications, list):
        return 0
    uncertain = [
        item
        for item in raw_classifications
        if isinstance(item, dict) and item.get("status") == "needs_confirmation"
    ]
    if not uncertain:
        return 0
    updates = preview.get("profile_updates")
    if not isinstance(updates, list):
        return 0
    by_path = {
        str(item.get("field_path")): item
        for item in uncertain
        if isinstance(item.get("field_path"), str)
    }
    rerouted: list[dict[str, Any]] = []
    changed = 0
    for item in updates:
        if not isinstance(item, Mapping):
            continue
        clean = {
            "field_path": item.get("field_path"),
            "value": item.get("value"),
            "source_ref": item.get("source_ref"),
            "replace_confirmed": bool(item.get("replace_confirmed")),
        }
        classification = by_path.get(str(clean["field_path"]))
        if classification is not None:
            clean["field_path"] = reroute_field_path(str(clean["field_path"]), document_type)
            classification["field_path"] = clean["field_path"]
            classification["document_type"] = document_type
            classification["confidence"] = 1.0
            classification["reason"] = "用户明确指定模块"
            classification["status"] = "confirmed"
            changed += 1
        rerouted.append(clean)
    if not changed:
        return 0
    merchant = preview.get("merchant")
    merchant_name = merchant.get("name") if isinstance(merchant, Mapping) else None
    validated = validate_update_set(merchant_name, rerouted, _current_profile_values(session))
    preview.update(validated)
    preview["classification_review_required"] = any(
        isinstance(item, Mapping) and item.get("status") == "needs_confirmation"
        for item in raw_classifications
    )
    if preview["classification_review_required"]:
        preview["save_readiness"] = "needs_confirmation"
    else:
        preview["next_question"] = None
    return changed


def _media_field_base(item: Mapping[str, Any]) -> str | None:
    archive_id = str(item.get("archive_id") or "")
    document_type = str(item.get("target_document_type") or "")
    prefix = MEDIA_FIELD_PREFIXES.get(document_type)
    if not prefix or not re.fullmatch(r"media_[a-f0-9]{16}", archive_id):
        return None
    if document_type == "document-archive":
        return f"documents.{archive_id}"
    return f"{prefix}.evidence.{archive_id}"


def _document_type_for_update(item: Mapping[str, Any]) -> str | None:
    document_type = str(item.get("document_type") or "")
    if document_type in MEDIA_FIELD_PREFIXES:
        return document_type
    field_path = item.get("field_path")
    if not isinstance(field_path, str):
        return None
    try:
        document_type = document_for_field(field_path)
    except (ValueError, TypeError):
        return None
    return document_type if document_type in MEDIA_FIELD_PREFIXES else None


def _explicit_media_module(text: str) -> str | None:
    normalized = _normalize_intent_text(text)
    if not any(term in normalized for term in ("归到", "放到", "存到", "存进", "属于", "这是")):
        return None
    aliases = {
        "菜单与内容": "menu-content",
        "菜单": "menu-collection",
        "系统配置": "system-configuration",
        "硬件与安装": "hardware-installation",
        "硬件设备": "hardware-records",
        "刷卡机": "card-terminal",
        "风控合规": "risk-compliance",
        "商务信息": "commercial-information",
        "门店档案": "store-profile",
        "项目进度": "project-progress",
        "资料收集": "data-collection",
        "套餐商务": "package-commercial",
        "特殊事项": "special-matters",
        "沟通特点": "communication-profile",
        "内容设置": "content-settings",
        "账号与密码": "credential-records",
        "账号密码": "credential-records",
        "联系人": "business-contacts",
    }
    for label in sorted(aliases, key=len, reverse=True):
        if label in normalized:
            return aliases[label]
    return None


def _assign_media_modules(session: Session) -> None:
    updates = (session.preview or {}).get("profile_updates")
    update_items = updates if isinstance(updates, list) else []
    priority = {name: index for index, name in enumerate(MEDIA_ROUTE_PRIORITY)}
    raw_classifications = (session.preview or {}).get("module_classifications")
    classified_paths: set[str] | None = None
    if isinstance(raw_classifications, list) and raw_classifications:
        classified_paths = {
            str(item.get("field_path"))
            for item in raw_classifications
            if isinstance(item, Mapping)
            and item.get("status") in {"auto", "confirmed"}
            and isinstance(item.get("field_path"), str)
        }
    for media in session.pending_media:
        if str(media.get("target_document_type") or "") in MEDIA_FIELD_PREFIXES:
            continue
        source_ref = str(media.get("source_ref") or "")
        candidates = [
            candidate
            for update in update_items
            if isinstance(update, Mapping)
            and str(update.get("source_ref") or "") == source_ref
            and (
                classified_paths is None
                or str(update.get("field_path") or "") in classified_paths
            )
            and (candidate := _document_type_for_update(update)) is not None
        ]
        if candidates:
            counts = {candidate: candidates.count(candidate) for candidate in set(candidates)}
            media["target_document_type"] = min(
                counts,
                key=lambda candidate: (-counts[candidate], priority.get(candidate, 999)),
            )
            continue
        media["target_document_type"] = "document-archive"


def _unresolved_media(session: Session) -> list[dict[str, Any]]:
    return [item for item in session.pending_media if _media_field_base(item) is None]


def _pending_media_updates(session: Session) -> list[dict[str, Any]]:
    current = _current_profile_values(session)
    updates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in session.pending_media:
        archive_id = str(item.get("archive_id") or "")
        field_base = _media_field_base(item)
        if field_base is None or archive_id in seen:
            continue
        seen.add(archive_id)
        source_ref = str(item.get("source_ref") or "feishu://unknown")
        values = {
            "name": str(item.get("file_name") or "附件"),
            "media_type": str(item.get("media_type") or "application/octet-stream"),
            "size_bytes": str(item.get("size_bytes") or "0"),
            "sha256": str(item.get("sha256") or ""),
            "source_ref": source_ref,
            "status": "待保存",
        }
        for suffix, value in values.items():
            field_path = f"{field_base}.{suffix}"
            if field_path in current:
                continue
            updates.append(
                {
                    "field_path": field_path,
                    "value": value,
                    "source_ref": source_ref,
                    "replace_confirmed": False,
                    "state": "explicit",
                }
            )
    return updates


def _archived_media_updates(
    item: Mapping[str, Any], uploaded: Mapping[str, Any]
) -> list[dict[str, Any]]:
    archive_id = str(item["archive_id"])
    field_base = _media_field_base(item)
    if field_base is None:
        raise MediaArchiveError("附件尚未确定所属模块。")
    source_ref = str(item.get("source_ref") or "feishu://unknown")
    values = {
        "url": str(uploaded["url"]),
        "file_token": str(uploaded["file_token"]),
        "status": "已保存",
    }
    if uploaded.get("block_id"):
        values["block_id"] = str(uploaded["block_id"])
    return [
        {
            "field_path": f"{field_base}.{suffix}",
            "value": value,
            "source_ref": source_ref,
            "replace_confirmed": suffix == "status",
            "state": "explicit",
        }
        for suffix, value in values.items()
    ]


def _document_node_tokens(store_result: Mapping[str, Any]) -> dict[str, str]:
    documents = store_result.get("documents")
    if not isinstance(documents, list):
        return {}
    result: dict[str, str] = {}
    for item in documents:
        if not isinstance(item, Mapping):
            continue
        document_type = str(item.get("document_type") or "")
        obj_token = item.get("obj_token")
        if document_type in MEDIA_FIELD_PREFIXES and isinstance(obj_token, str) and obj_token:
            result[document_type] = obj_token
    return result


def _image_data_url_bytes(value: Any) -> int:
    if not isinstance(value, str) or ";base64," not in value:
        return 0
    try:
        return len(base64.b64decode(value.split(",", 1)[1], validate=True))
    except (ValueError, TypeError):
        return 0


class ImageBatchLimitError(RuntimeError):
    pass


def _message_batches_for_analysis(
    messages: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Split images into bounded model calls while retaining full text context."""
    base = [
        {
            key: value
            for key, value in message.items()
            if key not in {"images", "has_media", "media_failed"}
        }
        for message in messages
    ]
    queued: list[tuple[int, str, int]] = []
    for message_index, message in enumerate(messages):
        raw_images = message.get("images", [])
        if not isinstance(raw_images, list):
            continue
        for image in raw_images:
            size = _image_data_url_bytes(image)
            if size <= 0 or size > MAX_IMAGE_BYTES:
                continue
            queued.append((message_index, image, size))
    total_bytes = sum(item[2] for item in queued)
    if len(queued) > MAX_IMAGES_PER_ANALYSIS:
        raise ImageBatchLimitError(
            f"本轮共有 {len(queued)} 张图片，单轮最多自动处理 {MAX_IMAGES_PER_ANALYSIS} 张。"
            "请先总结并保存这一轮，再继续发送下一轮。"
        )
    if total_bytes > MAX_TOTAL_IMAGE_BYTES_PER_ANALYSIS:
        raise ImageBatchLimitError(
            f"本轮图片总大小约 {total_bytes / 1024 / 1024:.1f} MB，"
            f"单轮最多 {MAX_TOTAL_IMAGE_BYTES_PER_ANALYSIS // 1024 // 1024} MB。"
            "请拆成两轮总结。"
        )
    if not queued:
        return [base]

    batches: list[list[dict[str, Any]]] = []
    cursor = 0
    while cursor < len(queued):
        selected: dict[int, list[str]] = {}
        batch_bytes = 0
        batch_count = 0
        while cursor < len(queued) and batch_count < MAX_IMAGES_PER_MODEL_BATCH:
            message_index, image, size = queued[cursor]
            if batch_count and batch_bytes + size > MAX_IMAGE_BYTES_PER_MODEL_BATCH:
                break
            selected.setdefault(message_index, []).append(image)
            batch_bytes += size
            batch_count += 1
            cursor += 1
        copied = copy.deepcopy(base)
        for message_index, images in selected.items():
            copied[message_index]["images"] = images
        batches.append(copied)
    return batches


def _deduplicated(items: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for item in items:
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


def _merge_analysis_previews(previews: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not previews:
        raise MerchantApiError("analysis returned no batches")
    merged = copy.deepcopy(dict(previews[0]))
    list_fields = (
        "profile_updates",
        "unchanged",
        "conflicts",
        "assumptions",
        "information_gaps",
        "summary_points",
        "decisions",
        "action_items",
        "url_evidence",
        "module_classifications",
    )
    for field_name in list_fields:
        combined = [
            item
            for preview in previews
            for item in (
                preview.get(field_name)
                if isinstance(preview.get(field_name), list)
                else []
            )
        ]
        merged[field_name] = _deduplicated(combined)
    accepted = merged.get("profile_updates")
    conflicts = merged.get("conflicts")
    classifications = merged.get("module_classifications")
    accepted_items = accepted if isinstance(accepted, list) else []
    conflict_items = conflicts if isinstance(conflicts, list) else []
    classification_items = classifications if isinstance(classifications, list) else []
    modules = [
        str(item.get("document_type"))
        for item in accepted_items
        if isinstance(item, Mapping) and item.get("document_type")
    ]
    merged["affected_modules"] = list(dict.fromkeys(modules))
    for index, item in enumerate(classification_items):
        if isinstance(item, dict):
            item["update_index"] = index
    review_required = any(
        isinstance(item, Mapping) and item.get("status") == "needs_confirmation"
        for item in classification_items
    )
    merged["classification_review_required"] = review_required
    named_merchants = [
        preview.get("merchant")
        for preview in previews
        if isinstance(preview.get("merchant"), Mapping)
        and preview.get("merchant", {}).get("name")
    ]
    if named_merchants:
        merged["merchant"] = copy.deepcopy(named_merchants[0])
    merchant = merged.get("merchant")
    has_merchant = isinstance(merchant, Mapping) and bool(merchant.get("name"))
    if conflict_items or review_required:
        merged["save_readiness"] = "needs_confirmation"
    elif accepted_items and has_merchant:
        merged["save_readiness"] = "ready"
    elif not accepted_items:
        merged["save_readiness"] = "needs_input"
    questions = [
        str(preview.get("next_question"))
        for preview in previews
        if preview.get("next_question")
    ]
    merged["next_question"] = questions[0] if questions else None
    merged["analysis_batches"] = len(previews)
    merged["write_performed"] = False
    return merged


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
        media_staging: MediaStagingStore | None = None,
        media_archiver: FeishuDriveArchiver | None = None,
        dialogue_router: DialogueRouter | None = None,
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
        self.media_staging = media_staging
        self.media_archiver = media_archiver
        self.dialogue_router = dialogue_router

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self.api.post, path, payload)

    async def _reply(self, message: Any, text: str) -> None:
        # Plain text prevents user-supplied values from becoming active markdown,
        # links, or mentions when a preview is echoed into a group chat.
        await self.channel.reply(message, {"text": text})

    async def _reply_current_profile_links(
        self, message: Any, session: Session
    ) -> None:
        merchant = session.merchant if isinstance(session.merchant, Mapping) else {}
        merchant_name = str(merchant.get("name") or "").strip()
        if not merchant_name:
            if session.last_documents:
                await self._reply(message, _links_text(session.last_documents))
            else:
                await self._reply(
                    message,
                    "当前还没有确认商户。请先说明商户名称，再让我查询资料链接。",
                )
            return
        try:
            readback = await self._post(
                "/v1/profile/read",
                {
                    "merchant_name": merchant_name,
                    "merchant_id": merchant.get("id"),
                },
            )
        except MerchantApiError:
            LOGGER.exception("real-time merchant document lookup failed")
            if session.last_documents:
                await self._reply(
                    message,
                    "实时查询暂时失败，以下是本会话最近一次保存返回的链接：\n"
                    + "\n".join(
                        f"- {item['label']}：{item['url']}"
                        for item in session.last_documents
                    ),
                )
            else:
                await self._reply(message, "资料页面实时查询失败，请稍后再试。")
            return
        profile = readback.get("profile")
        if not isinstance(profile, dict):
            await self._reply(message, "知识库没有返回有效的商户资料页面。")
            return
        session.current_profile = profile
        session.current_profile_loaded = True
        self._apply_crm_resolution(session, readback.get("crm"))
        documents = _document_links(profile)
        if not documents:
            if session.last_documents:
                await self._reply(
                    message,
                    f"知识库暂未返回“{merchant_name}”的实时目录；"
                    "以下是本会话最近一次保存返回的链接：\n"
                    + "\n".join(
                        f"- {item['label']}：{item['url']}"
                        for item in session.last_documents
                    ),
                )
            else:
                await self._reply(
                    message,
                    f"知识库中尚未找到“{merchant_name}”的资料页面。",
                )
            return
        session.last_documents = documents
        await self._reply(
            message,
            f"{merchant_name} 的资料页面（实时查询）：\n"
            + "\n".join(
                f"- {item['label']}：{item['url']}" for item in documents
            ),
        )

    async def _ensure_group_identity(self, session: Session, message: Any) -> None:
        """Resolve a new group's title to CRM without overriding explicit evidence."""

        if session.merchant or session.group_name_lookup_attempted:
            return
        hint = str(session.group_merchant_hint or "").strip()
        if not hint:
            get_chat_info = getattr(self.channel, "get_chat_info", None)
            chat_id = str(getattr(message, "chat_id", "") or "").strip()
            if not callable(get_chat_info) or not chat_id:
                return
            try:
                chat_info = await get_chat_info(chat_id)
            except Exception:
                LOGGER.exception("could not read Feishu group metadata chat=%s", chat_id)
                return
            group_name = str(getattr(chat_info, "name", "") or "").strip()
            if not group_name:
                return
            session.group_name = group_name
            hint = _merchant_hint_from_group_name(group_name) or ""
            session.group_merchant_hint = hint or None
        if not hint:
            # A readable but generic group name is a stable negative result. Do not
            # request the same group metadata for every subsequent message.
            session.group_name_lookup_attempted = True
            return
        try:
            result = await self._post(
                "/v1/profile/crm/resolve",
                {"merchant_name": hint, "merchant_id": None},
            )
        except MerchantApiError:
            LOGGER.warning(
                "CRM lookup from Feishu group name failed group=%s",
                session.group_name,
            )
            return
        resolution = result.get("crm")
        if not isinstance(resolution, dict):
            return
        # Only cache a completed CRM lookup. Temporary API/auth/network failures
        # must remain retryable on the next mentioned command.
        session.group_name_lookup_attempted = True
        session.crm_resolution = resolution
        candidates = resolution.get("candidates")
        candidate = (
            candidates[0]
            if resolution.get("status") == "unique"
            and isinstance(candidates, list)
            and len(candidates) == 1
            and isinstance(candidates[0], Mapping)
            else None
        )
        if not candidate:
            return
        name = str(candidate.get("name") or "").strip()
        merchant_id = str(candidate.get("work_item_id") or "").strip()
        if not name or not merchant_id:
            return
        session.merchant = {"name": name, "id": merchant_id}
        session.current_profile_loaded = False

    @staticmethod
    def _dialogue_context(session: Session, *, is_group: bool) -> dict[str, Any]:
        preview = session.preview if isinstance(session.preview, Mapping) else {}
        merchant = session.merchant if isinstance(session.merchant, Mapping) else {}
        raw_classifications = preview.get("module_classifications")
        unresolved_modules = []
        if isinstance(raw_classifications, list):
            unresolved_modules = [
                {
                    "fact": _field_label(item.get("field_path")),
                    "suggested_document_type": str(item.get("document_type") or ""),
                    "reason": str(item.get("reason") or ""),
                }
                for item in raw_classifications[:10]
                if isinstance(item, Mapping)
                and item.get("status") == "needs_confirmation"
            ]
        return {
            "channel": "group" if is_group else "direct",
            "merchant_name": str(merchant.get("name") or ""),
            "draft_message_count": len(session.messages),
            "group_buffer_count": len(session.group_buffer),
            "has_unsaved_preview": bool(preview),
            "save_readiness": str(preview.get("save_readiness") or "unknown"),
            "next_question": str(preview.get("next_question") or ""),
            "unresolved_module_classifications": unresolved_modules,
            "pending_attachment_names": [
                str(item.get("file_name") or "附件")
                for item in session.pending_media[:10]
            ],
            "media_failures": [
                str(item.get("reason") or "读取失败")
                for item in session.media_failures[:10]
            ],
            "last_saved_pages": [
                str(item.get("label") or "知识库页面")
                for item in session.last_documents[:10]
            ],
            "group_recording_enabled": session.group_summary_enabled,
            "awaiting_followup": session.group_followup_pending,
        }

    async def _decide_dialogue(
        self,
        session: Session,
        text: str,
        *,
        is_group: bool,
    ) -> DialogueDecision | None:
        if self.dialogue_router is None or not text.strip():
            return None
        try:
            turn = await asyncio.to_thread(
                self.dialogue_router.route,
                text=text,
                context=self._dialogue_context(session, is_group=is_group),
                session_id=session.codex_dialogue_session_id,
            )
        except CodexDialogueError:
            LOGGER.exception("Codex dialogue routing failed; using local fallback")
            return None
        except Exception:
            LOGGER.exception("unexpected Codex dialogue routing failure; using local fallback")
            return None
        session.codex_dialogue_session_id = turn.session_id
        return turn.decision

    async def _start_processing_status(self, message: Any) -> str | None:
        method = getattr(self.channel, "add_typing_reaction", None)
        message_id = str(getattr(message, "message_id", "") or "")
        if not callable(method) or not message_id:
            return None
        try:
            result = await method(message_id)
        except Exception:
            LOGGER.warning("could not add processing reaction to message=%s", message_id)
            return None
        return str(result) if result else None

    async def _stop_processing_status(
        self, message: Any, reaction_id: str | None
    ) -> None:
        if not reaction_id:
            return
        method = getattr(self.channel, "remove_typing_reaction", None)
        message_id = str(getattr(message, "message_id", "") or "")
        if not callable(method) or not message_id:
            return
        try:
            await method(message_id, reaction_id)
        except Exception:
            LOGGER.warning("could not remove processing reaction from message=%s", message_id)

    def _discard_pending_media(self, session: Session) -> None:
        if self.media_staging is not None:
            self.media_staging.discard(session.pending_media)
        session.pending_media.clear()

    def _stage_downloaded_media(
        self, downloaded_media: Sequence[DownloadedMedia]
    ) -> list[dict[str, Any]]:
        if self.media_staging is None:
            return []
        staged: list[dict[str, Any]] = []
        try:
            for item in downloaded_media:
                staged.append(self.media_staging.stage(item))
        except MediaArchiveError:
            self.media_staging.discard(staged)
            raise
        return staged

    def _append_pending_media(
        self, session: Session, staged: Sequence[dict[str, Any]]
    ) -> None:
        existing = {
            str(item.get("archive_id") or "") for item in session.pending_media
        }
        for item in staged:
            archive_id = str(item.get("archive_id") or "")
            if archive_id in existing:
                if self.media_staging is not None:
                    self.media_staging.discard([item])
                continue
            session.pending_media.append(item)
            existing.add(archive_id)

    @staticmethod
    def _record_media_failure(
        session: Session,
        message: Any,
        reason: str,
    ) -> None:
        source_ref = _source_ref(message)
        if any(item.get("source_ref") == source_ref for item in session.media_failures):
            return
        session.media_failures.append(
            {
                "source_ref": source_ref,
                "reason": reason,
                "message_id": str(getattr(message, "message_id", "") or ""),
                "sender_id": str(getattr(message, "sender_id", "") or ""),
            }
        )

    @staticmethod
    def _resolve_media_failures_for_sender(session: Session, message: Any) -> None:
        sender_id = str(getattr(message, "sender_id", "") or "")
        if not sender_id:
            return
        session.media_failures = [
            item
            for item in session.media_failures
            if str(item.get("sender_id") or "") != sender_id
        ]

    @staticmethod
    def _attach_recovered_images(
        session: Session,
        source_ref: str,
        images: Sequence[str],
    ) -> None:
        for collection in (session.messages, session.group_buffer):
            for item in collection:
                if str(item.get("source_ref") or "") != source_ref:
                    continue
                existing = item.get("images")
                current = list(existing) if isinstance(existing, list) else []
                item["images"] = list(dict.fromkeys([*current, *images]))
                item["has_media"] = True
                item.pop("media_failed", None)

    async def _retry_group_work(self, session: Session, message: Any) -> None:
        failures = list(session.media_failures)
        recovered_images = 0
        fetch_message = getattr(self.channel, "fetch_inbound_message", None)
        for failure in failures:
            message_id = str(failure.get("message_id") or "")
            source_ref = str(failure.get("source_ref") or "")
            if not message_id or not callable(fetch_message):
                failure["reason"] = "当前飞书连接无法重新获取原消息，请重新发送原图。"
                continue
            try:
                original = await fetch_message(message_id)
                if original is None:
                    failure["reason"] = "飞书未返回原消息，请重新发送原图。"
                    continue
                images, downloaded = await self._download_images(original)
                if not images:
                    failure["reason"] = "原消息中没有可重新读取的图片，请重新发送原图。"
                    continue
                staged = self._stage_downloaded_media(downloaded)
                self._append_pending_media(session, staged)
                self._attach_recovered_images(session, source_ref, images)
            except (ImageInputError, MediaArchiveError) as exc:
                failure["reason"] = str(exc)
                continue
            except Exception as exc:
                LOGGER.warning(
                    "could not retry group image message=%s: %s", message_id, exc
                )
                failure["reason"] = "重新读取时飞书接口暂时失败，请稍后再试。"
                continue
            recovered_images += len(images)
            session.media_failures = [
                item
                for item in session.media_failures
                if str(item.get("source_ref") or "") != source_ref
            ]

        if session.group_buffer:
            candidates = [
                item
                for item in session.group_buffer
                if _is_group_profile_candidate(
                    str(item.get("content") or ""),
                    has_media=bool(item.get("images") or item.get("has_media")),
                )
            ]
            if session.group_followup_pending:
                existing_sources = {
                    str(item.get("source_ref") or "") for item in session.messages
                }
                session.messages = [
                    *session.messages,
                    *[
                        item
                        for item in candidates
                        if str(item.get("source_ref") or "") not in existing_sources
                    ],
                ]
            else:
                session.messages = candidates
        if not session.messages:
            await self._reply(message, "当前没有可重新处理的商户资料。")
            return

        # Retrying means all transient dependencies get another chance: Feishu
        # media, URL reading and the unresolved CRM lookup.
        if isinstance(session.crm_resolution, dict) and session.crm_resolution.get(
            "status"
        ) in {"not_found", "ambiguous"}:
            session.current_profile_loaded = False
        try:
            preview = await self._analyze_session(session)
        except ImageBatchLimitError as exc:
            await self._reply(message, str(exc))
            return
        except MerchantSwitchError:
            await self._reply(message, "重新分析识别到了不同商户，请先确认商户名称。")
            return
        except MerchantApiError:
            LOGGER.exception("retry group analysis failed")
            await self._reply(message, "已经重新尝试，但分析服务暂不可用；草稿仍保留。")
            return
        _assign_media_modules(session)
        session.clear_group_buffer()
        updates = preview.get("profile_updates")
        session.group_followup_pending = not (
            session.merchant
            and preview.get("save_readiness") == "ready"
            and isinstance(updates, list)
            and updates
        )
        if recovered_images:
            status = f"已重新读取 {recovered_images} 张图片，并重新检查链接、CRM 和摘要。"
        elif failures:
            status = "已重新请求原消息，并重新检查链接、CRM 和摘要。"
        else:
            status = "已重新检查链接、CRM 和摘要。"
        await self._reply(
            message,
            status
            + "\n\n"
            + _group_summary_text(preview)
            + _media_preview_suffix(session.pending_media)
            + _media_failure_suffix(session.media_failures),
        )

    async def _handle_group_dialogue_action(
        self,
        session: Session,
        message: Any,
        decision: DialogueDecision,
    ) -> bool:
        """Execute one Codex-selected action through existing guarded paths."""

        action = decision.action
        if action == "record":
            return False
        if action == "route_module":
            document_type = decision.document_type
            if document_type not in DOCUMENT_LABELS:
                await self._reply(message, "我仍不能确定对应页面，请再说明这是什么业务资料。")
                return True
            changed_facts = _apply_explicit_module_to_preview(session, document_type)
            routable = [
                item
                for item in session.pending_media
                if not isinstance(item.get("uploaded"), Mapping)
            ]
            for item in routable:
                item["target_document_type"] = document_type
            if not changed_facts and not routable:
                await self._reply(message, "当前没有等待确认归类的资料。")
                return True
            session.group_followup_pending = bool(
                (session.preview or {}).get("classification_review_required")
            )
            if session.group_save_pending and not session.group_followup_pending:
                await self._save(message, session, "结束并保存")
                return True
            details = []
            if changed_facts:
                details.append(f"{changed_facts} 条资料")
            if routable:
                details.append(f"{len(routable)} 个附件")
            await self._reply(
                message,
                f"已根据你的说明将{'和'.join(details)}归到“{DOCUMENT_LABELS[document_type]}”。"
                "需要写入时请 @机器人 回复“保存吧”。",
            )
            return True
        if action == "reply":
            await self._reply(
                message,
                decision.reply or "你可以补充商户资料，或让我总结、重试、保存当前草稿。",
            )
            return True
        if action == "summarize":
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
            return True
        if action == "retry":
            await self._retry_group_work(session, message)
            return True
        if action == "media_status":
            if session.media_failures:
                await self._reply(message, _media_failure_suffix(session.media_failures).strip())
            else:
                await self._reply(message, "当前草稿没有图片或附件读取失败记录。")
            return True
        if action == "help":
            await self._reply(
                message,
                "本群会静默收集商户资料；@我后可以自然地说“帮我总结一下”、"
                "“刚才哪个附件没读到”、“你再试一次”或“把文字和图片保存”。"
                "只有明确要求保存时才会写知识库。",
            )
            return True
        if action == "links":
            await self._reply_current_profile_links(message, session)
            return True
        if action == "pause":
            session.group_summary_enabled = False
            session.clear_group_buffer()
            await self._reply(message, "静默记录已暂停；需要恢复时直接 @我说继续记录。")
            return True
        if action == "resume":
            session.group_summary_enabled = True
            await self._reply(message, "本群已恢复静默记录；需要查看时直接 @我让我总结。")
            return True
        if action == "discard":
            self._discard_pending_media(session)
            session.reset_draft()
            session.clear_group_buffer()
            await self._reply(message, "已放弃当前摘要草稿，静默记录仍开启。")
            return True
        if action in {"save", "save_text_only"}:
            if action == "save_text_only":
                session.media_failures.clear()
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
                    return True
            await self._save(
                message,
                session,
                "仅保存文字" if action == "save_text_only" else "结束并保存",
            )
            return True
        return False

    async def _handle_direct_dialogue_action(
        self,
        *,
        key: str,
        session: Session,
        message: Any,
        decision: DialogueDecision,
    ) -> bool:
        action = decision.action
        if action == "record":
            return False
        if action == "route_module":
            document_type = decision.document_type
            if document_type not in DOCUMENT_LABELS:
                await self._reply(message, "我仍不能确定对应页面，请再说明这是什么业务资料。")
                return True
            changed_facts = _apply_explicit_module_to_preview(session, document_type)
            routable = [
                item
                for item in session.pending_media
                if not isinstance(item.get("uploaded"), Mapping)
            ]
            for item in routable:
                item["target_document_type"] = document_type
            if not changed_facts and not routable:
                await self._reply(message, "当前没有等待确认归类的资料。")
                return True
            details = []
            if changed_facts:
                details.append(f"{changed_facts} 条资料")
            if routable:
                details.append(f"{len(routable)} 个附件")
            await self._reply(
                message,
                f"已根据你的说明将{'和'.join(details)}归到“{DOCUMENT_LABELS[document_type]}”。"
                "回复“保存吧”即可写入。",
            )
            return True
        if action == "reply":
            await self._reply(
                message,
                decision.reply or "请补充要记录的商户资料，或告诉我想查看、重试还是保存。",
            )
            return True
        if action == "help":
            await self._reply(
                message,
                "直接发送商户名称、文字、截图、视频或文件即可。我会先形成草稿；"
                "你明确说保存后才会写入知识库。也可以自然地问我当前状态或让我重试。",
            )
            return True
        if action == "links":
            await self._reply_current_profile_links(message, session)
            return True
        if action == "media_status":
            if session.media_failures:
                await self._reply(message, _media_failure_suffix(session.media_failures).strip())
            else:
                await self._reply(message, "当前草稿没有图片或附件读取失败记录。")
            return True
        if action == "discard":
            self._discard_pending_media(session)
            self.sessions.discard(key)
            await self._reply(message, "已放弃，未写入知识库。")
            return True
        if action in {"save", "save_text_only"}:
            if action == "save_text_only":
                session.media_failures.clear()
            await self._save(
                message,
                session,
                "仅保存文字" if action == "save_text_only" else "结束并保存",
            )
            return True
        if action in {"summarize", "retry"}:
            if not session.messages:
                await self._reply(message, "当前还没有可整理的商户资料。")
                return True
            try:
                preview = await self._analyze_session(session)
            except (ImageBatchLimitError, MerchantSwitchError, MerchantApiError):
                LOGGER.exception("direct dialogue re-analysis failed")
                await self._reply(message, "重新分析暂时失败，草稿仍保留，请稍后再试。")
                return True
            await self._reply(
                message,
                _preview_text(preview) + _media_preview_suffix(session.pending_media),
            )
            return True
        if action in {"pause", "resume"}:
            await self._reply(message, "暂停或恢复记录只适用于群聊；私聊会一直保留当前草稿。")
            return True
        return False

    async def _download_resource_from_message_tree(
        self,
        message: Any,
        file_key: str,
        *,
        resource_type: str,
        media_label: str,
    ) -> tuple[bytes | None, Exception | None]:
        """Download a resource from its real owner with bounded transport retries.

        Feishu resource keys are scoped to the message that owns them. Replies,
        rich posts and merged forwards can expose a key on a wrapper message while
        the downloadable bytes remain attached to a parent/root message. Try all
        plausible owners, but only repeat an owner when the failure is transient.
        """

        owner_message_ids: list[str | None] = list(_resource_message_ids(message))
        if not owner_message_ids:
            owner_message_ids = [None]
        last_error: Exception | None = None
        for owner_index, owner_message_id in enumerate(owner_message_ids):
            for attempt in range(len(RESOURCE_DOWNLOAD_RETRY_DELAYS_SECONDS) + 1):
                try:
                    candidate = await self.channel.download_resource(
                        file_key,
                        resource_type=resource_type,
                        message_id=owner_message_id,
                    )
                except Exception as exc:
                    last_error = exc
                    LOGGER.warning(
                        "%s resource download raised for message=%s owner=%s "
                        "key=%s attempt=%s: %s",
                        media_label,
                        str(getattr(message, "message_id", "") or "unknown"),
                        owner_message_id or "none",
                        file_key,
                        attempt + 1,
                        exc,
                    )
                    # 234003 is deterministic for this owner. Move directly to
                    # the referenced parent/root instead of retrying the same ID.
                    if _is_resource_owner_mismatch(exc):
                        break
                    if not _is_transient_resource_error(exc):
                        break
                    if attempt >= len(RESOURCE_DOWNLOAD_RETRY_DELAYS_SECONDS):
                        break
                    await asyncio.sleep(
                        RESOURCE_DOWNLOAD_RETRY_DELAYS_SECONDS[attempt]
                    )
                    continue
                if isinstance(candidate, bytes) and candidate:
                    if owner_index or attempt:
                        LOGGER.info(
                            "%s resource recovered owner=%s key=%s attempt=%s",
                            media_label,
                            owner_message_id or "none",
                            file_key,
                            attempt + 1,
                        )
                    return candidate, last_error
                # The SDK returns no bytes for permanent API rejections such as
                # File-not-in-message. Try the next plausible owner immediately.
                break
        return None, last_error

    async def _download_images(
        self, message: Any
    ) -> tuple[list[str], list[DownloadedMedia]]:
        file_keys = _image_resource_keys(message)
        if not file_keys:
            return [], []
        if len(file_keys) > MAX_IMAGES_PER_MESSAGE:
            raise ImageInputError(f"每条消息最多支持 {MAX_IMAGES_PER_MESSAGE} 张图片。")
        image_data_urls: list[str] = []
        downloaded: list[DownloadedMedia] = []
        total_bytes = 0
        for ordinal, file_key in enumerate(file_keys):
            data, last_error = await self._download_resource_from_message_tree(
                message,
                file_key,
                resource_type="image",
                media_label="image",
            )
            if not isinstance(data, bytes) or not data:
                LOGGER.warning(
                    "image resource download returned no bytes for message=%s owners=%s key=%s",
                    str(getattr(message, "message_id", "") or "unknown"),
                    _resource_message_ids(message) or [None],
                    file_key,
                )
                if last_error is not None:
                    raise ImageInputError("图片下载失败，请重新发送原图。") from last_error
                raise ImageInputError("图片下载失败，请重新发送原图。")
            if len(data) > MAX_IMAGE_BYTES:
                raise ImageInputError("单张图片不能超过 4 MB。")
            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_IMAGE_BYTES_PER_ANALYSIS:
                raise ImageInputError(
                    "本条消息中的图片总大小不能超过 "
                    f"{MAX_TOTAL_IMAGE_BYTES_PER_ANALYSIS // 1024 // 1024} MB。"
                )
            mime_type = _image_mime_type(data)
            if mime_type is None:
                raise ImageInputError("仅支持 PNG、JPEG 或 WEBP 图片。")
            encoded = base64.b64encode(data).decode("ascii")
            image_data_urls.append(f"data:{mime_type};base64,{encoded}")
            downloaded.append(
                DownloadedMedia(
                    data=data,
                    file_name=_image_file_name(message, ordinal, mime_type),
                    media_type=mime_type,
                    source_ref=_source_ref(message),
                )
            )
        return image_data_urls, downloaded

    async def _download_video(
        self, message: Any
    ) -> tuple[VideoEvidence, DownloadedMedia] | None:
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
        data, last_error = await self._download_resource_from_message_tree(
            message,
            resource.file_key,
            resource_type="file",
            media_label="video",
        )
        if not isinstance(data, bytes) or not data:
            if last_error is not None:
                raise VideoProcessingError("视频下载失败，请稍后重试。") from last_error
            raise VideoProcessingError("视频下载失败，请重新发送。")
        if len(data) > self.maximum_video_bytes:
            raise VideoProcessingError(
                f"视频不能超过 {self.maximum_video_bytes // (1024 * 1024)} MB。"
            )
        try:
            evidence = await asyncio.to_thread(
                self.video_processor.process,
                data,
                file_name=resource.file_name,
                duration_ms=resource.duration_ms,
            )
            return evidence, DownloadedMedia(
                data=data,
                file_name=resource.file_name,
                media_type={
                    ".mp4": "video/mp4",
                    ".mov": "video/quicktime",
                    ".m4v": "video/x-m4v",
                    ".webm": "video/webm",
                }.get(Path(resource.file_name).suffix.lower(), "video/mp4"),
                source_ref=_source_ref(message),
            )
        except VideoProcessingError:
            raise
        except Exception as exc:
            LOGGER.exception("unexpected video processing failure")
            raise VideoProcessingError("服务器暂时无法处理视频，请稍后重试。") from exc

    async def _download_archive_files(self, message: Any) -> list[DownloadedMedia]:
        downloaded: list[DownloadedMedia] = []
        for resource in _archive_file_resources(message):
            data, last_error = await self._download_resource_from_message_tree(
                message,
                resource.file_key,
                resource_type="file",
                media_label="file",
            )
            if not isinstance(data, bytes) or not data:
                if last_error is not None:
                    raise MediaArchiveError("文件下载失败，请稍后重试。") from last_error
                raise MediaArchiveError("文件下载失败，请重新发送原文件。")
            if len(data) > MAX_ARCHIVE_FILE_BYTES:
                raise MediaArchiveError("单个归档文件不能超过 25 MB。")
            media_type = mimetypes.guess_type(resource.file_name)[0] or "application/octet-stream"
            downloaded.append(
                DownloadedMedia(
                    data=data,
                    file_name=resource.file_name,
                    media_type=media_type,
                    source_ref=_source_ref(message),
                )
            )
        return downloaded

    async def on_message(self, message: Any) -> None:
        raw_text = str(getattr(message, "body_text", "") or "").strip()
        text = _without_file_placeholders(
            _without_video_placeholders(_without_image_placeholders(raw_text))
        )
        has_video = _video_resource(message) is not None
        has_archive_file = bool(_archive_file_resources(message))
        image_keys = _image_resource_keys(message)
        if not raw_text and not image_keys and not has_video and not has_archive_file:
            return
        processing_reaction_id: str | None = None
        try:
            if _is_group_message(message) and (image_keys or has_video or has_archive_file):
                group_session = self.sessions.get(_session_key(message))
                if not group_session.group_summary_enabled:
                    if bool(getattr(message, "mentioned_bot", False)):
                        await self._reply(
                            message,
                            "本群记录已暂停。请 @机器人 回复“继续记录”恢复。",
                        )
                    return
            should_show_processing = bool(
                not _is_group_message(message)
                or getattr(message, "mentioned_bot", False)
                or _is_group_profile_candidate(
                    text,
                    has_media=bool(image_keys or has_video or has_archive_file),
                )
            )
            if should_show_processing:
                processing_reaction_id = await self._start_processing_status(message)
            media_failure: str | None = None
            try:
                # Native Feishu video messages expose a poster image in their raw
                # payload. The video processor already produces representative key
                # frames, so treating that poster as a separate original attachment
                # creates a misleading duplicate beside the actual video.
                if has_video:
                    images, downloaded_media = [], []
                else:
                    images, downloaded_media = await self._download_images(message)
            except ImageInputError as exc:
                if not _is_group_message(message):
                    # A normal URL preview can expose an inaccessible thumbnail.
                    # Keep useful direct-message text, while image-only messages
                    # still fail explicitly instead of pretending to be read.
                    if not text and not has_video:
                        await self._reply(message, str(exc))
                        return
                    LOGGER.warning(
                        "ignoring unavailable optional direct-message image for message=%s",
                        str(getattr(message, "message_id", "") or "unknown"),
                    )
                    images = []
                    downloaded_media = []
                else:
                    # Group collection stays silent, but failed evidence is retained
                    # as a blocking state and surfaced on the next explicit summary
                    # or save. It must never disappear behind a successful save.
                    media_failure = str(exc)
                    LOGGER.warning(
                        "recording unavailable group image for message=%s",
                        str(getattr(message, "message_id", "") or "unknown"),
                    )
                    images = []
                    downloaded_media = []
            video = None
            if has_video:
                try:
                    video_download = await self._download_video(message)
                except VideoProcessingError as exc:
                    LOGGER.warning(
                        "video processing rejected message=%s: %s",
                        str(getattr(message, "message_id", "") or "unknown"),
                        exc,
                    )
                    await self._reply(message, f"视频处理失败：{exc}")
                    return
                if video_download is not None:
                    video, downloaded_video = video_download
                    downloaded_media.append(downloaded_video)
            if has_archive_file:
                try:
                    downloaded_media.extend(await self._download_archive_files(message))
                except MediaArchiveError as exc:
                    await self._reply(message, f"文件处理失败：{exc}")
                    return
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
            if downloaded_media and not text:
                names = "、".join(item.file_name for item in downloaded_media[:5])
                text = f"用户上传了待归档文件：{names}。只记录文件元数据，不推断文件内容。"
            if not text and not media_failure:
                return
            if _is_group_message(message):
                await self._on_group_message(
                    message,
                    text,
                    images,
                    downloaded_media,
                    media_failure=media_failure,
                )
                return
            await self._on_direct_message(message, text, images, downloaded_media)
        finally:
            await self._stop_processing_status(message, processing_reaction_id)
            try:
                self.sessions.persist()
            except OSError:
                LOGGER.exception("could not persist Feishu session state")

    async def _on_direct_message(
        self,
        message: Any,
        text: str,
        images: list[str],
        downloaded_media: list[DownloadedMedia],
    ) -> None:
        key = _session_key(message)
        if text in HELP_COMMANDS:
            await self._reply(
                message,
                "发送商户名称和要记录的资料。"
                "\n- 可直接发送截图，我会提取其中明确可见的有用信息"
                "\n- 可发送 5 分钟、25 MB 以内的视频，我会转写语音并读取关键帧"
                "\n- 可发送 25 MB 以内的 PDF、Office、CSV、TXT、RTF 或 ZIP 文件归档"
                "\n- 可说“没问题，保存吧”或“结束并保存”"
                "\n- /discard：放弃草稿",
            )
            return
        if text in DISCARD_COMMANDS:
            session = self.sessions.get(key)
            self._discard_pending_media(session)
            self.sessions.discard(key)
            await self._reply(message, "已放弃，未写入知识库。")
            return

        session = self.sessions.get(key)
        if _is_link_request(text):
            await self._reply_current_profile_links(message, session)
            return
        explicit_module = _explicit_media_module(text)
        if explicit_module and (
            session.pending_media
            or bool((session.preview or {}).get("classification_review_required"))
        ):
            changed_facts = _apply_explicit_module_to_preview(session, explicit_module)
            routable = [
                item
                for item in session.pending_media
                if not isinstance(item.get("uploaded"), Mapping)
            ]
            for item in routable:
                item["target_document_type"] = explicit_module
            details = []
            if changed_facts:
                details.append(f"{changed_facts} 条资料")
            if routable:
                details.append(f"{len(routable)} 个附件")
            await self._reply(
                message,
                f"已将{'和'.join(details)}归到“{DOCUMENT_LABELS[explicit_module]}”。"
                "回复“保存吧”即可写入。",
            )
            return
        if _is_save_confirmation(text):
            await self._save(message, session, "结束并保存")
            return

        decision = await self._decide_dialogue(session, text, is_group=False)
        if decision is not None and await self._handle_direct_dialogue_action(
            key=key,
            session=session,
            message=message,
            decision=decision,
        ):
            return

        user_message: dict[str, Any] = {
            "role": "user",
            "content": text,
            "source_ref": _source_ref(message),
        }
        if images:
            user_message["images"] = images
        if downloaded_media:
            user_message["has_media"] = True
        session.messages.append(user_message)
        # Assistant previews are not needed by the extractor. Keeping only user
        # turns doubles useful conversation capacity and avoids the API's
        # 40-message limit during longer staff tests.
        if len(session.messages) > 40:
            session.messages = session.messages[-40:]
        try:
            preview = await self._analyze_session(session)
            if downloaded_media:
                self._append_pending_media(
                    session, self._stage_downloaded_media(downloaded_media)
                )
                _assign_media_modules(session)
            reply_text = _preview_text(preview) + _media_preview_suffix(session.pending_media)
            await self._reply(message, reply_text)
        except MediaArchiveError as exc:
            session.messages.pop()
            await self._reply(message, f"附件暂存失败：{exc}")
        except ImageBatchLimitError as exc:
            session.messages.pop()
            await self._reply(message, str(exc))
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
        crm_unresolved = isinstance(session.crm_resolution, dict) and session.crm_resolution.get(
            "status"
        ) in {"not_found", "ambiguous"}
        if session.merchant and (not session.current_profile_loaded or crm_unresolved):
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
        analysis_profile = _profile_for_analysis(session.current_profile)
        if session.merchant:
            existing_identity = analysis_profile.get("merchant")
            identity = dict(existing_identity) if isinstance(existing_identity, dict) else {}
            identity.update(session.merchant)
            analysis_profile["merchant"] = identity
        preview = await self._analyze_in_batches(session.messages, analysis_profile)
        merchant = preview.get("merchant")
        if isinstance(merchant, dict) and merchant.get("name"):
            previous_name = session.merchant.get("name") if session.merchant else None
            previous_id = session.merchant.get("id") if session.merchant else None
            proposed_name = str(merchant.get("name"))
            is_switch = bool(
                previous_name
                and _merchant_name_key(previous_name) != _merchant_name_key(proposed_name)
            )
            if is_switch:
                explicit_switch = _explicitly_mentions_merchant(
                    session.messages, proposed_name
                )
                has_unsaved_draft = bool(
                    session.preview
                    and (
                        session.preview.get("profile_updates")
                        or session.preview.get("conflicts")
                    )
                )
                if not explicit_switch:
                    # The analyzer found a different merchant but the evidence
                    # does not explicitly anchor that identity. Never silently
                    # route it to the previously active merchant: a false prompt
                    # is recoverable, while a cross-merchant write is not.
                    raise MerchantSwitchError
                elif has_unsaved_draft or session.group_followup_pending:
                    raise MerchantSwitchError
                else:
                    # A clearly named merchant after a completed save starts a
                    # new draft automatically; no manual /discard is needed.
                    previous_id = None
                    session.current_profile = {}
                    session.current_profile_loaded = False
                    session.crm_resolution = None
            proposed_id = merchant.get("id") or (None if is_switch else previous_id)
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
                    preview = await self._analyze_in_batches(
                        session.messages,
                        {
                            **session.current_profile,
                            "merchant": dict(session.merchant),
                        },
                    )
        if session.merchant:
            preview["merchant"] = dict(session.merchant)
        self._apply_crm_gate(session, preview)
        session.preview = preview
        return preview

    async def _analyze_in_batches(
        self,
        messages: list[dict[str, Any]],
        current_profile: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Analyze every image in bounded calls and merge one coherent preview."""
        batches = _message_batches_for_analysis(messages)
        working_profile = copy.deepcopy(dict(current_profile))
        working_fields = working_profile.get("fields")
        if not isinstance(working_fields, dict):
            working_fields = {}
            working_profile["fields"] = working_fields
        previews: list[dict[str, Any]] = []
        for batch in batches:
            preview = await self._post(
                "/v1/profile/analyze",
                {
                    "messages": batch,
                    "current_profile": working_profile,
                },
            )
            previews.append(preview)
            merchant = preview.get("merchant")
            if isinstance(merchant, Mapping) and merchant.get("name"):
                working_profile["merchant"] = copy.deepcopy(dict(merchant))
            updates = preview.get("profile_updates")
            if not isinstance(updates, list):
                continue
            for update in updates:
                if not isinstance(update, Mapping):
                    continue
                field_path = update.get("field_path")
                value = update.get("value")
                if (
                    isinstance(field_path, str)
                    and value is not None
                    and not _is_password_field(field_path)
                ):
                    working_fields[field_path] = {"value": value}
        return _merge_analysis_previews(previews)

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
        self,
        message: Any,
        text: str,
        images: list[str],
        downloaded_media: list[DownloadedMedia],
        *,
        media_failure: str | None = None,
    ) -> None:
        key = _session_key(message)
        session = self.sessions.get(key)
        mentioned = bool(getattr(message, "mentioned_bot", False))
        await self._ensure_group_identity(session, message)
        save_request_evidence = _save_request_evidence(text) if mentioned else None

        explicit_module = _explicit_media_module(text) if mentioned else None
        if explicit_module and (
            session.pending_media
            or bool((session.preview or {}).get("classification_review_required"))
        ):
            changed_facts = _apply_explicit_module_to_preview(session, explicit_module)
            routable = [
                item
                for item in session.pending_media
                if not isinstance(item.get("uploaded"), Mapping)
            ]
            for item in routable:
                item["target_document_type"] = explicit_module
            details = []
            if changed_facts:
                details.append(f"{changed_facts} 条资料")
            if routable:
                details.append(f"{len(routable)} 个附件")
            await self._reply(
                message,
                f"已将{'和'.join(details)}归到“{DOCUMENT_LABELS[explicit_module]}”。"
                "需要写入时请 @机器人 回复“保存吧”。",
            )
            return

        if mentioned and _is_group_pause_intent(text):
            session.group_summary_enabled = False
            session.clear_group_buffer()
            await self._reply(message, "静默记录已暂停；恢复时请 @机器人 回复“继续记录”。")
            return
        if mentioned and _is_group_enable_intent(text):
            session.group_summary_enabled = True
            await self._reply(
                message,
                "本群已开启静默记录：普通消息只进入草稿，不会自动发言。"
                "需要查看时请 @机器人 回复“总结吧”；"
                "只有明确 @机器人 回复“保存吧”才会写入知识库。",
            )
            return
        if not session.group_summary_enabled:
            if mentioned and _matches_command(text, HELP_COMMANDS):
                await self._reply(message, "本群记录已暂停。请 @机器人 回复“继续记录”恢复。")
            elif mentioned:
                await self._reply(
                    message,
                    "本群记录已暂停。请 @机器人 回复“继续记录”恢复。",
                )
            return
        if mentioned and _is_media_status_request(text):
            if session.media_failures:
                await self._reply(
                    message,
                    _media_failure_suffix(session.media_failures).strip(),
                )
            else:
                await self._reply(message, "当前草稿没有图片或附件读取失败记录。")
            return
        if mentioned and _is_retry_request(text):
            await self._retry_group_work(session, message)
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
                "本群默认静默记录，普通消息只进入草稿且不会自动发言。可用："
                "@机器人 总结吧、@机器人 暂停记录、@机器人 保存吧、"
                "@机器人 再试一下、@机器人 /discard。图片读取失败时可让机器人重试、"
                "重新发原图，或回复"
                "“仅保存文字”。只有保存吧才会写知识库。",
            )
            return
        if mentioned and _matches_command(text, DISCARD_COMMANDS):
            self._discard_pending_media(session)
            session.reset_draft()
            session.clear_group_buffer()
            await self._reply(message, "已放弃当前摘要草稿，静默记录仍开启。")
            return
        if _is_link_request(text):
            await self._reply_current_profile_links(message, session)
            return
        if save_request_evidence:
            # Preserve the content before a trailing save command as evidence.
            # The normal buffering path below will analyze it together with the
            # existing draft, retain same-message attachments, and then perform
            # the guarded save once merchant/CRM checks succeed.
            text = save_request_evidence
            session.group_followup_pending = True
            session.group_save_pending = True
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
        if mentioned and _matches_command(text, TEXT_ONLY_SAVE_COMMANDS):
            session.media_failures.clear()
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
            await self._save(message, session, "仅保存文字")
            return
        if mentioned and not save_request_evidence:
            decision = await self._decide_dialogue(session, text, is_group=True)
            if decision is not None and await self._handle_group_dialogue_action(
                session,
                message,
                decision,
            ):
                return
        if not mentioned and (
            _is_group_save_confirmation(text)
            or _matches_command(text, GROUP_ENABLE_COMMANDS)
            or _matches_command(text, GROUP_PAUSE_COMMANDS)
            or _matches_command(text, GROUP_SUMMARIZE_COMMANDS)
            or _matches_command(text, DISCARD_COMMANDS)
        ):
            return
        failed_profile_media = bool(
            media_failure
            and (
                not text
                or _is_group_profile_candidate(text, has_media=False)
            )
        )
        if not _is_effective_group_message(text) and not failed_profile_media:
            if mentioned:
                await self._reply(
                    message,
                    "我没理解这句话。可以说：总结吧、保存吧、再试一下，或帮助。",
                )
            return
        if not (session.group_followup_pending and mentioned) and not (
            _is_group_profile_candidate(
                text, has_media=bool(images or downloaded_media)
            )
            or failed_profile_media
        ):
            if mentioned:
                await self._reply(
                    message,
                    "我没理解这句话。可以说：总结吧、保存吧、再试一下，或帮助。",
                )
            return

        sender_name = str(getattr(message, "sender_name", "") or "").strip()
        if not sender_name:
            sender_id = str(getattr(message, "sender_id", "") or "").strip()
            if sender_id:
                sender_name = f"成员[{sender_id[-8:]}]"
        message_text = text or "用户上传了一张未能读取的图片。"
        content = f"{sender_name}：{message_text}" if sender_name else message_text
        user_message: dict[str, Any] = {
            "role": "user",
            "content": content,
            "source_ref": _source_ref(message),
        }
        if images:
            user_message["images"] = images
        if downloaded_media:
            user_message["has_media"] = True
        if failed_profile_media:
            user_message["has_media"] = True
            user_message["media_failed"] = True
            self._record_media_failure(session, message, str(media_failure))
        if downloaded_media:
            try:
                self._append_pending_media(
                    session, self._stage_downloaded_media(downloaded_media)
                )
                # A successful image from the same sender resolves that sender's
                # unavailable-image state. Failures belonging to other members
                # stay visible as independent retry items.
                self._resolve_media_failures_for_sender(session, message)
            except MediaArchiveError as exc:
                await self._reply(message, f"附件暂存失败：{exc}")
                return
        session.group_buffer.append(user_message)
        session.group_pending_count += 1
        session.group_generation += 1
        session.group_last_message = message
        if failed_profile_media and not mentioned:
            await self._reply(
                message,
                "这条图片消息未能完整读取："
                + str(media_failure)
                + " 草稿已保留，不会静默漏写。",
            )
            return
        if mentioned:
            session.cancel_idle_task()
            if failed_profile_media:
                await self._reply(
                    message,
                    _media_failure_suffix(session.media_failures).strip(),
                )
                return
            if session.group_followup_pending:
                save_pending = session.group_save_pending
                analyzed = await self._summarize_group_followup(
                    session,
                    message,
                    reply_on_success=not save_pending,
                )
                if analyzed and save_pending and not session.group_followup_pending:
                    await self._save(message, session, "结束并保存")
            return
        if session.group_followup_pending:
            # Keep nearby, unmentioned corrections so the next explicit @bot
            # follow-up can reconcile them with the existing unsaved summary.
            return
        # Group collection is intentionally silent. Threshold and idle settings
        # remain accepted for deployment compatibility but never trigger a
        # summary; only an explicit @bot summary/save command does.
        return

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
        segment = [
            item
            for item in session.group_buffer
            if _is_group_profile_candidate(
                str(item.get("content") or ""),
                has_media=bool(item.get("images") or item.get("has_media")),
            )
        ]
        if not segment:
            session.clear_group_buffer()
            await self._reply(message, "这段没有识别到可记录的商户资料，未写入知识库。")
            return False
        if len(segment) != len(session.group_buffer):
            session.group_buffer = segment
            session.group_pending_count = len(segment)
        session.messages = segment
        try:
            preview = await self._analyze_session(session)
        except ImageBatchLimitError as exc:
            await self._reply(message, str(exc))
            return False
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
        _assign_media_modules(session)
        session.clear_group_buffer()
        if reply_on_success or session.group_followup_pending:
            await self._reply(
                message,
                _group_summary_text(preview)
                + _media_preview_suffix(session.pending_media)
                + _media_failure_suffix(session.media_failures),
            )
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
        except ImageBatchLimitError as exc:
            session.messages = previous_messages
            await self._reply(message, str(exc))
            return False
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
        _assign_media_modules(session)
        session.clear_group_buffer()
        updates = preview.get("profile_updates")
        session.group_followup_pending = not (
            session.merchant
            and preview.get("save_readiness") == "ready"
            and isinstance(updates, list)
            and updates
        )
        if reply_on_success or session.group_followup_pending:
            await self._reply(
                message,
                _group_summary_text(preview)
                + _media_preview_suffix(session.pending_media)
                + _media_failure_suffix(session.media_failures),
            )
        return True

    async def _save(self, message: Any, session: Session, confirmation: str) -> None:
        # Failed Feishu media must not silently disappear, but it also should
        # not hold already usable text and successfully downloaded attachments
        # hostage. Preserve both the failure records and their source messages
        # across a successful partial save so a later "再试一下" can
        # fetch, analyze, and append the missing evidence.
        deferred_media_failures = copy.deepcopy(session.media_failures)
        deferred_source_refs = {
            str(item.get("source_ref") or "")
            for item in deferred_media_failures
            if item.get("source_ref")
        }
        deferred_retry_messages = [
            copy.deepcopy(item)
            for item in [*session.messages, *session.group_buffer]
            if str(item.get("source_ref") or "") in deferred_source_refs
        ]
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
            except ImageBatchLimitError as exc:
                await self._reply(message, str(exc))
                return
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
        _assign_media_modules(session)
        unresolved_media = _unresolved_media(session)
        if unresolved_media:
            names = "、".join(str(item.get("file_name") or "附件") for item in unresolved_media[:5])
            await self._reply(
                message,
                "附件尚未确定对应模块，暂未写入："
                + names
                + "。请 @机器人 回复“附件归到菜单/硬件设备/账号与密码”等明确模块。",
            )
            return
        preview = session.preview or {}
        merchant = session.merchant
        raw_updates = preview.get("profile_updates")
        updates = list(raw_updates) if isinstance(raw_updates, list) else []
        conflicts = preview.get("conflicts")
        has_conflicts = isinstance(conflicts, list) and bool(conflicts)
        classification_ready = not bool(preview.get("classification_review_required"))
        crm_ready = not isinstance(resolution, dict) or resolution.get("status") in {
            "unique",
            "disabled",
        }
        media_updates = _pending_media_updates(session)
        media_ready = (
            bool(session.pending_media)
            and bool(merchant)
            and crm_ready
            and not has_conflicts
            and classification_ready
        )
        profile_ready = (
            preview.get("save_readiness") == "ready"
            and bool(updates)
            and not has_conflicts
            and classification_ready
        )
        if not merchant or not (profile_ready or media_ready):
            if _is_group_message(message):
                session.group_followup_pending = True
                session.group_save_pending = True
                if has_conflicts:
                    lines = [
                        f"- {_field_label(item.get('field_path'))}："
                        f"{_display_value(item.get('field_path'), item.get('current_value'))} → "
                        f"{_display_value(item.get('field_path'), item.get('proposed_value'))}"
                        for item in conflicts[:10]
                        if isinstance(item, dict)
                    ]
                    await self._reply(
                        message,
                        "检测到知识库现有资料不同，暂未写入：\n"
                        + "\n".join(lines)
                        + "\n如需用新值替换，请 @机器人 回复“确认替换”。",
                    )
                elif not classification_ready:
                    await self._reply(
                        message,
                        str(
                            preview.get("next_question")
                            or "资料模块尚未确认，暂未写入。请明确回复要归入的模块。"
                        ),
                    )
                elif not merchant:
                    group_hint = str(session.group_merchant_hint or "").strip()
                    group_resolution = session.crm_resolution or {}
                    group_status = group_resolution.get("status")
                    if group_hint and group_status == "ambiguous":
                        await self._reply(
                            message,
                            f"已从群名识别到“{group_hint}”，但 CRM 匹配到多个客户，"
                            "暂未写入。请 @机器人 补充准确商户名称或店铺 ID。",
                        )
                    elif group_hint and group_status == "not_found":
                        await self._reply(
                            message,
                            f"已从群名识别到“{group_hint}”，但 CRM 未找到对应客户，"
                            "暂未写入。请 @机器人 补充准确商户名称或店铺 ID。",
                        )
                    else:
                        await self._reply(
                            message,
                            "未识别到商户名称，暂未写入。请 @机器人 补充商户名称。",
                        )
                elif not updates and not session.pending_media:
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
        messages = [
            {
                key: item[key]
                for key in ("role", "content", "source_ref")
                if key in item
            }
            for item in session.messages
        ]

        async def verify_updates_present(
            submitted_updates: Sequence[Mapping[str, Any]],
        ) -> bool:
            try:
                readback = await self._post(
                    "/v1/profile/read",
                    {
                        "merchant_name": merchant["name"],
                        "merchant_id": merchant.get("id"),
                    },
                )
            except MerchantApiError:
                LOGGER.warning(
                    "merchant profile commit readback unavailable",
                    exc_info=True,
                )
                session.current_profile_loaded = False
                return False
            profile = readback.get("profile")
            if not isinstance(profile, dict) or not _profile_contains_updates(
                profile, submitted_updates
            ):
                session.current_profile_loaded = False
                return False
            session.current_profile = profile
            self._apply_crm_resolution(session, readback.get("crm"))
            session.current_profile_loaded = True
            return True

        combined_updates = [*updates, *media_updates]
        first_result: dict[str, Any] | None = None
        first_store: dict[str, Any] = {}
        first_verified_by_readback = False
        documents: list[dict[str, str]] = []
        if combined_updates:
            try:
                first_result = await self._post(
                    "/v1/profile/commit",
                    {
                        "confirmation": confirmation,
                        "conversation_id": f"feishu:{session.conversation_id}",
                        "merchant": merchant,
                        "messages": messages,
                        "updates": combined_updates,
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
                    await self._reply(
                        message,
                        "保存失败，草稿和原文件已保留。请稍后 @机器人 回复“保存吧”重试；"
                        "无需重新发送资料或附件。",
                    )
                return
            first_store = (
                first_result.get("result")
                if isinstance(first_result.get("result"), dict)
                else {}
            )
            documents = _document_links(first_store)
            session.media_module_node_tokens.update(_document_node_tokens(first_store))
            if not _commit_acknowledged(first_result):
                first_verified_by_readback = await verify_updates_present(combined_updates)
                if not first_verified_by_readback:
                    await self._reply(
                        message,
                        "知识库写入未完成，草稿和原文件已保留。"
                        "请稍后 @机器人 回复“保存吧”重试；无需重新发送资料或附件。",
                    )
                    return

        archived_count = 0
        if session.pending_media:
            current = _current_profile_values(session)
            outstanding = [
                item
                for item in session.pending_media
                if not current.get(f"{_media_field_base(item)}.url")
            ]
            archived_count = len(session.pending_media) - len(outstanding)
            if outstanding:
                missing_modules = sorted(
                    {
                        str(item.get("target_document_type") or "")
                        for item in outstanding
                        if not session.media_module_node_tokens.get(
                            str(item.get("target_document_type") or "")
                        )
                    }
                )
                if self.media_archiver is None or missing_modules:
                    session.current_profile_loaded = False
                    session.preview = None
                    await self._reply(
                        message,
                        "文字资料已写入，但未找到附件对应模块页面；"
                        "草稿和原文件已保留，请稍后再次回复“保存吧”。",
                    )
                    return
                uploaded_updates: list[dict[str, Any]] = []
                try:
                    for item in outstanding:
                        uploaded = item.get("uploaded")
                        if not isinstance(uploaded, Mapping):
                            uploaded = await asyncio.to_thread(
                                self.media_archiver.archive,
                                item,
                                document_token=session.media_module_node_tokens[
                                    str(item["target_document_type"])
                                ],
                            )
                            item["uploaded"] = dict(uploaded)
                        uploaded_updates.extend(_archived_media_updates(item, uploaded))
                except MediaArchiveError as exc:
                    LOGGER.exception("merchant media archive upload failed")
                    session.current_profile_loaded = False
                    session.preview = None
                    await self._reply(
                        message,
                        f"文字资料已写入，但附件归档失败：{exc} 草稿和原文件已保留，可稍后重试。",
                    )
                    return
                try:
                    media_result = await self._post(
                        "/v1/profile/commit",
                        {
                            "confirmation": confirmation,
                            "conversation_id": f"feishu:{session.conversation_id}:media",
                            "merchant": merchant,
                            "messages": messages,
                            "updates": uploaded_updates,
                        },
                    )
                except MerchantApiError:
                    LOGGER.exception("merchant media link commit failed")
                    session.current_profile_loaded = False
                    session.preview = None
                    await self._reply(
                        message,
                        "附件已上传，但对应模块的链接写入失败；草稿已保留，可再次回复“保存吧”重试。",
                    )
                    return
                media_store = (
                    media_result.get("result")
                    if isinstance(media_result.get("result"), dict)
                    else {}
                )
                if not _commit_acknowledged(media_result):
                    if not await verify_updates_present(uploaded_updates):
                        session.current_profile_loaded = False
                        session.preview = None
                        await self._reply(
                            message,
                            "附件已经上传，但链接尚未写入对应模块。"
                            "草稿和原文件已保留，请稍后 @机器人 回复“保存吧”重试；"
                            "无需重新上传附件。",
                        )
                        return
                archived_count += len(outstanding)
                media_documents = _document_links(media_store)
                if media_documents:
                    documents = media_documents

        if documents:
            session.last_documents = documents
        duplicate = (
            "（此前已保存，本次未重复写入）"
            if (first_store.get("duplicate") or first_verified_by_readback)
            and not archived_count
            else ""
        )
        if documents:
            reply_text = "已写入：\n" + "\n".join(
                f"- {item['label']}：{item['url']}" for item in documents
            )
        else:
            reply_text = f"已写入知识库。{duplicate}"
        if archived_count:
            reply_text += f"\n- 已保存到对应模块的附件：{archived_count} 个"
        if deferred_media_failures:
            reply_text += _partial_save_media_failure_suffix(deferred_media_failures)
        self._discard_pending_media(session)
        await self._reply(message, reply_text)
        session.reset_draft(preserve_merchant=_is_group_message(message))
        if deferred_media_failures:
            session.media_failures = deferred_media_failures
            session.messages = deferred_retry_messages
