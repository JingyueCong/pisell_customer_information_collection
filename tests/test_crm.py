from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from merchant_profile_agent.crm import (
    CrmResolverUnavailable,
    MeegleCrmResolver,
)


def completed(payload, returncode=0):
    return subprocess.CompletedProcess(
        args=["meegle"], returncode=returncode, stdout=json.dumps(payload), stderr=""
    )


class CrmResolverTests(unittest.TestCase):
    def make_resolver(self) -> MeegleCrmResolver:
        return MeegleCrmResolver(
            executable="/opt/bin/meegle",
            project_key="v2qint",
            work_item_type="crm",
            profile="merchant-bot",
            url_template="https://project.feishu.cn/v2qint/crm/detail/{work_item_id}",
        )

    @patch("merchant_profile_agent.crm.subprocess.run")
    def test_unique_exact_match_returns_read_only_binding(self, run) -> None:
        run.return_value = completed(
            {
                "list": [
                    {
                        "group_infos": [
                            {
                                "data": [
                                    {
                                        "work_item_id": "123456789",
                                        "name": "汪汪咖啡店",
                                        "work_item_status": "跟进中",
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        )

        result = self.make_resolver().resolve({"merchant_name": "汪汪咖啡店"})

        self.assertEqual(result["status"], "unique")
        self.assertTrue(result["read_only"])
        self.assertEqual(result["candidates"][0]["work_item_id"], "123456789")
        self.assertEqual(
            result["candidates"][0]["url"],
            "https://project.feishu.cn/v2qint/crm/detail/123456789",
        )
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["/opt/bin/meegle", "--profile", "merchant-bot"])
        mql = command[command.index("--mql") + 1]
        self.assertIn("`name` = '汪汪咖啡店'", mql)

    @patch("merchant_profile_agent.crm.subprocess.run")
    def test_store_hint_narrows_multiple_name_matches(self, run) -> None:
        run.side_effect = [
            completed(
                {
                    "rows": [
                        {"work_item_id": "101", "name": "store-1001 汪汪咖啡店"},
                        {"work_item_id": "102", "name": "store-1002 汪汪咖啡店"},
                    ]
                }
            ),
            completed(
                {"rows": [{"work_item_id": "102", "name": "store-1002 汪汪咖啡店"}]}
            ),
        ]

        result = self.make_resolver().resolve(
            {"merchant_name": "汪汪咖啡店", "lookup_hint": "store-1002"}
        )

        self.assertEqual(result["status"], "unique")
        self.assertEqual(result["match_method"], "name_and_hint")
        self.assertEqual(result["candidates"][0]["work_item_id"], "102")
        second_command = run.call_args_list[1].args[0]
        mql = second_command[second_command.index("--mql") + 1]
        self.assertIn("AND", mql)
        self.assertIn("store-1002", mql)

    @patch("merchant_profile_agent.crm.subprocess.run")
    def test_no_match_is_explicit_and_does_not_create(self, run) -> None:
        run.side_effect = [completed({"rows": []}), completed({"rows": []})]

        result = self.make_resolver().resolve({"merchant_name": "不存在商户"})

        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["candidates"], [])
        self.assertTrue(all("create" not in call.args[0] for call in run.call_args_list))

    @patch("merchant_profile_agent.crm.subprocess.run")
    def test_cli_failure_is_fail_closed(self, run) -> None:
        run.return_value = completed({}, returncode=1)

        with self.assertRaises(CrmResolverUnavailable):
            self.make_resolver().resolve({"merchant_name": "汪汪咖啡店"})


if __name__ == "__main__":
    unittest.main()
