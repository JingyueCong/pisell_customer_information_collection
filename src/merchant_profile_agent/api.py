from __future__ import annotations

import hmac
import json
import logging
import re
import uuid
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import urlsplit

from . import __version__
from .agent import MerchantProfileAgent
from .crm import (
    CrmResolutionRequired,
    CrmResolver,
    CrmResolverError,
    CrmResolverUnavailable,
    DisabledCrmResolver,
)
from .knowledge_store import (
    DisabledKnowledgeStore,
    KnowledgeStore,
    KnowledgeStoreError,
    KnowledgeStoreUnavailable,
)
from .openai_client import OpenAIClientError
from .settings import Settings
from .validation import ProfileValidationError, validate_update_set


LOGGER = logging.getLogger("merchant_profile_agent")


def openapi_document() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "PiSell Merchant Profile Agent API", "version": __version__},
        "servers": [{"url": "/"}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"}
            }
        },
        "paths": {
            "/healthz": {"get": {"responses": {"200": {"description": "Healthy"}}}},
            "/v1/profile/analyze": {
                "post": {
                    "security": [{"bearerAuth": []}],
                    "responses": {
                        "200": {"description": "Structured merchant profile preview"},
                        "400": {"description": "Invalid request"},
                        "401": {"description": "Unauthorized"},
                        "502": {"description": "OpenAI upstream failure"},
                    },
                }
            },
            "/v1/profile/validate": {
                "post": {
                    "security": [{"bearerAuth": []}],
                    "responses": {
                        "200": {"description": "Validated update preview"},
                        "400": {"description": "Invalid request"},
                        "401": {"description": "Unauthorized"},
                    },
                }
            },
            "/v1/profile/read": {
                "post": {
                    "security": [{"bearerAuth": []}],
                    "responses": {
                        "200": {"description": "Current profile rebuilt from Feishu Events"},
                        "400": {"description": "Invalid request"},
                        "401": {"description": "Unauthorized"},
                        "503": {"description": "Knowledge store unavailable"},
                    },
                }
            },
            "/v1/profile/crm/resolve": {
                "post": {
                    "security": [{"bearerAuth": []}],
                    "responses": {
                        "200": {"description": "Read-only CRM customer resolution"},
                        "400": {"description": "Invalid request"},
                        "401": {"description": "Unauthorized"},
                        "503": {"description": "CRM lookup unavailable"},
                    },
                }
            },
            "/v1/profile/commit": {
                "post": {
                    "security": [{"bearerAuth": []}],
                    "responses": {
                        "200": {"description": "Confirmed Event and Wiki projection commit"},
                        "400": {"description": "Invalid or unconfirmed request"},
                        "401": {"description": "Unauthorized"},
                        "502": {"description": "Knowledge store operation failed"},
                        "503": {"description": "Knowledge store unavailable"},
                    },
                }
            },
        },
    }


