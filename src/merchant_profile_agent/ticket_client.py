from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping


class TicketAgentError(RuntimeError):
    pass


class TicketAgentClient:
    def __init__(self, *, base_url: str, token: str, timeout: int = 660) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def create_customer_service_ticket(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            self.base_url + "/internal/customer-service-tickets",
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
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
            except (json.JSONDecodeError, UnicodeDecodeError):
                code = "request_failed"
            raise TicketAgentError(str(code)) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise TicketAgentError("ticket agent is unavailable") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise TicketAgentError("ticket agent returned an invalid response")
        if not isinstance(result.get("reply"), str) or not result["reply"].strip():
            raise TicketAgentError("ticket agent returned an empty reply")
        if not isinstance(result.get("draft_open"), bool):
            raise TicketAgentError("ticket agent returned an invalid draft state")
        work_item_ids = result.get("work_item_ids", [])
        if not isinstance(work_item_ids, list) or not all(
            isinstance(item, str) for item in work_item_ids
        ):
            raise TicketAgentError("ticket agent returned invalid work item IDs")
        return result
