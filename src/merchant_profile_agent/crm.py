from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .settings import Settings
from .validation import ProfileValidationError


class CrmResolverError(RuntimeError):
    """A CRM lookup failed without exposing credentials or command output."""


class CrmResolverUnavailable(CrmResolverError):
    pass


class CrmResolutionRequired(CrmResolverError):
    def __init__(self, resolution: Mapping[str, Any]) -> None:
        super().__init__("a unique CRM customer is required before saving")
        self.resolution = dict(resolution)


class CrmResolver(Protocol):
    name: str

    def resolve(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


class DisabledCrmResolver:
    name = "disabled"

    def resolve(self, request: Mapping[str, Any]) -> dict[str, Any]:
        merchant_name, lookup_hint = _validated_request(request)
        return {
            "status": "disabled",
            "query": {"merchant_name": merchant_name, "lookup_hint": lookup_hint},
            "candidates": [],
            "read_only": True,
        }


@dataclass(frozen=True)
class CrmCandidate:
    work_item_id: str
    name: str
    status: str | None = None
    url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": self.work_item_id,
            "name": self.name,
            "status": self.status,
            "url": self.url,
        }


def _validated_request(request: Mapping[str, Any]) -> tuple[str, str | None]:
    extra = set(request) - {"merchant_name", "merchant_id", "lookup_hint"}
    if extra:
        raise ProfileValidationError(
            "request has undeclared fields: " + ", ".join(sorted(extra))
        )
    name = request.get("merchant_name")
    hint = request.get("lookup_hint", request.get("merchant_id"))
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
        raise ProfileValidationError("merchant_name is required")
    if hint is not None and (
        not isinstance(hint, str) or not hint.strip() or len(hint.strip()) > 200
    ):
        raise ProfileValidationError("lookup_hint is invalid")
    return name.strip(), hint.strip() if isinstance(hint, str) else None


def _mql_string(value: str) -> str:
    return value.replace("'", "''")


def _mql_like(value: str) -> str:
    return _mql_string(value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))


def _scalar(value: Any) -> str | None:
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()
    if isinstance(value, Mapping):
        for key in ("value", "label", "name", "text", "id"):
            candidate = _scalar(value.get(key))
            if candidate:
                return candidate
    if isinstance(value, list) and len(value) == 1:
        return _scalar(value[0])
    return None


def _candidate_from_mapping(
    value: Mapping[str, Any], *, url_template: str
) -> CrmCandidate | None:
    identifier = None
    name = None
    status = None
    for key in ("work_item_id", "workItemId", "工作项ID", "工作项id", "id"):
        identifier = _scalar(value.get(key))
        if identifier:
            break
    for key in ("name", "名称", "title"):
        name = _scalar(value.get(key))
        if name:
            break
    for key in ("work_item_status", "workItemStatus", "状态", "status"):
        status = _scalar(value.get(key))
        if status:
            break
    if not identifier or not name:
        return None
    url = url_template.replace("{work_item_id}", identifier) if url_template else None
    return CrmCandidate(identifier, name, status, url)


def _extract_candidates(value: Any, *, url_template: str) -> list[CrmCandidate]:
    found: dict[str, CrmCandidate] = {}

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            candidate = _candidate_from_mapping(node, url_template=url_template)
            if candidate:
                found.setdefault(candidate.work_item_id, candidate)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return list(found.values())


