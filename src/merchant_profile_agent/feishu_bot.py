from __future__ import annotations

import asyncio
import base64
import copy
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
from .media_archive import (
    DownloadedMedia,
    FeishuDriveArchiver,
    MediaArchiveError,
    MediaStagingStore,
)


LOGGER = logging.getLogger("merchant_profile_agent.feishu_bot")
MAX_IMAGES_PER_MESSAGE = 4
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 6 * 1024 * 1024
SAVE_COMMANDS = {"结束并保存", "/save"}
DISCARD_COMMANDS = {"放弃本次对话", "/discard"}
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
    pending_media: list[dict[str, Any]] = field(default_factory=list)
    media_archive_node_token: str | None = None
    # Group collection is long-running by default. It only writes after an
    # explicit @bot save command; this flag is the user's persisted pause state.
    group_summary_enabled: bool = True
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
        self.pending_media.clear()
        self.media_archive_node_token = None
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
        "media_archive_node_token",
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
            "version": 2,
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
    normalized = _normalize_intent_text(text)
    return normalized == "/save" or normalized in NATURAL_SAVE_CONFIRMATIONS


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


def _image_file_name(message: Any, ordinal: int, media_type: str) -> str:
    content = getattr(message, "content", None)
    supplied = _resource_value(content, "file_name") or _resource_value(content, "name")
    if isinstance(supplied, str) and supplied.strip():
        return supplied.strip()
    extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[
        media_type
    ]
    message_id = re.sub(
        r"[^A-Za-z0-9_-]+", "_", str(getattr(message, "message_id", "image"))
    )[:80]
    suffix = f"-{ordinal + 1}" if ordinal else ""
    return f"screenshot-{message_id}{suffix}{extension}"


def _media_preview_suffix(items: Sequence[Mapping[str, Any]]) -> str:
    if not items:
        return ""
    names = [str(item.get("file_name") or "附件") for item in items[:5]]
    remainder = len(items) - len(names)
    line = "待归档附件：" + "、".join(names)
    if remainder > 0:
        line += f"，另有 {remainder} 个"
    return f"\n\n{line}\n回复“保存吧”后会上传到 15｜资料归档。"


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


def _pending_media_updates(session: Session) -> list[dict[str, Any]]:
    current = _current_profile_values(session)
    updates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in session.pending_media:
        archive_id = str(item.get("archive_id") or "")
        if (
            not re.fullmatch(r"media_[a-f0-9]{16}", archive_id)
            or archive_id in seen
        ):
            continue
        seen.add(archive_id)
        source_ref = str(item.get("source_ref") or "feishu://unknown")
        values = {
            "name": str(item.get("file_name") or "附件"),
            "media_type": str(item.get("media_type") or "application/octet-stream"),
            "size_bytes": str(item.get("size_bytes") or "0"),
            "sha256": str(item.get("sha256") or ""),
            "source_ref": source_ref,
            "status": "待上传",
        }
        for suffix, value in values.items():
            field_path = f"documents.{archive_id}.{suffix}"
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
    source_ref = str(item.get("source_ref") or "feishu://unknown")
    values = {
        "url": str(uploaded["url"]),
        "file_token": str(uploaded["file_token"]),
        "status": "已归档",
    }
    return [
        {
            "field_path": f"documents.{archive_id}.{suffix}",
            "value": value,
            "source_ref": source_ref,
            "replace_confirmed": suffix == "status",
            "state": "explicit",
        }
        for suffix, value in values.items()
    ]


