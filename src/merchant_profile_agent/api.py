from __future__ import annotations

import hmac
import json
import logging
import re
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import urlsplit

from . import __version__
from .agent import MerchantProfileAgent
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
        },
    }


def create_server(settings: Settings, agent: MerchantProfileAgent) -> ThreadingHTTPServer:
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

