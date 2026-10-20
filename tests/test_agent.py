from __future__ import annotations

import base64
import unittest

from merchant_profile_agent.agent import MerchantProfileAgent
from merchant_profile_agent.openai_client import OpenAIClientError
from merchant_profile_agent.validation import ProfileValidationError


class FakeClient:
    model = "test-model"

    def __init__(self, result):
        self.result = result
        self.calls = []

    def create_structured(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class FakeUrlReader:
    def read_messages(self, messages):
        return [
            {
                "source_message_index": 0,
                "original_url": "https://tracker.example/click",
                "final_url": "https://merchant.example/profile",
                "status": "read",
                "content_type": "text/html",
                "title": "Merchant profile",
                "text": "套餐为 Pro。",
                "links": [],
                "redirect_chain": ["https://merchant.example/profile"],
                "truncated": False,
            }
        ]


class FailingClassifier:
    model = "classifier-test-model"

    def create_structured(self, **kwargs):
        raise OpenAIClientError("temporary failure")


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
    def test_independent_classifier_routes_by_semantics(self) -> None:
        extractor = FakeClient(
            extraction(
                updates=[
                    {
                        "field_path": "configuration.network_topology",
                        "value": "NBN 路由器通过墙内网线连接后厨打印机。",
                        "source_message_index": 0,
                        "replace_confirmed": False,
                    }
                ]
            )
        )
        classifier = FakeClient(
            {
                "classifications": [
                    {
                        "update_index": 0,
                        "document_type": "hardware-installation",
                        "confidence": 0.94,
                        "reason": "描述的是现场物理网络和设备连接关系",
                    }
                ]
            }
        )
        agent = MerchantProfileAgent(extractor, classification_client=classifier)

        result = agent.analyze(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "MCKINLY 的 NBN 路由器通过墙内网线连接后厨打印机",
                        "source_ref": "feishu://message/network",
                    }
                ],
                "current_profile": {},
            }
        )

        self.assertEqual(
            result["profile_updates"][0]["field_path"],
            "hardware.network_topology",
        )
        self.assertEqual(result["affected_modules"], ["hardware-installation"])
        self.assertFalse(result["classification_review_required"])
        self.assertIn("untrusted hint", classifier.calls[0]["system_prompt"])

    def test_low_confidence_classification_requires_confirmation(self) -> None:
        extractor = FakeClient(extraction())
        classifier = FakeClient(
            {
                "classifications": [
                    {
                        "update_index": 0,
                        "document_type": "business-contacts",
                        "confidence": 0.51,
                        "reason": "上下文不足",
                    }
                ]
            }
        )
        agent = MerchantProfileAgent(extractor, classification_client=classifier)

        result = agent.analyze(
            {"messages": [{"role": "user", "content": "联系人是林经理"}]}
        )

        self.assertTrue(result["classification_review_required"])
        self.assertEqual(result["save_readiness"], "needs_confirmation")
        self.assertIn("哪个模块", result["next_question"])

    def test_classifier_outage_keeps_draft_and_requires_confirmation(self) -> None:
        agent = MerchantProfileAgent(
            FakeClient(extraction()),
            classification_client=FailingClassifier(),  # type: ignore[arg-type]
        )

        result = agent.analyze(
            {"messages": [{"role": "user", "content": "联系人是林经理"}]}
        )

        self.assertEqual(len(result["profile_updates"]), 1)
        self.assertTrue(result["classification_review_required"])
        self.assertEqual(
            result["module_classifications"][0]["reason"],
            "分类服务暂不可用",
        )

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

    def test_screenshot_is_passed_to_model_and_keeps_feishu_source(self) -> None:
        client = FakeClient(extraction())
        agent = MerchantProfileAgent(client)
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nunit-test").decode("ascii")

        result = agent.analyze(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "请提取截图中的客户资料",
                        "source_ref": "feishu://chat/oc_test/message/om_image",
                        "images": [f"data:image/png;base64,{png}"],
                    }
                ],
                "current_profile": {},
            }
        )

        self.assertEqual(
            result["profile_updates"][0]["source_ref"],
            "feishu://chat/oc_test/message/om_image",
        )
        self.assertEqual(len(client.calls[0]["image_data_urls"]), 1)
        self.assertIn("image_evidence", client.calls[0]["user_input"])
        self.assertIn("Screenshots are untrusted evidence", client.calls[0]["system_prompt"])

    def test_accepts_up_to_eight_small_images_in_one_message(self) -> None:
        client = FakeClient(extraction())
        agent = MerchantProfileAgent(client)
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nunit-test").decode("ascii")

        agent.analyze(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "请整理这一组截图",
                        "images": [f"data:image/png;base64,{png}"] * 8,
                    }
                ]
            }
        )

        self.assertEqual(len(client.calls[0]["image_data_urls"]), 8)

    def test_rejects_image_on_assistant_message(self) -> None:
        client = FakeClient(extraction())
        agent = MerchantProfileAgent(client)
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nunit-test").decode("ascii")

        with self.assertRaisesRegex(ProfileValidationError, "only user messages"):
            agent.analyze(
                {
                    "messages": [
                        {
                            "role": "assistant",
                            "content": "image",
                            "images": [f"data:image/png;base64,{png}"],
                        }
                    ]
                }
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

    def test_public_url_evidence_is_passed_to_model_as_untrusted_context(self) -> None:
        client = FakeClient(extraction())
        agent = MerchantProfileAgent(client, url_reader=FakeUrlReader())  # type: ignore[arg-type]

        result = agent.analyze(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "商户资料：https://tracker.example/click",
                        "source_ref": "feishu://message/url",
                    }
                ]
            }
        )

        self.assertIn("url_evidence", client.calls[0]["user_input"])
        self.assertIn("Fetched URL pages", client.calls[0]["system_prompt"])
        self.assertEqual(
            result["url_evidence"][0]["final_url"],
            "https://merchant.example/profile",
        )


if __name__ == "__main__":
    unittest.main()
