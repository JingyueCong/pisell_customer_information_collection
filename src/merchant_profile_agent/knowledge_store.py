from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Mapping, Protocol

from .agent import validate_messages
from .settings import Settings
from .validation import ProfileValidationError, validate_update_set


SAVE_CONFIRMATIONS = {"结束并保存", "/save"}


class KnowledgeStoreError(RuntimeError):
    """A knowledge-store operation failed without exposing connector secrets."""


class KnowledgeStoreUnavailable(KnowledgeStoreError):
    pass


class KnowledgeStore(Protocol):
    name: str

    def read_profile(self, request: Mapping[str, Any]) -> dict[str, Any]: ...

    def commit_profile(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


class DisabledKnowledgeStore:
    name = "disabled"

    def read_profile(self, request: Mapping[str, Any]) -> dict[str, Any]:
        raise KnowledgeStoreUnavailable("knowledge store is not configured")

    def commit_profile(self, request: Mapping[str, Any]) -> dict[str, Any]:
        raise KnowledgeStoreUnavailable("knowledge store is not configured")


class P1KnowledgeStore:
    """Adapter for the established P1 Event/Artifact/Wiki projection runtime.

    Imports are delayed until startup so the standalone API remains independently
    testable and can still run in preview-only mode without the P1 checkout.
    """

    name = "p1"

    def __init__(self, *, root: Path, config_path: Path, lark_cli: str) -> None:
        root = root.expanduser().resolve()
        config_path = config_path.expanduser().resolve()
        if not (root / "src" / "pisell_agents").is_dir():
            raise KnowledgeStoreUnavailable("configured P1 runtime is unavailable")
        if not config_path.is_file():
            raise KnowledgeStoreUnavailable("configured Feishu data-layer file is unavailable")
        source = str(root / "src")
        if source not in sys.path:
            sys.path.insert(0, source)
        try:
            registry = importlib.import_module("pisell_agents.registry")
            local_commit = importlib.import_module("pisell_agents.local_commit")
            data_layer = importlib.import_module("pisell_agents.feishu_data_layer")
            lark_module = importlib.import_module("pisell_agents.lark_cli")
        except ImportError as exc:
            raise KnowledgeStoreUnavailable("P1 runtime could not be imported") from exc

        try:
            self.agent = registry.load_agent("operation-agent", root)
            config = data_layer.FeishuDataLayerConfig.load(config_path)
            client = lark_module.LarkCliClient(executable=lark_cli)
            self.committer = local_commit.LocalConversationCommitter(
                client, config, project_root=root
            )
        except Exception as exc:
            raise KnowledgeStoreUnavailable("P1 knowledge connector could not start") from exc

    @staticmethod
    def _merchant(request: Mapping[str, Any]) -> tuple[str, str | None]:
        extra = set(request) - {"merchant_name", "merchant_id"}
        if extra:
            raise ProfileValidationError(
                "request has undeclared fields: " + ", ".join(sorted(extra))
            )
        name = request.get("merchant_name")
        merchant_id = request.get("merchant_id")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise ProfileValidationError("merchant_name is required")
        if merchant_id is not None and (
            not isinstance(merchant_id, str)
            or not merchant_id.strip()
            or len(merchant_id.strip()) > 200
        ):
            raise ProfileValidationError("merchant_id is invalid")
        return name.strip(), merchant_id.strip() if isinstance(merchant_id, str) else None

    def read_profile(self, request: Mapping[str, Any]) -> dict[str, Any]:
        merchant_name, merchant_id = self._merchant(request)
        try:
            profile = self.committer.read_merchant_profile(
                agent=self.agent,
                merchant_name=merchant_name,
                merchant_id=merchant_id,
            )
        except Exception as exc:
            raise KnowledgeStoreError("could not read the merchant profile") from exc
        return {"profile": profile, "write_performed": False, "knowledge_store": self.name}

    def commit_profile(self, request: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"confirmation", "conversation_id", "merchant", "messages", "updates"}
        extra = set(request) - allowed
        if extra:
            raise ProfileValidationError(
                "request has undeclared fields: " + ", ".join(sorted(extra))
            )
        confirmation = request.get("confirmation")
        if confirmation not in SAVE_CONFIRMATIONS:
            raise ProfileValidationError("confirmation must be 结束并保存 or /save")
        conversation_id = request.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ProfileValidationError("conversation_id is required")
        if len(conversation_id) > 300:
            raise ProfileValidationError("conversation_id is too long")
        merchant = request.get("merchant")
        if not isinstance(merchant, Mapping):
            raise ProfileValidationError("merchant is required")
        merchant_name, merchant_id = self._merchant(
            {"merchant_name": merchant.get("name"), "merchant_id": merchant.get("id")}
        )
        # Validate the submitted conversation, but do not persist it verbatim.
        # Only the accepted facts below belong in the durable P1 event; the
        # source_ref on each fact preserves traceability without storing chat
        # filler, repetition, or unrelated background.
        validate_messages(request.get("messages"))
        raw_updates = request.get("updates")
        if not isinstance(raw_updates, list) or not raw_updates:
            raise ProfileValidationError("updates must be a non-empty array")
        if not all(isinstance(item, Mapping) for item in raw_updates):
            raise ProfileValidationError("every update must be an object")
        cleaned_updates: list[dict[str, Any]] = []
        for item in raw_updates:
            extra = set(item) - {
                "field_path",
                "value",
                "source_ref",
                "replace_confirmed",
                "state",
                "document_type",
            }
            if extra:
                raise ProfileValidationError(
                    "profile update has undeclared fields: " + ", ".join(sorted(extra))
                )
            if item.get("state", "explicit") != "explicit":
                raise ProfileValidationError("only explicit profile updates can be saved")
            cleaned_updates.append(
                {
                    key: item.get(key)
                    for key in (
                        "field_path",
                        "value",
                        "source_ref",
                        "replace_confirmed",
                    )
                }
            )

        try:
            current_profile = self.committer.read_merchant_profile(
                agent=self.agent,
                merchant_name=merchant_name,
                merchant_id=merchant_id,
            )
        except Exception as exc:
            raise KnowledgeStoreError("could not read the merchant profile before save") from exc
        validated = validate_update_set(merchant_name, cleaned_updates, current_profile)
        if validated["save_readiness"] != "ready" or not validated["profile_updates"]:
            raise ProfileValidationError(
                "updates are not ready to save; resolve missing merchant data or conflicts first"
            )

        updates = [
            {
                "field_path": item["field_path"],
                "value": item["value"],
                "state": "explicit",
                "source_ref": item["source_ref"],
                "replace_confirmed": item["replace_confirmed"],
            }
            for item in validated["profile_updates"]
        ]
        event_count = int(current_profile.get("event_count") or 0)
        close_result = {
            "output": {
                "summary": f"更新商户 {merchant_name} 的 {len(updates)} 项资料",
                "decision": "ready_for_review",
                "facts": [f"{item['field_path']} 已由用户明确提供" for item in updates],
                "assumptions": [],
                "evidence": [item["source_ref"] for item in updates],
                "risks": [],
                "next_actions": ["由经手人员按需复核知识库投影"],
            },
            "business_objects": {
                "merchant": {"name": merchant_name, "id": merchant_id}
            },
            "documents": [
                {
                    "document_type": "merchant-overview",
                    "markdown": "Managed merchant profile projection generated from committed Events.",
                }
            ],
            "specific": {
                "operation_status": "ready_for_review",
                "operation_type": "update" if event_count else "create",
                "profile_updates": updates,
                "affected_modules": validated["affected_modules"],
                "current_profile_summary": {
                    "event_count": event_count,
                    "digest": current_profile.get("digest", ""),
                },
                "information_gaps": [],
                "conflicts": [],
                "save_readiness": "ready",
            },
            "source_correlation_id": None,
            "source_message_id": None,
        }
        try:
            result = self.committer.commit(
                agent=self.agent,
                conversation_id=conversation_id.strip(),
                message_history=[
                    {
                        "role": "user",
                        "content": f"{item['field_path']}: {item['value']}",
                    }
                    for item in updates
                ],
                close_result=close_result,
            )
        except Exception as exc:
            raise KnowledgeStoreError("could not commit the merchant profile") from exc
        return {
            "knowledge_store": self.name,
            "write_performed": result.get("store_status") == "stored"
            and int(result.get("records_written") or 0) > 0,
            "result": result,
        }


def create_knowledge_store(settings: Settings) -> KnowledgeStore:
    if settings.knowledge_store == "disabled":
        return DisabledKnowledgeStore()
    return P1KnowledgeStore(
        root=Path(settings.p1_root),
        config_path=Path(settings.feishu_data_layer_config),
        lark_cli=settings.lark_cli,
    )
