from __future__ import annotations

import unittest
from types import SimpleNamespace

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
    store.agent = {"id": "operation-agent", "department": "operation"}
    store.committer = FakeCommitter()
    store._read_merchant_documents = lambda **_: []  # type: ignore[method-assign]
    return store


def valid_request():
    return {
        "confirmation": "结束并保存",
        "conversation_id": "feishu:test",
        "merchant": {"name": "青禾便当", "id": None},
        "messages": [
            {
                "role": "user",
                "content": "你好，今天挺忙的。青禾便当的联系人是林经理，麻烦了。",
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
    def test_lists_existing_pages_from_merchant_wiki(self) -> None:
        class Routes:
            def resolve_canonical_document_route(self, department, agent_id):
                return {
                    "space_id": "space-1",
                    "root_parent_node_token": "root",
                    "path_template": [
                        {"kind": "object", "object_kind": "merchant"}
                    ],
                    "documents": [
                        {
                            "document_type": "merchant-overview",
                            "title": "00｜商家总览",
                        },
                        {
                            "document_type": "business-contacts",
                            "title": "01｜业务与联系人",
                        },
                    ],
                }

            def resolve_business_object_identity(self, *args, **kwargs):
                return {"object_name": "青禾便当"}

        class Client:
            def wiki_children(self, space_id, parent):
                nodes = {
                    "root": [
                        {"title": "青禾便当", "node_token": "merchant-node"}
                    ],
                    "merchant-node": [
                        {"title": "00｜商家总览", "node_token": "overview"},
                        {"title": "01｜业务与联系人", "node_token": "contacts"},
                    ],
                }
                return nodes.get(parent, [])

        class Committer:
            routes = Routes()
            client = Client()
            config = SimpleNamespace(
                knowledge_projection={
                    "routing_profiles": {
                        "merchant_profile_workspace": {
                            "node_token": "root",
                            "root_url": "https://tenant.feishu.cn/wiki/root",
                        }
                    }
                }
            )

            @staticmethod
            def _space_id(route):
                return route["space_id"]

            @staticmethod
            def _exact_child(children, title):
                matches = [item for item in children if item.get("title") == title]
                return matches[0] if len(matches) == 1 else None

        store = object.__new__(P1KnowledgeStore)
        store.agent = {"id": "operation-agent", "department": "operation"}
        store.committer = Committer()

        documents = store._read_merchant_documents(
            merchant_name="青禾便当", merchant_id="crm-1"
        )

        self.assertEqual(
            [item["node_token"] for item in documents],
            ["merchant-node", "overview", "contacts"],
        )
        self.assertTrue(
            all(
                item["url"].startswith("https://tenant.feishu.cn/wiki/")
                for item in documents
            )
        )

    def test_read_includes_current_wiki_documents(self) -> None:
        store = make_store()
        store._read_merchant_documents = lambda **_: [  # type: ignore[method-assign]
            {
                "document_type": "merchant-directory",
                "label": "商户资料目录",
                "url": "https://example.feishu.cn/wiki/merchant",
            },
            {
                "document_type": "business-contacts",
                "label": "01｜业务与联系人",
                "url": "https://example.feishu.cn/wiki/contacts",
            },
        ]

        result = store.read_profile(
            {"merchant_name": "青禾便当", "merchant_id": "crm-1"}
        )

        self.assertEqual(len(result["profile"]["documents"]), 2)
        self.assertEqual(
            result["profile"]["documents"][0]["label"], "商户资料目录"
        )

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

    def test_commit_preserves_crm_name_aliases_in_merchant_identity(self) -> None:
        store = make_store()
        request = valid_request()
        request["merchant"] = {
            "name": "9262 U Bowl Melbourne Central （海底捞）",
            "id": "6856334446",
            "aliases": ["U Bowl Malatang"],
        }

        store.commit_profile(request)

        close = store.committer.committed["close_result"]
        self.assertEqual(
            close["business_objects"]["merchant"],
            {
                "name": "9262 U Bowl Melbourne Central （海底捞）",
                "id": "6856334446",
                "aliases": ["U Bowl Malatang"],
            },
        )
        self.assertEqual(close["specific"]["operation_type"], "create")
        self.assertEqual(
            close["specific"]["profile_updates"][0]["state"], "explicit"
        )
        history = store.committer.committed["message_history"]
        self.assertEqual(
            history,
            [{"role": "user", "content": "contacts.primary.name: 林经理"}],
        )
        self.assertNotIn("今天挺忙", str(history))


if __name__ == "__main__":
    unittest.main()
