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


@dataclass(frozen=True)
class Settings:
    api_key: str
    openai_api_key: str
    host: str = "127.0.0.1"
    port: int = 8090
    model: str = "gpt-6-luna"
    openai_timeout: int = 60
    max_body_bytes: int = 65_536

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.environ.get("MERCHANT_AGENT_API_KEY", "").strip()
        openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        model = os.environ.get("MERCHANT_AGENT_MODEL", "gpt-6-luna").strip()
        host = os.environ.get("MERCHANT_AGENT_HOST", "127.0.0.1").strip()
        if len(api_key) < 24:
            raise ConfigurationError("MERCHANT_AGENT_API_KEY must be at least 24 characters")
        if not openai_api_key:
            raise ConfigurationError("OPENAI_API_KEY is required")
        if not model:
            raise ConfigurationError("MERCHANT_AGENT_MODEL is required")
        if not host:
            raise ConfigurationError("MERCHANT_AGENT_HOST is required")
        return cls(
            api_key=api_key,
            openai_api_key=openai_api_key,
            host=host,
            port=_integer("MERCHANT_AGENT_PORT", 8090, 1, 65_535),
            model=model,
            openai_timeout=_integer("MERCHANT_AGENT_OPENAI_TIMEOUT", 60, 5, 300),
            max_body_bytes=_integer(
                "MERCHANT_AGENT_MAX_BODY_BYTES", 65_536, 1_024, 1_048_576
            ),
        )

