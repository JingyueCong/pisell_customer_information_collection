from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    pass


def load_env_file(path: Path) -> None:
    if not path.is_file():
        raise ConfigurationError(f"environment file does not exist: {path}")
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigurationError(f"invalid environment line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum() or not key[0].isalpha():
            raise ConfigurationError(f"invalid environment key on line {line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


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


@dataclass(frozen=True)
class Settings:
    api_key: str
    openai_api_key: str
    host: str = "127.0.0.1"
    port: int = 8090
    model: str = "gpt-6-luna"
    classifier_model: str = "gpt-6-sol"
    openai_timeout: int = 60
    max_body_bytes: int = 10_485_760
    knowledge_store: str = "disabled"
    p1_root: str = ""
    feishu_data_layer_config: str = ""
    lark_cli: str = "lark-cli"
    crm_resolver: str = "disabled"
    crm_timeout: int = 20
    ticket_bridge_url: str = "http://127.0.0.1:8787"
    ticket_bridge_token: str = ""
    meegle_bin: str = "meegle"
    meegle_profile: str = ""
    meegle_project_key: str = "v2qint"
    meegle_crm_work_item_type: str = "67df84a32e42950cb0195cba"
    meegle_crm_url_template: str = ""
    url_reading_enabled: bool = True
    url_read_timeout: int = 10
    url_read_max_urls: int = 3
    url_read_max_bytes: int = 1_048_576
    url_read_max_chars: int = 8_000

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.environ.get("MERCHANT_AGENT_API_KEY", "").strip()
        openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        model = os.environ.get("MERCHANT_AGENT_MODEL", "gpt-6-luna").strip()
        classifier_model = os.environ.get(
            "MERCHANT_AGENT_CLASSIFIER_MODEL", "gpt-6-sol"
        ).strip()
        host = os.environ.get("MERCHANT_AGENT_HOST", "127.0.0.1").strip()
        if len(api_key) < 24:
            raise ConfigurationError("MERCHANT_AGENT_API_KEY must be at least 24 characters")
        if not openai_api_key:
            raise ConfigurationError("OPENAI_API_KEY is required")
        if not model:
            raise ConfigurationError("MERCHANT_AGENT_MODEL is required")
        if not classifier_model:
            raise ConfigurationError("MERCHANT_AGENT_CLASSIFIER_MODEL is required")
        if not host:
            raise ConfigurationError("MERCHANT_AGENT_HOST is required")
        knowledge_store = os.environ.get(
            "MERCHANT_AGENT_KNOWLEDGE_STORE", "disabled"
        ).strip().lower()
        if knowledge_store not in {"disabled", "p1"}:
            raise ConfigurationError(
                "MERCHANT_AGENT_KNOWLEDGE_STORE must be disabled or p1"
            )
        p1_root = os.environ.get("PISELL_P1_ROOT", "").strip()
        feishu_data_layer_config = os.environ.get(
            "PISELL_FEISHU_DATA_LAYER_CONFIG", ""
        ).strip()
        if knowledge_store == "p1" and not p1_root:
            raise ConfigurationError("PISELL_P1_ROOT is required for the p1 knowledge store")
        if knowledge_store == "p1" and not feishu_data_layer_config:
            raise ConfigurationError(
                "PISELL_FEISHU_DATA_LAYER_CONFIG is required for the p1 knowledge store"
            )
        crm_resolver = os.environ.get("MERCHANT_AGENT_CRM_RESOLVER", "disabled").strip().lower()
        if crm_resolver not in {"disabled", "meegle", "ticket_bridge"}:
            raise ConfigurationError(
                "MERCHANT_AGENT_CRM_RESOLVER must be disabled, meegle, or ticket_bridge"
            )
        meegle_bin = os.environ.get("MEEGLE_BIN", "meegle").strip()
        meegle_project_key = os.environ.get("MEEGLE_PROJECT_KEY", "v2qint").strip()
        meegle_crm_work_item_type = os.environ.get(
            "MEEGLE_CRM_WORK_ITEM_TYPE", "67df84a32e42950cb0195cba"
        ).strip()
        if crm_resolver == "meegle" and not all(
            (meegle_bin, meegle_project_key, meegle_crm_work_item_type)
        ):
            raise ConfigurationError("Meegle CRM resolver settings cannot be empty")
        ticket_bridge_url = os.environ.get(
            "TICKET_BRIDGE_URL", "http://127.0.0.1:8787"
        ).strip().rstrip("/")
        ticket_bridge_token = os.environ.get("TICKET_BRIDGE_TOKEN", "").strip()
        if crm_resolver == "ticket_bridge":
            if ticket_bridge_url not in {
                "http://127.0.0.1:8787",
                "http://localhost:8787",
                "http://[::1]:8787",
            }:
                raise ConfigurationError("TICKET_BRIDGE_URL must use the local ticket bridge")
            if len(ticket_bridge_token) < 24:
                raise ConfigurationError(
                    "TICKET_BRIDGE_TOKEN must contain at least 24 characters"
                )
        return cls(
            api_key=api_key,
            openai_api_key=openai_api_key,
            host=host,
            port=_integer("MERCHANT_AGENT_PORT", 8090, 1, 65_535),
            model=model,
            classifier_model=classifier_model,
            openai_timeout=_integer("MERCHANT_AGENT_OPENAI_TIMEOUT", 60, 5, 300),
            max_body_bytes=_integer(
                "MERCHANT_AGENT_MAX_BODY_BYTES", 10_485_760, 1_024, 16_777_216
            ),
            knowledge_store=knowledge_store,
            p1_root=p1_root,
            feishu_data_layer_config=feishu_data_layer_config,
            lark_cli=os.environ.get("LARK_CLI", "lark-cli").strip() or "lark-cli",
            crm_resolver=crm_resolver,
            crm_timeout=_integer("MERCHANT_AGENT_CRM_TIMEOUT", 20, 3, 120),
            ticket_bridge_url=ticket_bridge_url,
            ticket_bridge_token=ticket_bridge_token,
            meegle_bin=meegle_bin,
            meegle_profile=os.environ.get("MEEGLE_PROFILE", "").strip(),
            meegle_project_key=meegle_project_key,
            meegle_crm_work_item_type=meegle_crm_work_item_type,
            meegle_crm_url_template=os.environ.get(
                "MEEGLE_CRM_URL_TEMPLATE", ""
            ).strip(),
            url_reading_enabled=_boolean("MERCHANT_AGENT_URL_READING_ENABLED", True),
            url_read_timeout=_integer("MERCHANT_AGENT_URL_READ_TIMEOUT", 10, 3, 30),
            url_read_max_urls=_integer("MERCHANT_AGENT_URL_READ_MAX_URLS", 3, 1, 5),
            url_read_max_bytes=_integer(
                "MERCHANT_AGENT_URL_READ_MAX_BYTES", 1_048_576, 65_536, 2_097_152
            ),
            url_read_max_chars=_integer(
                "MERCHANT_AGENT_URL_READ_MAX_CHARS", 8_000, 1_000, 16_000
            ),
        )
