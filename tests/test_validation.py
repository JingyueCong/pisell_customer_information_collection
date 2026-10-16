from __future__ import annotations

import unittest

from merchant_profile_agent.validation import (
    ProfileValidationError,
    validate_update_set,
)


def update(**overrides):
    value = {
        "field_path": "contacts.primary.name",
        "value": "林经理",
        "source_ref": "crm://note/123",
        "replace_confirmed": False,
    }
    value.update(overrides)
    return value


class ValidationTests(unittest.TestCase):
    def test_common_contact_aliases_are_canonicalized(self) -> None:
        result = validate_update_set(
            "青禾便当",
            [update(field_path="contacts.phone", value="0400000000")],
            {},
        )

        self.assertEqual(
            result["profile_updates"][0]["field_path"],
            "contacts.primary.phone",
        )

    def test_alias_conflicts_with_existing_canonical_field(self) -> None:
        result = validate_update_set(
            "青禾便当",
            [update(field_path="contacts.phone", value="0400000001")],
            {"fields": {"contacts.primary.phone": {"value": "0400000000"}}},
        )

        self.assertEqual(result["save_readiness"], "needs_confirmation")
        self.assertEqual(
            result["conflicts"][0]["field_path"],
            "contacts.primary.phone",
        )

    def test_new_explicit_update_is_ready(self) -> None:
        result = validate_update_set("青禾便当", [update()], {})

        self.assertEqual(result["save_readiness"], "ready")
        self.assertEqual(result["affected_modules"], ["business-contacts"])
        self.assertEqual(result["profile_updates"][0]["state"], "explicit")
        self.assertFalse(result["write_performed"])

    def test_foundational_and_supplemental_page_categories_are_supported(self) -> None:
        cases = {
            "contacts.primary.name": "business-contacts",
            "menu.url": "menu-content",
            "system.customer_flow.start_screen": "system-configuration",
            "hardware.devices": "hardware-installation",
            "risk.materials": "risk-compliance",
            "commercial.notes": "commercial-information",
            "stores.primary.address": "store-profile",
            "project.feishu_link": "project-progress",
            "collection.merchant_form_url": "data-collection",
            "package.type": "package-commercial",
            "hardware_record.devices": "hardware-records",
            "card_terminal.surcharge": "card-terminal",
            "special.communication_focus": "special-matters",
            "menu_collection.url": "menu-collection",
            "archive.folder_url": "document-archive",
            "communication.preferences": "communication-profile",
            "content_setup.notes": "content-settings",
        }
        for field_path, document_type in cases.items():
            with self.subTest(field_path=field_path):
                result = validate_update_set(
                    "青禾便当",
                    [update(field_path=field_path, value="测试值")],
                    {},
                )
                self.assertEqual(result["affected_modules"], [document_type])

    def test_same_value_is_unchanged(self) -> None:
        result = validate_update_set(
            "青禾便当", [update()], {"contacts.primary.name": "林经理"}
        )

        self.assertEqual(result["profile_updates"], [])
        self.assertEqual(result["unchanged"][0]["field_path"], "contacts.primary.name")

    def test_conflict_requires_confirmation(self) -> None:
        result = validate_update_set(
            "青禾便当", [update(value="陈经理")], {"contacts.primary.name": "林经理"}
        )

        self.assertEqual(result["save_readiness"], "needs_confirmation")
        self.assertEqual(result["conflicts"][0]["current_value"], "林经理")

    def test_confirmed_replacement_is_accepted(self) -> None:
        result = validate_update_set(
            "青禾便当",
            [update(value="陈经理", replace_confirmed=True)],
            {"contacts.primary.name": "林经理"},
        )

        self.assertEqual(result["save_readiness"], "ready")

    def test_sensitive_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProfileValidationError, "sensitive data"):
            validate_update_set("青禾便当", [update(field_path="system.api_key")], {})

    def test_sensitive_value_is_rejected(self) -> None:
        for value in (
            "后台密码是 hunter2",
            "Authorization: Bearer secret-token",
            "付款卡号 4111 1111 1111 1111",
            "-----BEGIN PRIVATE KEY-----",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ProfileValidationError, "contains sensitive data"):
                    validate_update_set(
                        "青禾便当", [update(field_path="system.notes", value=value)], {}
                    )

    def test_duplicate_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProfileValidationError, "duplicate field paths"):
            validate_update_set("青禾便当", [update(), update()], {})


if __name__ == "__main__":
    unittest.main()
