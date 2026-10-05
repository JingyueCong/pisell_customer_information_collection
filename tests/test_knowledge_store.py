from __future__ import annotations

import unittest

from merchant_profile_agent.knowledge_store import P1KnowledgeStore
from merchant_profile_agent.validation import ProfileValidationError


class FakeCommitter:
    def __init__(self) -> None:
        self.committed = None

    def read_merchant_profile(self, **kwargs):
        return {
            "merchant": {"name": kwargs["merchant_name"], "id": kwargs["merchant_id"]},
            "fields": {},
            "event_count": 0,
            "digest": "digest",
        }

    def commit(self, **kwargs):
        self.committed = kwargs
        return {
            "store_status": "stored",
            "records_written": 2,
            "wiki_documents_updated": 2,
        }


def make_store() -> P1KnowledgeStore:
    store = object.__new__(P1KnowledgeStore)
    store.agent = {"id": "operation-agent"}
    store.committer = FakeCommitter()
    return store


def valid_request():
    return {
        "confirmation": "结束并保存",
        "conversation_id": "feishu:test",
        "merchant": {"name": "青禾便当", "id": None},
        "messages": [
            {
                "role": "user",
                "content": "青禾便当的联系人是林经理",
                "source_ref": "feishu://chat/oc/message/om",
            }
        ],
        "updates": [
            {
                "field_path": "contacts.primary.name",
                "value": "林经理",
                "source_ref": "feishu://chat/oc/message/om",
                "replace_confirmed": False,
                "state": "explicit",
                "document_type": "business-contacts",
            }
        ],
    }


class KnowledgeStoreTests(unittest.TestCase):
    def test_commit_requires_exact_confirmation(self) -> None:
        store = make_store()
        request = valid_request()
        request["confirmation"] = "保存一下"

        with self.assertRaises(ProfileValidationError):
            store.commit_profile(request)

        self.assertIsNone(store.committer.committed)

    def test_commit_builds_operation_event_snapshot(self) -> None:
        store = make_store()

        result = store.commit_profile(valid_request())

        self.assertTrue(result["write_performed"])
        close = store.committer.committed["close_result"]
        self.assertEqual(close["business_objects"]["merchant"]["name"], "青禾便当")
        self.assertEqual(close["specific"]["operation_type"], "create")
        self.assertEqual(
            close["specific"]["profile_updates"][0]["state"], "explicit"
        )


if __name__ == "__main__":
    unittest.main()
