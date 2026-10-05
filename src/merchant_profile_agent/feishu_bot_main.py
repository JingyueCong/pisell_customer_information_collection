from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from .api_client import MerchantApiClient
from .feishu_bot import FeishuBotController, SessionStore
from .settings import ConfigurationError, load_env_file


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


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
    policy_options: dict[str, object] = {"require_mention": True}
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
    controller = FeishuBotController(
        api=api,
        channel=channel,
        sessions=SessionStore(
            ttl_seconds=int(os.environ.get("FEISHU_BOT_SESSION_TTL", "14400"))
        ),
    )
    channel.on("message", controller.on_message)
    channel.on("error", lambda error: logging.getLogger(__name__).error("channel error: %s", error))
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
