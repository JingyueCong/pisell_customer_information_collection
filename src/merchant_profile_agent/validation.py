from __future__ import annotations

import re
from collections import Counter, OrderedDict
from typing import Any, Mapping, Sequence


MODULES: "OrderedDict[str, tuple[str, ...]]" = OrderedDict(
    [
        ("business-contacts", ("business.", "contacts.")),
        ("menu-content", ("menu.", "content.")),
        ("system-configuration", ("system.", "configuration.", "workarounds.")),
        ("hardware-installation", ("hardware.", "installation.")),
        ("risk-compliance", ("risk.", "compliance.")),
        ("commercial-information", ("commercial.",)),
        ("store-profile", ("stores.",)),
    ]
)

FIELD_PATH = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_-]+)+$")
SENSITIVE_FIELD_PATH = re.compile(
    r"(?:^|[._-])(?:password|passcode|verification[_-]?code|secret|api[_-]?key|"
    r"access[_-]?token|private[_-]?key|pin|cvv|full[_-]?card(?:[_-]?number)?)(?:$|[._-])"
)
SENSITIVE_VALUE_LABEL = re.compile(
    r"(?:password|passcode|verification\s*code|api\s*key|access\s*token|private\s*key|"
    r"secret|bearer\s+[a-z0-9._-]+|密码|口令|验证码|密钥|访问令牌)",
    re.IGNORECASE,
)
PRIVATE_KEY_MARKER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
LONG_DIGIT_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
UPDATE_KEYS = {"field_path", "value", "source_ref", "replace_confirmed"}


class ProfileValidationError(ValueError):
    pass


def document_for_field(field_path: str) -> str:
    for document_type, prefixes in MODULES.items():
        if any(field_path.startswith(prefix) for prefix in prefixes):
            return document_type
    raise ProfileValidationError(f"unsupported merchant profile field path: {field_path!r}")


def _luhn_valid(candidate: str) -> bool:
    digits = [int(character) for character in candidate if character.isdigit()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def contains_sensitive_value(value: str) -> bool:
    if SENSITIVE_VALUE_LABEL.search(value) or PRIVATE_KEY_MARKER.search(value):
        return True
    return any(_luhn_valid(match.group(0)) for match in LONG_DIGIT_CANDIDATE.finditer(value))


def validate_update(raw: Mapping[str, Any]) -> dict[str, Any]:
    extra = set(raw) - UPDATE_KEYS
    missing = UPDATE_KEYS - set(raw)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {sorted(missing)}")
        if extra:
            details.append(f"undeclared {sorted(extra)}")
        raise ProfileValidationError("invalid profile update: " + "; ".join(details))

    field_path = raw.get("field_path")
    if not isinstance(field_path, str) or not FIELD_PATH.fullmatch(field_path):
        raise ProfileValidationError("profile update field_path is invalid")
    if SENSITIVE_FIELD_PATH.search(field_path):
        raise ProfileValidationError(f"profile update {field_path!r} targets sensitive data")
    document_type = document_for_field(field_path)

    value = raw.get("value")
    if not isinstance(value, str) or not value.strip():
        raise ProfileValidationError(f"profile update {field_path!r} has no text value")
    if len(value) > 4_000:
        raise ProfileValidationError(f"profile update {field_path!r} value is too long")
    if contains_sensitive_value(value):
        raise ProfileValidationError(f"profile update {field_path!r} contains sensitive data")

    source_ref = raw.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref.strip():
        raise ProfileValidationError(f"profile update {field_path!r} requires a source")
    if len(source_ref) > 500:
        raise ProfileValidationError(f"profile update {field_path!r} source_ref is too long")

    replace_confirmed = raw.get("replace_confirmed")
    if not isinstance(replace_confirmed, bool):
        raise ProfileValidationError(
            f"profile update {field_path!r} replace_confirmed must be boolean"
        )
    return {
        "field_path": field_path,
        "value": value.strip(),
        "state": "explicit",
        "source_ref": source_ref.strip(),
        "replace_confirmed": replace_confirmed,
        "document_type": document_type,
    }


def _current_fields(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(profile, Mapping):
        return {}
    raw_fields = profile.get("fields", profile)
    if not isinstance(raw_fields, Mapping):
        raise ProfileValidationError("current_profile fields must be an object")
    fields: dict[str, Any] = {}
    for field_path, raw_value in raw_fields.items():
        if not isinstance(field_path, str):
            continue
        if isinstance(raw_value, Mapping) and "value" in raw_value:
            fields[field_path] = raw_value["value"]
        else:
            fields[field_path] = raw_value
    return fields


def validate_update_set(
    merchant_name: str | None,
    raw_updates: Sequence[Mapping[str, Any]],
    current_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_name = merchant_name.strip() if isinstance(merchant_name, str) else ""
    updates = [validate_update(item) for item in raw_updates]
    counts = Counter(item["field_path"] for item in updates)
    duplicates = sorted(path for path, count in counts.items() if count > 1)
    if duplicates:
        raise ProfileValidationError(
            "profile updates contain duplicate field paths: " + ", ".join(duplicates)
        )

    current_fields = _current_fields(current_profile)
    accepted: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for update in updates:
        current_value = current_fields.get(update["field_path"])
        if update["field_path"] not in current_fields:
            accepted.append(update)
        elif str(current_value) == update["value"]:
            unchanged.append(
                {"field_path": update["field_path"], "value": update["value"]}
            )
        elif update["replace_confirmed"]:
            accepted.append(update)
        else:
            conflicts.append(
                {
                    "field_path": update["field_path"],
                    "current_value": current_value,
                    "proposed_value": update["value"],
                }
            )

    if not normalized_name:
        save_readiness = "needs_input"
    elif conflicts:
        save_readiness = "needs_confirmation"
    elif accepted:
        save_readiness = "ready"
    else:
        save_readiness = "needs_input"
    affected_modules = [
        module
        for module in MODULES
        if any(item["document_type"] == module for item in accepted)
    ]
    return {
        "merchant_name": normalized_name or None,
        "profile_updates": accepted,
        "unchanged": unchanged,
        "conflicts": conflicts,
        "affected_modules": affected_modules,
        "save_readiness": save_readiness,
        "write_performed": False,
    }