def _archive_wiki_token(store_result: Mapping[str, Any]) -> str | None:
    documents = store_result.get("documents")
    if not isinstance(documents, list):
        return None
    for item in documents:
        if (
            isinstance(item, Mapping)
            and item.get("document_type") == "document-archive"
            and isinstance(item.get("node_token"), str)
        ):
            return str(item["node_token"])
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
        media_staging: MediaStagingStore | None = None,
        media_archiver: FeishuDriveArchiver | None = None,
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

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self.api.post, path, payload)

    async def _reply(self, message: Any, text: str) -> None:
        # Plain text prevents user-supplied values from becoming active markdown,
        # links, or mentions when a preview is echoed into a group chat.
        await self.channel.reply(message, {"text": text})

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

    async def on_message(self, message: Any) -> None:
        raw_text = str(getattr(message, "body_text", "") or "").strip()
        text = _without_video_placeholders(_without_image_placeholders(raw_text))
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
                            "本群记录已暂停。请 @机器人 回复“继续记录”恢复。",
                        )
                    return
            try:
                images, downloaded_media = await self._download_images(message)
            except ImageInputError as exc:
                # Link previews and quoted rich posts may expose an image_key
                # that Feishu refuses with "File not in msg". Keep valid text
                # or video instead of failing the entire message.
                if not text and not has_video:
                    await self._reply(message, str(exc))
                    return
                LOGGER.warning(
                    "ignoring unavailable optional image for text message=%s",
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
                await self._on_group_message(message, text, images, downloaded_media)
                return
            await self._on_direct_message(message, text, images, downloaded_media)
        finally:
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
            if downloaded_media:
                self._append_pending_media(
                    session, self._stage_downloaded_media(downloaded_media)
                )
            reply_text = _preview_text(preview) + _media_preview_suffix(session.pending_media)
            await self._reply(message, reply_text)
        except MediaArchiveError as exc:
            session.messages.pop()
            await self._reply(message, f"附件暂存失败：{exc}")
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
        preview = await self._post(
            "/v1/profile/analyze",
            {
                "messages": _messages_for_analysis(session.messages),
                "current_profile": analysis_profile,
            },
        )
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
                    # Pronouns such as “他的地址” belong to the active merchant.
                    merchant = {**merchant, "name": previous_name, "id": previous_id}
                    preview["merchant"] = merchant
                    proposed_name = str(previous_name)
                    is_switch = False
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
                    preview = await self._post(
                        "/v1/profile/analyze",
                        {
                            "messages": _messages_for_analysis(session.messages),
                            "current_profile": {
                                **session.current_profile,
                                "merchant": dict(session.merchant),
                            },
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
        self,
        message: Any,
        text: str,
        images: list[str],
        downloaded_media: list[DownloadedMedia],
    ) -> None:
        key = _session_key(message)
        session = self.sessions.get(key)
        mentioned = bool(getattr(message, "mentioned_bot", False))

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
                "@机器人 /discard。只有保存吧才会写知识库。",
            )
            return
        if mentioned and _matches_command(text, DISCARD_COMMANDS):
            self._discard_pending_media(session)
            session.reset_draft()
            session.clear_group_buffer()
            await self._reply(message, "已放弃当前摘要草稿，静默记录仍开启。")
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
        if not (session.group_followup_pending and mentioned) and not _is_group_profile_candidate(
            text, has_media=bool(images)
        ):
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
        if downloaded_media:
            try:
                self._append_pending_media(
                    session, self._stage_downloaded_media(downloaded_media)
                )
            except MediaArchiveError as exc:
                await self._reply(message, f"附件暂存失败：{exc}")
                return
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
                has_media=bool(item.get("images")),
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
            await self._reply(
                message,
                _group_summary_text(preview) + _media_preview_suffix(session.pending_media),
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
            await self._reply(
                message,
                _group_summary_text(preview) + _media_preview_suffix(session.pending_media),
            )
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
        raw_updates = preview.get("profile_updates")
        updates = list(raw_updates) if isinstance(raw_updates, list) else []
        conflicts = preview.get("conflicts")
        has_conflicts = isinstance(conflicts, list) and bool(conflicts)
        crm_ready = not isinstance(resolution, dict) or resolution.get("status") in {
            "unique",
            "disabled",
        }
        media_updates = _pending_media_updates(session)
        media_ready = bool(session.pending_media) and bool(merchant) and crm_ready and not has_conflicts
        profile_ready = (
            preview.get("save_readiness") == "ready" and bool(updates) and not has_conflicts
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
                elif not merchant:
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
        combined_updates = [*updates, *media_updates]
        first_result: dict[str, Any] | None = None
        first_store: dict[str, Any] = {}
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
                    await self._reply(message, "保存失败，草稿已保留，请稍后重试。")
                return
            first_store = (
                first_result.get("result")
                if isinstance(first_result.get("result"), dict)
                else {}
            )
            if not (first_result.get("write_performed") or first_store.get("duplicate")):
                await self._reply(message, "提交未完成，草稿已保留。")
                return
            documents = _document_links(first_store)
            session.media_archive_node_token = (
                _archive_wiki_token(first_store) or session.media_archive_node_token
            )

        archived_count = 0
        if session.pending_media:
            current = _current_profile_values(session)
            outstanding = [
                item
                for item in session.pending_media
                if not current.get(f"documents.{item.get('archive_id')}.url")
            ]
            archived_count = len(session.pending_media) - len(outstanding)
            if outstanding:
                if self.media_archiver is None or not session.media_archive_node_token:
                    session.current_profile_loaded = False
                    session.preview = None
                    await self._reply(
                        message,
                        "文字资料已写入，但附件尚未归档；草稿和原文件已保留，请稍后再次回复“保存吧”。",
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
                                wiki_token=session.media_archive_node_token,
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
                        "附件已上传，但资料归档链接写入失败；草稿已保留，可再次回复“保存吧”重试。",
                    )
                    return
                media_store = (
                    media_result.get("result")
                    if isinstance(media_result.get("result"), dict)
                    else {}
                )
                if not (media_result.get("write_performed") or media_store.get("duplicate")):
                    session.current_profile_loaded = False
                    session.preview = None
                    await self._reply(message, "附件链接提交未完成，草稿已保留。")
                    return
                archived_count += len(outstanding)
                media_documents = _document_links(media_store)
                if media_documents:
                    documents = media_documents

        if documents:
            session.last_documents = documents
        duplicate = (
            "（此前已保存，本次未重复写入）"
            if first_store.get("duplicate") and not archived_count
            else ""
        )
        if documents:
            reply_text = "已写入：\n" + "\n".join(
                f"- {item['label']}：{item['url']}" for item in documents
            )
        else:
            reply_text = f"已写入知识库。{duplicate}"
        if archived_count:
            reply_text += f"\n- 已归档附件：{archived_count} 个"
        self._discard_pending_media(session)
        await self._reply(message, reply_text)
        session.reset_draft(preserve_merchant=_is_group_message(message))
