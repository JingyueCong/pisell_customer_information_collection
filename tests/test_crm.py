from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from merchant_profile_agent.crm import (
    CrmResolverUnavailable,
    MeegleCrmResolver,
    TicketBridgeCrmResolver,
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
    def test_common_business_suffix_is_removed_for_safe_fallback(self, run) -> None:
        run.side_effect = [
            completed({"rows": []}),
            completed({"rows": []}),
            completed(
                {
                    "rows": [
                        {
                            "work_item_id": "6856334446",
                            "name": "9262 U Bowl Melbourne Central （海底捞）",
                        }
                    ]
                }
            ),
        ]

        result = self.make_resolver().resolve({"merchant_name": "U Bowl Malatang"})

        self.assertEqual(result["status"], "unique")
        self.assertEqual(result["match_method"], "shortened_name")
        last_command = run.call_args_list[-1].args[0]
        mql = last_command[last_command.index("--mql") + 1]
        self.assertIn("LIKE '%U Bowl%'", mql)

    @patch("merchant_profile_agent.crm.subprocess.run")
    def test_bound_id_accepts_a_safe_shortened_business_name(self, run) -> None:
        run.return_value = completed(
            {
                "rows": [
                    {
                        "work_item_id": "6856334446",
                        "name": "9262 U Bowl Melbourne Central （海底捞）",
                    }
                ]
            }
        )

        result = self.make_resolver().resolve(
            {"merchant_name": "U Bowl Malatang", "merchant_id": "6856334446"}
        )

        self.assertEqual(result["status"], "unique")
        self.assertEqual(result["match_method"], "work_item_id")
        self.assertEqual(result["candidates"][0]["work_item_id"], "6856334446")
        self.assertEqual(run.call_count, 1)

    @patch("merchant_profile_agent.crm.subprocess.run")
    def test_cli_failure_is_fail_closed(self, run) -> None:
        run.return_value = completed({}, returncode=1)

        with self.assertRaises(CrmResolverUnavailable):
            self.make_resolver().resolve({"merchant_name": "汪汪咖啡店"})

    @patch("merchant_profile_agent.crm.urllib.request.urlopen")
    def test_ticket_bridge_reuses_read_only_crm_response(self, urlopen) -> None:
        response = MagicMock()
        response.read.return_value = json.dumps(
                {
                    "ok": True,
                    "crm": {
                        "status": "unique",
                        "query": {"merchant_name": "汪汪咖啡店"},
                        "match_method": "exact_name",
                        "candidates": [
                            {
                                "work_item_id": "123456789",
                                "name": "汪汪咖啡店",
                                "url": "https://project.feishu.cn/v2qint/crm/detail/123456789",
                            }
                        ],
                        "read_only": True,
                    },
                },
                ensure_ascii=False,
            ).encode("utf-8")
        response.__enter__.return_value = response
        urlopen.return_value = response
        resolver = TicketBridgeCrmResolver(
            base_url="http://127.0.0.1:8787",
            token="a" * 32,
        )

        result = resolver.resolve(
            {"merchant_name": "汪汪咖啡店", "lookup_hint": "store-1002"}
        )

        self.assertEqual(result["status"], "unique")
        self.assertTrue(result["read_only"])
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8787/internal/customer-crm/resolve",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer " + "a" * 32)
        self.assertEqual(
            json.loads(request.data),
            {"merchant_name": "汪汪咖啡店", "lookup_hint": "store-1002"},
        )

    @patch("merchant_profile_agent.crm.urllib.request.urlopen")
    def test_ticket_bridge_retries_a_common_business_suffix(self, urlopen) -> None:
        def response(payload):
            value = MagicMock()
            value.read.return_value = json.dumps(payload).encode("utf-8")
            value.__enter__.return_value = value
            return value

        urlopen.side_effect = [
            response(
                {
                    "ok": True,
                    "crm": {
                        "status": "not_found",
                        "query": {"merchant_name": "U Bowl Malatang"},
                        "match_method": "fuzzy_name",
                        "candidates": [],
                        "read_only": True,
                    },
                }
            ),
            response(
                {
                    "ok": True,
                    "crm": {
                        "status": "unique",
                        "query": {"merchant_name": "U Bowl"},
                        "match_method": "fuzzy_name",
                        "candidates": [
                            {
                                "work_item_id": "6856334446",
                                "name": "9262 U Bowl Melbourne Central （海底捞）",
                            }
                        ],
                        "read_only": True,
                    },
                }
            ),
        ]
        resolver = TicketBridgeCrmResolver(
            base_url="http://127.0.0.1:8787",
            token="a" * 32,
        )

        result = resolver.resolve({"merchant_name": "U Bowl Malatang"})

        self.assertEqual(result["status"], "unique")
        self.assertEqual(result["match_method"], "shortened_name")
        self.assertEqual(result["matched_query"], "U Bowl")
        second_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(json.loads(second_request.data), {"merchant_name": "U Bowl"})

    @patch("merchant_profile_agent.crm.urllib.request.urlopen")
    def test_ticket_bridge_drops_store_code_and_does_not_treat_it_as_work_item_id(
        self, urlopen
    ) -> None:
        def response(payload):
            value = MagicMock()
            value.read.return_value = json.dumps(payload).encode("utf-8")
            value.__enter__.return_value = value
            return value

        urlopen.side_effect = [
            response(
                {
                    "ok": True,
                    "crm": {"status": "not_found", "candidates": [], "read_only": True},
                }
            ),
            response(
                {
                    "ok": True,
                    "crm": {
                        "status": "unique",
                        "candidates": [
                            {
                                "work_item_id": "6856334446",
                                "name": "9262 U Bowl Melbourne Central （海底捞）",
                            }
                        ],
                        "read_only": True,
                    },
                }
            ),
        ]
        resolver = TicketBridgeCrmResolver(
            base_url="http://127.0.0.1:8787",
            token="a" * 32,
        )

        result = resolver.resolve(
            {
                "merchant_name": "9262 U Bowl Melbourne Central（海底捞）",
                "merchant_id": "9262",
            }
        )

        self.assertEqual(result["status"], "unique")
        self.assertEqual(result["candidates"][0]["work_item_id"], "6856334446")
        retry = json.loads(urlopen.call_args_list[1].args[0].data)
        self.assertEqual(
            retry,
            {"merchant_name": "U Bowl Melbourne Central（海底捞）"},
        )

    @patch("merchant_profile_agent.crm.urllib.request.urlopen")
    def test_ticket_bridge_rechecks_the_same_bound_id_with_shortened_name(
        self, urlopen
    ) -> None:
        def response(payload):
            value = MagicMock()
            value.read.return_value = json.dumps(payload).encode("utf-8")
            value.__enter__.return_value = value
            return value

        urlopen.side_effect = [
            response(
                {
                    "ok": True,
                    "crm": {
                        "status": "not_found",
                        "candidates": [],
                        "read_only": True,
                    },
                }
            ),
            response(
                {
                    "ok": True,
                    "crm": {
                        "status": "unique",
                        "candidates": [
                            {
                                "work_item_id": "6856334446",
                                "name": "9262 U Bowl Melbourne Central （海底捞）",
                            }
                        ],
                        "read_only": True,
                    },
                }
            ),
        ]
        resolver = TicketBridgeCrmResolver(
            base_url="http://127.0.0.1:8787",
            token="a" * 32,
        )

        result = resolver.resolve(
            {"merchant_name": "U Bowl Malatang", "merchant_id": "6856334446"}
        )

        self.assertEqual(result["status"], "unique")
        self.assertEqual(result["match_method"], "work_item_id_shortened_name")
        self.assertEqual(result["matched_query"], "U Bowl")
        second_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(
            json.loads(second_request.data),
            {"merchant_name": "U Bowl", "lookup_hint": "6856334446"},
        )

    @patch("merchant_profile_agent.crm.urllib.request.urlopen")
    def test_ticket_bridge_does_not_accept_a_different_id_on_retry(self, urlopen) -> None:
        def response(payload):
            value = MagicMock()
            value.read.return_value = json.dumps(payload).encode("utf-8")
            value.__enter__.return_value = value
            return value

        urlopen.side_effect = [
            response(
                {"ok": True, "crm": {"status": "not_found", "candidates": []}}
            ),
            response(
                {
                    "ok": True,
                    "crm": {
                        "status": "unique",
                        "candidates": [
                            {"work_item_id": "9999999999", "name": "U Bowl Other"}
                        ],
                    },
                }
            ),
        ]
        resolver = TicketBridgeCrmResolver(
            base_url="http://127.0.0.1:8787",
            token="a" * 32,
        )

        result = resolver.resolve(
            {"merchant_name": "U Bowl Malatang", "merchant_id": "6856334446"}
        )

        self.assertEqual(result["status"], "not_found")


if __name__ == "__main__":
    unittest.main()
