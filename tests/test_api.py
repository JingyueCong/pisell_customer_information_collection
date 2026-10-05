from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from merchant_profile_agent.api import create_server
from merchant_profile_agent.settings import Settings


class FakeAgent:
    def analyze(self, request):
        return {"merchant": {"name": "青禾便当", "id": None}, "write_performed": False}


class FakeKnowledgeStore:
    name = "fake"

    def read_profile(self, request):
        return {"profile": {"merchant": {"name": request["merchant_name"]}}, "write_performed": False}

    def commit_profile(self, request):
        return {
            "write_performed": True,
            "result": {"store_status": "stored", "wiki_documents_updated": 2},
        }


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = Settings(
            api_key="a" * 24,
            openai_api_key="test-openai-key",
            host="127.0.0.1",
            port=0,
        )
        self.server = create_server(
            settings, FakeAgent(), FakeKnowledgeStore()  # type: ignore[arg-type]
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, payload=None, *, token=None):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())

    def test_health_is_public(self) -> None:
        status, result = self.request("/healthz")

        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["knowledge_store"], "fake")

    def test_analyze_requires_bearer_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/v1/profile/analyze", {"messages": []})

        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()

    def test_analyze_returns_request_id_and_no_write(self) -> None:
        status, result = self.request(
            "/v1/profile/analyze",
            {"messages": [{"role": "user", "content": "hello"}]},
            token="a" * 24,
        )

        self.assertEqual(status, 200)
        self.assertFalse(result["write_performed"])
        self.assertIn("request_id", result)

    def test_validate_detects_existing_value_conflict(self) -> None:
        status, result = self.request(
            "/v1/profile/validate",
            {
                "merchant_name": "青禾便当",
                "updates": [
                    {
                        "field_path": "contacts.primary.name",
                        "value": "陈经理",
                        "source_ref": "crm://note/456",
                        "replace_confirmed": False,
                    }
                ],
                "current_profile": {"contacts.primary.name": "林经理"},
            },
            token="a" * 24,
        )

        self.assertEqual(status, 200)
        self.assertEqual(result["save_readiness"], "needs_confirmation")
        self.assertEqual(result["conflicts"][0]["current_value"], "林经理")

    def test_read_uses_shared_knowledge_store(self) -> None:
        status, result = self.request(
            "/v1/profile/read",
            {"merchant_name": "青禾便当", "merchant_id": None},
            token="a" * 24,
        )

        self.assertEqual(status, 200)
        self.assertEqual(result["profile"]["merchant"]["name"], "青禾便当")
        self.assertFalse(result["write_performed"])

    def test_commit_uses_shared_knowledge_store(self) -> None:
        status, result = self.request(
            "/v1/profile/commit",
            {"confirmation": "结束并保存"},
            token="a" * 24,
        )

        self.assertEqual(status, 200)
        self.assertTrue(result["write_performed"])


if __name__ == "__main__":
    unittest.main()
