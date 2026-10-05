from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping


class MerchantApiError(RuntimeError):
    pass


class MerchantApiClient:
    def __init__(self, *, base_url: str, api_key: str, timeout: int = 90) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read())
                code = error.get("error", "request_failed")
                message = error.get("message")
            except (json.JSONDecodeError, UnicodeDecodeError):
                code, message = "request_failed", None
            detail = f"{code}: {message}" if message else str(code)
            raise MerchantApiError(detail) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise MerchantApiError("merchant profile API is unavailable") from exc
        if not isinstance(result, dict):
            raise MerchantApiError("merchant profile API returned an invalid response")
        return result