class MeegleCrmResolver:
    name = "meegle"

    def __init__(
        self,
        *,
        executable: str,
        project_key: str,
        work_item_type: str,
        profile: str = "",
        url_template: str = "",
        timeout: int = 20,
    ) -> None:
        self.executable = executable
        self.project_key = project_key
        self.work_item_type = work_item_type
        self.profile = profile
        self.url_template = url_template
        self.timeout = timeout

    def _query(self, mql: str) -> list[CrmCandidate]:
        command = [self.executable]
        if self.profile:
            command.extend(["--profile", self.profile])
        command.extend(
            [
                "workitem",
                "query",
                "--project-key",
                self.project_key,
                "--mql",
                mql,
                "--format",
                "json",
            ]
        )
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CrmResolverUnavailable("CRM lookup service is unavailable") from exc
        if completed.returncode != 0:
            raise CrmResolverUnavailable("CRM lookup service is unavailable")
        try:
            payload = json.loads(completed.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CrmResolverUnavailable("CRM lookup returned an invalid response") from exc
        return _extract_candidates(payload, url_template=self.url_template)

    def _mql(self, where: str) -> str:
        return (
            "SELECT `work_item_id`, `name` "
            f"FROM `{self.project_key}`.`{self.work_item_type}` "
            f"WHERE {where} LIMIT 10"
        )

    def resolve(self, request: Mapping[str, Any]) -> dict[str, Any]:
        merchant_name, lookup_hint = _validated_request(request)
        if lookup_hint and re.fullmatch(r"\d{6,30}", lookup_hint):
            by_id = self._query(self._mql(f"`work_item_id` = {lookup_hint}"))
            if len(by_id) == 1:
                normalized_name = re.sub(r"\s+", "", merchant_name).casefold()
                normalized_candidate = re.sub(r"\s+", "", by_id[0].name).casefold()
                if normalized_name in normalized_candidate or normalized_candidate in normalized_name:
                    return {
                        "status": "unique",
                        "query": {
                            "merchant_name": merchant_name,
                            "lookup_hint": lookup_hint,
                        },
                        "match_method": "work_item_id",
                        "candidates": [by_id[0].as_dict()],
                        "read_only": True,
                    }
        exact = self._query(self._mql(f"`name` = '{_mql_string(merchant_name)}'"))
        candidates = exact
        match_method = "exact_name"
        if lookup_hint and lookup_hint.casefold() != merchant_name.casefold() and len(exact) != 1:
            conditions = (
                f"`name` LIKE '%{_mql_like(merchant_name)}%' AND "
                f"`name` LIKE '%{_mql_like(lookup_hint)}%'"
            )
            candidates = self._query(self._mql(f"({conditions})"))
            match_method = "name_and_hint"
        if not candidates:
            candidates = self._query(
                self._mql(f"`name` LIKE '%{_mql_like(merchant_name)}%'")
            )
            match_method = "fuzzy_name"
        status = "unique" if len(candidates) == 1 else "ambiguous" if candidates else "not_found"
        return {
            "status": status,
            "query": {"merchant_name": merchant_name, "lookup_hint": lookup_hint},
            "match_method": match_method,
            "candidates": [candidate.as_dict() for candidate in candidates[:10]],
            "read_only": True,
        }


class TicketBridgeCrmResolver:
    name = "ticket_bridge"

    def __init__(self, *, base_url: str, token: str, timeout: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def resolve(self, request: Mapping[str, Any]) -> dict[str, Any]:
        merchant_name, lookup_hint = _validated_request(request)
        body = {"merchant_name": merchant_name}
        if lookup_hint:
            body["lookup_hint"] = lookup_hint
        http_request = urllib.request.Request(
            f"{self.base_url}/internal/customer-crm/resolve",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise CrmResolverUnavailable(
                    "ticket Agent CRM identity requires authorization"
                ) from exc
            raise CrmResolverUnavailable("CRM lookup service is unavailable") from exc
        except (OSError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CrmResolverUnavailable("CRM lookup service is unavailable") from exc
        if not isinstance(payload, Mapping) or payload.get("ok") is not True:
            raise CrmResolverUnavailable("CRM lookup returned an invalid response")
        crm = payload.get("crm")
        if not isinstance(crm, Mapping):
            raise CrmResolverUnavailable("CRM lookup returned an invalid response")
        status = crm.get("status")
        candidates = crm.get("candidates")
        if status not in {"unique", "ambiguous", "not_found"} or not isinstance(
            candidates, list
        ):
            raise CrmResolverUnavailable("CRM lookup returned an invalid response")
        return dict(crm)


def create_crm_resolver(settings: Settings) -> CrmResolver:
    if settings.crm_resolver == "disabled":
        return DisabledCrmResolver()
    if settings.crm_resolver == "ticket_bridge":
        return TicketBridgeCrmResolver(
            base_url=settings.ticket_bridge_url,
            token=settings.ticket_bridge_token,
            timeout=settings.crm_timeout,
        )
    return MeegleCrmResolver(
        executable=settings.meegle_bin,
        project_key=settings.meegle_project_key,
        work_item_type=settings.meegle_crm_work_item_type,
        profile=settings.meegle_profile,
        url_template=settings.meegle_crm_url_template,
        timeout=settings.crm_timeout,
    )
