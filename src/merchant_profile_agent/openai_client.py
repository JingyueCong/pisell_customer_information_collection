from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping


class OpenAIClientError(RuntimeError):
    pass


class OpenAIResponsesClient:
    endpoint = "https://api.openai.com/v1/responses"

    def __init__(self, api_key: str, model: str, timeout: int = 60) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def create_structured(
        self,
        *,
        system_prompt: str,
        user_input: str,
        schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "store": False,
            "reasoning": {"effort": "low"},
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "merchant_profile_intake",
                    "strict": True,
                    "schema": dict(schema),
                }
            },
            "max_output_tokens": 2_500,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise OpenAIClientError(f"OpenAI API returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise OpenAIClientError("OpenAI API request failed") from exc
        try:
            response_data = json.loads(raw)
            output_text = self._output_text(response_data)
            result = json.loads(output_text)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise OpenAIClientError("OpenAI API returned an invalid structured response") from exc
        if not isinstance(result, dict):
            raise OpenAIClientError("OpenAI structured response must be an object")
        return result

    @staticmethod
    def _output_text(response: Mapping[str, Any]) -> str:
        direct = response.get("output_text")
        if isinstance(direct, str) and direct:
            return direct
        for item in response.get("output", []):
            if not isinstance(item, Mapping) or item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if (
                    isinstance(content, Mapping)
                    and content.get("type") == "output_text"
                    and isinstance(content.get("text"), str)
                ):
                    return str(content["text"])
        raise ValueError("response contains no output text")

