from __future__ import annotations

import unittest

from merchant_profile_agent.agent import MerchantProfileAgent
from merchant_profile_agent.validation import ProfileValidationError


class FakeClient:
    model = "test-model"

    def __init__(self, result):
        self.result = result
        self.calls = []

    def create_structured(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def extraction(**overrides):
    value = {
        "merchant_name": "青禾便当",
        "merchant_id": None,
        "intent": "update",
        "reply": "已整理联系人信息。",
        "updates": [
            {
                "field_path": "contacts.primary.name",
                "value": "林经理",
                "source_message_index": 0,
                "replace_confirmed": False,
            }
        ],
        "assumptions": [],
        "information_gaps": [],
        "next_question": None,
        "summary_points": ["联系人已确认"],
        "decisions": [],
        "action_items": [],
    }
    value.update(overrides)
    return value


class AgentTests(unittest.TestCase):
    def test_uses_caller_source_and_never_writes(self) -> None:
        client = FakeClient(extraction())
        agent = MerchantProfileAgent(client)

        result = agent.analyze(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "青禾便当联系人是林经理",
                        "source_ref": "crm://note/123",
                    }
                ],
                "current_profile": {},
            }
        )

        self.assertEqual(result["profile_updates"][0]["source_ref"], "crm://note/123")
        self.assertEqual(result["save_readiness"], "ready")
        self.assertFalse(result["write_performed"])
        self.assertEqual(result["model"], "test-model")
        self.assertIn(
            "Keep the knowledge base minimal",
            client.calls[0]["system_prompt"],
        )

    def test_rejects_assistant_message_as_fact_source(self) -> None:
        client = FakeClient(
            extraction(
                updates=[
                    {
                        "field_path": "contacts.primary.name",
                        "value": "林经理",
                        "source_message_index": 1,
                        "replace_confirmed": False,
                    }
                ]
            )
        )
        agent = MerchantProfileAgent(client)

        with self.assertRaisesRegex(ProfileValidationError, "user messages"):
            agent.analyze(
                {
                    "messages": [
                        {"role": "user", "content": "请整理"},
                        {"role": "assistant", "content": "联系人可能是林经理"},
                    ]
                }
            )


if __name__ == "__main__":
    unittest.main()
