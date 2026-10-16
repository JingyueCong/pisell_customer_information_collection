from __future__ import annotations

import html
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable, Mapping, Sequence


URL_PATTERN = re.compile(
    r"https://[^\s<>\"'\]\[{}，。；：！？、（）【】《》]+",
    re.IGNORECASE,
)
TRAILING_PUNCTUATION = ".,;:!?，。；：！？、）)】]》>"
SUPPORTED_CONTENT_TYPES = {
    "text/html",
    "text/plain",
    "application/json",
    "application/ld+json",
}
LOGIN_MARKERS = (
    "login",
    "log in",
    "sign in",
    "merchant login",
    "登录",
    "登入",
    "账号密码",
)


class UrlReadError(RuntimeError):
    pass


def extract_public_urls(text: str) -> list[str]:
    urls: list[str] = []
    for matched in URL_PATTERN.finditer(text):
        candidate = html.unescape(matched.group(0)).rstrip(TRAILING_PUNCTUATION)
        if candidate and candidate not in urls:
            urls.append(candidate)
    return urls


def _public_addresses(hostname: str, port: int) -> list[str]:
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UrlReadError("域名无法解析") from exc
    addresses = sorted({str(record[4][0]).split("%", 1)[0] for record in records})
    if not addresses:
        raise UrlReadError("域名没有可用地址")
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise UrlReadError("域名返回了无效地址") from exc
        if not parsed.is_global:
            raise UrlReadError("链接指向内网或非公开地址")
    return addresses


def validate_public_https_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2_048:
        raise UrlReadError("链接格式无效")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise UrlReadError("只读取 HTTPS 链接")
    if parsed.username or parsed.password:
        raise UrlReadError("链接不能包含登录凭证")
    hostname = parsed.hostname
    if not hostname:
        raise UrlReadError("链接缺少域名")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UrlReadError("链接端口无效") from exc
    if port not in {None, 443}:
        raise UrlReadError("链接端口不在允许范围内")
    normalized_host = hostname.rstrip(".").lower()
    if normalized_host == "localhost" or normalized_host.endswith((".localhost", ".local")):
        raise UrlReadError("链接指向本机或局域网")
    try:
        direct_ip = ipaddress.ip_address(normalized_host)
    except ValueError:
        direct_ip = None
    if direct_ip is not None:
        if not direct_ip.is_global:
            raise UrlReadError("链接指向内网或非公开地址")
    else:
        _public_addresses(normalized_host, 443)
    return urllib.parse.urlunsplit(
        ("https", parsed.netloc, parsed.path or "/", parsed.query, "")
    )


class _VisibleHtml(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.hidden_depth = 0
        self.in_title = False
        self.in_anchor = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name in {"script", "style", "noscript", "svg", "canvas", "template"}:
            self.hidden_depth += 1
            return
        if name == "title":
            self.in_title = True
        if name == "a" and self.hidden_depth == 0:
            values = {key.lower(): value for key, value in attrs if value}
            href = values.get("href")
            if href and len(self.links) < 20:
                resolved = urllib.parse.urljoin(self.base_url, href)
                if resolved.startswith("https://"):
                    self.links.append({"url": resolved, "label": ""})
                    self.in_anchor = True

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in {"script", "style", "noscript", "svg", "canvas", "template"}:
            self.hidden_depth = max(0, self.hidden_depth - 1)
        elif name == "title":
            self.in_title = False
        elif name == "a":
            self.in_anchor = False

    def handle_data(self, data: str) -> None:
        if self.hidden_depth:
            return
        normalized = " ".join(data.split())
        if not normalized:
            return
        if self.in_title:
            self.title_parts.append(normalized)
        else:
            self.text_parts.append(normalized)
        if self.in_anchor and self.links:
            current = self.links[-1]["label"]
            self.links[-1]["label"] = " ".join(
                part for part in (current, normalized) if part
            )[:160]


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, validator: Callable[[str], str], maximum_redirects: int) -> None:
        super().__init__()
        self.validator = validator
        self.maximum_redirects = maximum_redirects
        self.redirect_chain: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        resolved = urllib.parse.urljoin(req.full_url, newurl)
        validated = self.validator(resolved)
        if len(self.redirect_chain) >= self.maximum_redirects:
            raise UrlReadError("链接跳转次数过多")
        self.redirect_chain.append(validated)
        return super().redirect_request(req, fp, code, msg, headers, validated)


