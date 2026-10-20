from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from .api_client import MerchantApiClient
from .codex_dialogue import CodexDialogueError, CodexDialogueRouter
from .feishu_bot import FeishuBotController, SessionStore
from .media_archive import FeishuDriveArchiver, MediaStagingStore
from .settings import ConfigurationError, load_env_file
from .video import OpenAIAudioTranscriber, OpenAIVideoProcessor


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _feishu_origin() -> str:
    configured = os.environ.get("FEISHU_DRIVE_ORIGIN", "").strip()
    if configured:
        parsed = urlsplit(configured)
        if parsed.scheme == "https" and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        raise ConfigurationError("FEISHU_DRIVE_ORIGIN must be an https origin")
    config_path = os.environ.get("PISELL_FEISHU_DATA_LAYER_CONFIG", "").strip()
    if config_path:
        try:
            payload = json.loads(Path(config_path).expanduser().read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("data-layer config must be an object")
            projection = payload.get("knowledge_projection", {})
            profiles = (
                projection.get("routing_profiles", {})
                if isinstance(projection, dict)
                else {}
            )
            for profile in profiles.values():
                root_url = profile.get("root_url") if isinstance(profile, dict) else None
                if isinstance(root_url, str):
                    parsed = urlsplit(root_url)
                    if parsed.scheme == "https" and parsed.netloc:
                        return f"{parsed.scheme}://{parsed.netloc}"
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logging.getLogger(__name__).warning(
                "could not derive Feishu origin from data-layer config"
            )
    return "https://www.feishu.cn"


def build_channel():
    try:
        from lark_channel import FeishuChannel, LogLevel, PolicyConfig, SecurityConfig
    except ImportError as exc:
        raise ConfigurationError(
            "install the Feishu bot extra: pip install -e '.[feishu]'"
        ) from exc

    app_id = _required("FEISHU_BOT_APP_ID")
    app_secret = _required("FEISHU_BOT_APP_SECRET")
    api_key = _required("MERCHANT_AGENT_API_KEY")
    group_allowlist = [
        item.strip()
        for item in os.environ.get("FEISHU_BOT_GROUP_ALLOWLIST", "").split(",")
        if item.strip()
    ]
    sender_allowlist = [
        item.strip()
        for item in os.environ.get("FEISHU_BOT_SENDER_ALLOWLIST", "").split(",")
        if item.strip()
    ]
    # Passive group collection is gated again inside the controller by an
    # explicit @bot enable command. Save/control commands still require @bot.
    policy_options: dict[str, object] = {"require_mention": False}
    if group_allowlist:
        policy_options.update(
            {"group_policy": "allowlist", "group_allowlist": group_allowlist}
        )
    if sender_allowlist:
        policy_options.update({"dm_policy": "allowlist", "allow_from": sender_allowlist})
    channel = FeishuChannel(
        app_id=app_id,
        app_secret=app_secret,
        # INFO includes the SDK's full WebSocket URL. Keep temporary connection
        # parameters out of persistent launchd logs.
        log_level=LogLevel.WARNING,
        policy=PolicyConfig(**policy_options),
        security=SecurityConfig(
            mode="strict",
            strict_content_text=True,
            max_ws_fragment_parts=128,
            max_ws_fragment_bytes=8 * 1024 * 1024,
            max_concurrent_ws_handlers=32,
        ),
    )
    api = MerchantApiClient(
        base_url=os.environ.get("MERCHANT_AGENT_API_URL", "http://127.0.0.1:8090"),
        api_key=api_key,
    )
    default_session_store = (
        Path.home()
        / "Library"
        / "Application Support"
        / "PiSell"
        / "merchant-profile-feishu-sessions.json"
    )
    configured_session_store = os.environ.get("FEISHU_BOT_SESSION_STORE", "").strip()
    media_staging_path = os.environ.get("FEISHU_MEDIA_STAGING_DIR", "").strip()
    media_staging = MediaStagingStore(
        Path(media_staging_path)
        if media_staging_path
        else Path.home() / "files" / "PiSell" / "merchant-profile-pending",
        ttl_seconds=int(os.environ.get("FEISHU_BOT_SESSION_TTL", "604800")),
    )
    media_archiver = FeishuDriveArchiver(
        executable=os.environ.get("LARK_CLI", "lark-cli").strip() or "lark-cli",
        origin=_feishu_origin(),
    )
    video_max_bytes = _integer(
        "FEISHU_VIDEO_MAX_BYTES", 25 * 1024 * 1024, 1024, 25 * 1024 * 1024
    )
    video_max_seconds = _integer("FEISHU_VIDEO_MAX_DURATION_SECONDS", 300, 1, 300)
    video_processor = OpenAIVideoProcessor(
        OpenAIAudioTranscriber(
            _required("OPENAI_API_KEY"),
            model=os.environ.get(
                "MERCHANT_AGENT_TRANSCRIPTION_MODEL", "gpt-transcribe"
            ).strip()
            or "gpt-transcribe",
            timeout=_integer("FEISHU_VIDEO_TRANSCRIPTION_TIMEOUT", 180, 10, 600),
        ),
        maximum_bytes=video_max_bytes,
        maximum_duration_seconds=video_max_seconds,
    )
    dialogue_router = None
    if _boolean("FEISHU_CODEX_DIALOGUE_ENABLED", True):
        try:
            dialogue_router = CodexDialogueRouter(
                api_key=_required("OPENAI_API_KEY"),
                model=os.environ.get(
                    "MERCHANT_AGENT_CODEX_MODEL", "gpt-6-astra"
                ).strip()
                or "gpt-6-astra",
                timeout=float(os.environ.get("MERCHANT_AGENT_CODEX_TIMEOUT", "45")),
            )
        except (ValueError, CodexDialogueError) as exc:
            raise ConfigurationError(str(exc)) from exc
    controller = FeishuBotController(
        api=api,
        channel=channel,
        sessions=SessionStore(
            ttl_seconds=int(os.environ.get("FEISHU_BOT_SESSION_TTL", "604800")),
            state_path=Path(configured_session_store or default_session_store),
        ),
        group_message_threshold=_integer(
            "FEISHU_GROUP_SUMMARY_MESSAGE_THRESHOLD", 20, 5, 100
        ),
        group_idle_seconds=_integer(
            "FEISHU_GROUP_SUMMARY_IDLE_SECONDS", 600, 60, 3_600
        ),
        group_idle_min_messages=_integer(
            "FEISHU_GROUP_SUMMARY_IDLE_MIN_MESSAGES", 1, 1, 50
        ),
        video_processor=video_processor,
        maximum_video_bytes=video_max_bytes,
        maximum_video_duration_seconds=video_max_seconds,
        media_staging=media_staging,
        media_archiver=media_archiver,
        dialogue_router=dialogue_router,
    )
    channel.on("message", controller.on_message)
    channel.on("error", lambda error: logging.getLogger(__name__).error("channel error: %s", error))
    channel.on(
        "reconnecting",
        lambda event=None: logging.getLogger(__name__).warning("Feishu channel reconnecting"),
    )
    channel.on(
        "reconnected",
        lambda event=None: logging.getLogger(__name__).info("Feishu channel reconnected"),
    )
    channel.on(
        "reject",
        lambda event: logging.getLogger(__name__).warning(
            "Feishu message rejected message=%s chat=%s reason=%s",
            getattr(event, "message_id", "unknown"),
            getattr(event, "chat_id", "unknown"),
            getattr(event, "reason", "unknown"),
        ),
    )
    return channel


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the PiSell Feishu merchant profile bot")
    parser.add_argument("--env-file", type=Path, help="Optional KEY=VALUE environment file")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        if args.env_file:
            load_env_file(args.env_file)
        # Construct the SDK before the event loop starts. The WebSocket client
        # captures its loop during initialization and follows the official
        # ``asyncio.run(channel.connect())`` startup pattern.
        channel = build_channel()
        asyncio.run(channel.connect())
    except ConfigurationError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