def create_server(
    settings: Settings,
    agent: MerchantProfileAgent,
    knowledge_store: KnowledgeStore | None = None,
    crm_resolver: CrmResolver | None = None,
) -> ThreadingHTTPServer:
    store = knowledge_store or DisabledKnowledgeStore()
    crm = crm_resolver or DisabledCrmResolver()

    def resolve_crm(request: Mapping[str, Any]) -> dict[str, Any]:
        return crm.resolve(request)

    def canonical_crm_merchant(
        resolution: Mapping[str, Any], *, submitted_name: Any, submitted_id: Any
    ) -> dict[str, Any]:
        if resolution.get("status") != "unique":
            return {"name": submitted_name, "id": submitted_id, "aliases": []}
        candidates = resolution.get("candidates")
        candidate = (
            candidates[0]
            if isinstance(candidates, list) and len(candidates) == 1
            else None
        )
        if not isinstance(candidate, Mapping):
            raise CrmResolverError("CRM lookup returned an invalid unique match")
        candidate_id = candidate.get("work_item_id")
        candidate_name = candidate.get("name")
        if not isinstance(candidate_id, (str, int)) or not str(candidate_id).strip():
            raise CrmResolverError("CRM lookup returned an invalid unique match")
        if not isinstance(candidate_name, str) or not candidate_name.strip():
            raise CrmResolverError("CRM lookup returned an invalid unique match")
        canonical_name = candidate_name.strip()
        aliases: list[str] = []
        if isinstance(submitted_name, str):
            submitted_name = submitted_name.strip()
            if submitted_name and submitted_name.casefold() != canonical_name.casefold():
                aliases.append(submitted_name)
        return {
            "name": canonical_name,
            "id": str(candidate_id).strip(),
            "aliases": aliases,
        }

    def read_with_crm(request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            resolution = resolve_crm(request)
        except CrmResolverUnavailable:
            submitted_name = request.get("merchant_name")
            submitted_id = request.get("merchant_id")
            if (
                not isinstance(submitted_name, str)
                or not submitted_name.strip()
                or not isinstance(submitted_id, str)
                or not submitted_id.strip()
            ):
                raise
            # A previously bound CRM ID is sufficient for a fail-safe read-only
            # lookup when the CRM bridge is temporarily unavailable. Mutating
            # commits continue to require a fresh CRM resolution below.
            resolution = {
                "status": "bound_id_fallback",
                "query": {
                    "merchant_name": submitted_name.strip(),
                    "merchant_id": submitted_id.strip(),
                },
                "candidates": [],
                "read_only": True,
                "crm_available": False,
            }
        identity = canonical_crm_merchant(
            resolution,
            submitted_name=request.get("merchant_name"),
            submitted_id=request.get("merchant_id"),
        )
        effective_request = {
            "merchant_name": request.get("merchant_name"),
            "merchant_id": request.get("merchant_id"),
        }
        candidates = resolution.get("candidates")
        if resolution.get("status") == "unique" and isinstance(candidates, list):
            effective_request["merchant_name"] = identity["name"]
            effective_request["merchant_id"] = identity["id"]
        result = deepcopy(store.read_profile(effective_request))
        profile = result.get("profile")
        if resolution.get("status") == "unique" and isinstance(profile, Mapping):
            profile = dict(profile)
            existing_merchant = profile.get("merchant")
            existing_aliases = (
                existing_merchant.get("aliases")
                if isinstance(existing_merchant, Mapping)
                else None
            )
            aliases = list(identity["aliases"])
            if isinstance(existing_aliases, list):
                for alias in existing_aliases:
                    if (
                        isinstance(alias, str)
                        and alias.strip()
                        and alias.casefold()
                        not in {existing.casefold() for existing in aliases}
                    ):
                        aliases.append(alias.strip())
            profile["merchant"] = {**identity, "aliases": aliases[:10]}
            result["profile"] = profile
        return {
            **result,
            "crm": resolution,
            "merchant_identity": identity,
        }

    def commit_with_crm(request: Mapping[str, Any]) -> dict[str, Any]:
        merchant = request.get("merchant")
        if not isinstance(merchant, Mapping):
            raise ProfileValidationError("merchant is required")
        name = merchant.get("name")
        identifier = merchant.get("id")
        resolution = resolve_crm(
            {"merchant_name": name, "merchant_id": identifier}
        )
        if resolution.get("status") not in {"unique", "disabled"}:
            raise CrmResolutionRequired(resolution)
        effective_request = deepcopy(dict(request))
        if resolution.get("status") == "unique":
            identity = canonical_crm_merchant(
                resolution, submitted_name=name, submitted_id=identifier
            )
            effective_request["merchant"] = identity
        result = store.commit_profile(effective_request)
        return {
            **result,
            "crm": resolution,
            "merchant_identity": canonical_crm_merchant(
                resolution, submitted_name=name, submitted_id=identifier
            ),
        }

    class Handler(BaseHTTPRequestHandler):
        server_version = "PiSellMerchantProfileAgent/" + __version__

        def log_message(self, format_string: str, *args: object) -> None:
            LOGGER.info("%s %s", self.client_address[0], format_string % args)

        def _send_json(
            self,
            status: int,
            payload: Mapping[str, Any],
            *,
            request_id: str | None = None,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if request_id:
                self.send_header("X-Request-ID", request_id)
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            authorization = self.headers.get("Authorization", "")
            if not authorization.startswith("Bearer "):
                return False
            supplied = authorization[7:]
            return hmac.compare_digest(supplied, settings.api_key)

        def _read_json(self) -> dict[str, Any]:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
            if content_type != "application/json":
                raise ProfileValidationError("Content-Type must be application/json")
            raw_length = self.headers.get("Content-Length")
            if raw_length is None or not re.fullmatch(r"\d+", raw_length):
                raise ProfileValidationError("Content-Length is required")
            length = int(raw_length)
            if length <= 0 or length > settings.max_body_bytes:
                raise ProfileValidationError("request body size is invalid")
            try:
                value = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProfileValidationError("request body must be valid JSON") from exc
            if not isinstance(value, dict):
                raise ProfileValidationError("request body must be a JSON object")
            return value

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path == "/healthz":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "service": "pisell-merchant-profile-agent",
                        "version": __version__,
                        "model": settings.model,
                        "classifier_model": settings.classifier_model,
                        "knowledge_store": store.name,
                        "crm_resolver": crm.name,
                    },
                )
                return
            if path == "/openapi.json":
                self._send_json(HTTPStatus.OK, openapi_document())
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            request_id = str(uuid.uuid4())
            if not self._authorized():
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "unauthorized", "request_id": request_id},
                    request_id=request_id,
                )
                return
            path = urlsplit(self.path).path
            try:
                request = self._read_json()
                if path == "/v1/profile/analyze":
                    result = agent.analyze(request)
                elif path == "/v1/profile/validate":
                    extra = set(request) - {"merchant_name", "updates", "current_profile"}
                    if extra:
                        raise ProfileValidationError(
                            "request has undeclared fields: " + ", ".join(sorted(extra))
                        )
                    raw_updates = request.get("updates")
                    if not isinstance(raw_updates, list):
                        raise ProfileValidationError("updates must be an array")
                    if not all(isinstance(item, Mapping) for item in raw_updates):
                        raise ProfileValidationError("every update must be an object")
                    result = validate_update_set(
                        request.get("merchant_name"),
                        raw_updates,
                        request.get("current_profile", {}),
                    )
                elif path == "/v1/profile/read":
                    result = read_with_crm(request)
                elif path == "/v1/profile/crm/resolve":
                    result = {"crm": resolve_crm(request), "write_performed": False}
                elif path == "/v1/profile/commit":
                    result = commit_with_crm(request)
                else:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "not_found", "request_id": request_id},
                        request_id=request_id,
                    )
                    return
            except ProfileValidationError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_request", "message": str(exc), "request_id": request_id},
                    request_id=request_id,
                )
                return
            except KnowledgeStoreUnavailable as exc:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "error": "knowledge_store_unavailable",
                        "message": str(exc),
                        "request_id": request_id,
                    },
                    request_id=request_id,
                )
                return
            except CrmResolutionRequired as exc:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "error": "crm_resolution_required",
                        "message": str(exc),
                        "crm": exc.resolution,
                        "request_id": request_id,
                    },
                    request_id=request_id,
                )
                return
            except CrmResolverUnavailable as exc:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "error": "crm_unavailable",
                        "message": str(exc),
                        "request_id": request_id,
                    },
                    request_id=request_id,
                )
                return
            except CrmResolverError:
                LOGGER.exception("CRM lookup failed request_id=%s", request_id)
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "crm_failure", "request_id": request_id},
                    request_id=request_id,
                )
                return
            except KnowledgeStoreError:
                LOGGER.exception("Knowledge-store request failed request_id=%s", request_id)
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "knowledge_store_failure", "request_id": request_id},
                    request_id=request_id,
                )
                return
            except OpenAIClientError:
                LOGGER.exception("OpenAI request failed request_id=%s", request_id)
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "upstream_failure", "request_id": request_id},
                    request_id=request_id,
                )
                return
            except Exception:
                LOGGER.exception("Unhandled request failure request_id=%s", request_id)
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "internal_error", "request_id": request_id},
                    request_id=request_id,
                )
                return
            result = {**result, "request_id": request_id}
            self._send_json(HTTPStatus.OK, result, request_id=request_id)

    return ThreadingHTTPServer((settings.host, settings.port), Handler)
