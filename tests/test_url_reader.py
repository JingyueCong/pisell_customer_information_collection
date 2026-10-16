from __future__ import annotations

import unittest
from email.message import Message
from unittest.mock import patch

from merchant_profile_agent.url_reader import (
    PublicUrlReader,
    UrlReadError,
    extract_public_urls,
    validate_public_https_url,
)


class FakeResponse:
    def __init__(self, body: bytes, *, url: str, content_type: str = "text/html") -> None:
        self.body = body
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type + "; charset=utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self) -> str:
        return self.url

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response


class UrlReaderTests(unittest.TestCase):
    def test_extracts_distinct_https_urls_and_trims_chat_punctuation(self) -> None:
        urls = extract_public_urls(
            "项目：https://example.com/project，菜单：https://example.com/menu。"
        )

        self.assertEqual(
            urls,
            ["https://example.com/project", "https://example.com/menu"],
        )

    def test_rejects_private_hosts_credentials_and_non_https(self) -> None:
        for url in (
            "http://example.com/",
            "https://127.0.0.1/admin",
            "https://user:password@example.com/",
            "https://localhost/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(UrlReadError):
                    validate_public_https_url(url)

    @patch(
        "merchant_profile_agent.url_reader._public_addresses",
        return_value=["93.184.216.34"],
    )
    def test_reads_final_page_title_visible_text_and_links(self, _addresses) -> None:
        response = FakeResponse(
            b"""
            <html><head><title>My PiSell Shop</title><style>secret</style></head>
            <body><h1>Merchant login</h1><p>Welcome to PiSell.</p>
            <a href="/help">Help centre</a><script>ignore me</script></body></html>
            """,
            url="https://my.pisell.com/login",
        )
        opener = FakeOpener(response)
        reader = PublicUrlReader(opener_factory=lambda *_handlers: opener)

        result = reader.read("https://u26197356.ct.sendgrid.net/click")

        self.assertEqual(result["final_url"], "https://my.pisell.com/login")
        self.assertEqual(result["title"], "My PiSell Shop")
        self.assertEqual(result["status"], "login_required")
        self.assertIn("Welcome to PiSell", result["text"])
        self.assertNotIn("ignore me", result["text"])
        self.assertEqual(result["links"][0]["url"], "https://my.pisell.com/help")

    @patch(
        "merchant_profile_agent.url_reader._public_addresses",
        return_value=["93.184.216.34"],
    )
    def test_read_messages_keeps_failure_visible_without_blocking_analysis(
        self, _addresses
    ) -> None:
        response = FakeResponse(
            b"binary",
            url="https://example.com/file.zip",
            content_type="application/zip",
        )
        opener = FakeOpener(response)
        reader = PublicUrlReader(opener_factory=lambda *_handlers: opener)

        evidence = reader.read_messages(
            [{"role": "user", "content": "资料在 https://example.com/file.zip"}]
        )

        self.assertEqual(evidence[0]["status"], "unavailable")
        self.assertIn("application/zip", evidence[0]["reason"])


if __name__ == "__main__":
    unittest.main()