class PublicUrlReader:
    def __init__(
        self,
        *,
        timeout: int = 10,
        maximum_urls: int = 3,
        maximum_bytes: int = 1_048_576,
        maximum_chars: int = 8_000,
        maximum_redirects: int = 5,
        opener_factory: Callable[..., Any] = urllib.request.build_opener,
    ) -> None:
        self.timeout = timeout
        self.maximum_urls = maximum_urls
        self.maximum_bytes = maximum_bytes
        self.maximum_chars = maximum_chars
        self.maximum_redirects = maximum_redirects
        self.opener_factory = opener_factory

    def read_messages(self, messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        seen: set[str] = set()
        for message_index, message in enumerate(messages):
            if message.get("role") != "user":
                continue
            for url in extract_public_urls(str(message.get("content") or "")):
                if url in seen:
                    continue
                seen.add(url)
                if len(evidence) >= self.maximum_urls:
                    return evidence
                try:
                    item = self.read(url)
                except UrlReadError as exc:
                    item = {
                        "original_url": url,
                        "status": "unavailable",
                        "reason": str(exc),
                    }
                evidence.append({"source_message_index": message_index, **item})
        return evidence

    def read(self, url: str) -> dict[str, Any]:
        original_url = validate_public_https_url(url)
        redirects = _SafeRedirectHandler(validate_public_https_url, self.maximum_redirects)
        opener = self.opener_factory(urllib.request.ProxyHandler({}), redirects)
        request = urllib.request.Request(
            original_url,
            headers={
                "Accept": "text/html,text/plain,application/json;q=0.9",
                "User-Agent": "PiSellMerchantProfileAgent/0.8 (+safe-public-url-reader)",
            },
            method="GET",
        )
        try:
            with opener.open(request, timeout=self.timeout) as response:
                final_url = validate_public_https_url(str(response.geturl()))
                content_type = str(response.headers.get_content_type()).lower()
                if content_type not in SUPPORTED_CONTENT_TYPES:
                    raise UrlReadError(f"暂不读取此文件类型：{content_type or 'unknown'}")
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        declared_length = 0
                    if declared_length > self.maximum_bytes:
                        raise UrlReadError("页面超过读取大小限制")
                raw = response.read(self.maximum_bytes + 1)
                if len(raw) > self.maximum_bytes:
                    raise UrlReadError("页面超过读取大小限制")
                charset = response.headers.get_content_charset() or "utf-8"
        except UrlReadError:
            raise
        except urllib.error.HTTPError as exc:
            raise UrlReadError(f"页面返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UrlReadError("页面访问失败或超时") from exc
        try:
            decoded = raw.decode(charset, errors="replace")
        except LookupError:
            decoded = raw.decode("utf-8", errors="replace")

        title = ""
        links: list[dict[str, str]] = []
        if content_type == "text/html":
            parser = _VisibleHtml(base_url=final_url)
            parser.feed(decoded)
            title = " ".join(parser.title_parts).strip()[:300]
            text = "\n".join(parser.text_parts)
            links = parser.links
        else:
            text = decoded
        normalized_text = re.sub(r"[ \t]+", " ", text)
        normalized_text = re.sub(r"\n{3,}", "\n\n", normalized_text).strip()
        if not normalized_text:
            raise UrlReadError("页面没有可读取的文字内容")
        login_probe = f"{title}\n{normalized_text[:2_000]}".lower()
        status = "login_required" if any(marker in login_probe for marker in LOGIN_MARKERS) else "read"
        return {
            "original_url": original_url,
            "final_url": final_url,
            "redirect_chain": redirects.redirect_chain,
            "status": status,
            "content_type": content_type,
            "title": title,
            "text": normalized_text[: self.maximum_chars],
            "links": links,
            "truncated": len(normalized_text) > self.maximum_chars,
        }
