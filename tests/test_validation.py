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
    def test_new_explicit_update_is_ready(self) -> None:
        result = validate_update_set("青禾便当", [update()], {})

        self.assertEqual(result["save_readiness"], "ready")
        self.assertEqual(result["affected_modules"], ["business-contacts"])
        self.assertEqual(result["profile_updates"][0]["state"], "explicit")
        self.assertFalse(result["write_performed"])

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
