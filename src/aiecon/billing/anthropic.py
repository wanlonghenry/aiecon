"""Anthropic Admin usage and cost report poller (PLAN.md §7.3, source S2).

Endpoints (verified 2026-09-26):

* ``GET /v1/organizations/usage_report/messages`` - ``bucket_width`` 1m/1h/1d, ``group_by[]``
  workspace_id/model/api_key_id/service_tier/context_window/... , ``limit`` up to 31 daily
* ``GET /v1/organizations/cost_report`` - daily only, ``group_by[]`` workspace_id/description,
  ``limit`` up to 31; ``amount`` is a decimal string in cents ("123.45" means $1.2345 here),
  ``currency`` is always "USD"; ``workspace_id: null`` is the default workspace

Both need an Admin API key in ``x-api-key`` with ``anthropic-version: 2023-06-01`` and
paginate with ``has_more``/``next_page``. Data "typically appears within 5 minutes".
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from aiecon.billing.http import fetch_json
from aiecon.spec.common import TimeWindow
from aiecon.spec.provider import NOT_GROUPED, grain_key

BASE_URL = "https://api.anthropic.com/v1/organizations"
ANTHROPIC_VERSION = "2023-06-01"
USAGE_GROUP_BY = ("workspace_id", "model")
COST_GROUP_BY = ("workspace_id", "description")
USAGE_FIELDS = ("uncached_input_tokens", "cache_read_input_tokens", "output_tokens")
COST_DIMENSIONS = (
    "description",
    "cost_type",
    "context_window",
    "model",
    "service_tier",
    "token_type",
    "inference_geo",
)


def iso_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_ms(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def _safe_dim(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= 200 else hashlib.sha256(text.encode()).hexdigest()[:32]


@dataclass
class AnthropicReportSource:
    admin_key: str = field(repr=False)
    client: httpx.Client
    workspace_ids: Sequence[str] | None = None
    base_url: str = BASE_URL
    sleeper: Callable[[float], None] | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.admin_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "User-Agent": "aiecon/0.1.0 (+https://github.com/wanlonghenry/aiecon)",
        }

    def _paginate(self, path: str, params: list[tuple[str, Any]], describe: str) -> list[dict]:
        buckets: list[dict] = []
        page: str | None = None
        while True:
            query = list(params)
            if page:
                query.append(("page", page))
            kwargs: dict[str, Any] = {}
            if self.sleeper is not None:
                kwargs["sleeper"] = self.sleeper
            data = fetch_json(
                self.client,
                f"{self.base_url}{path}",
                params=query,
                headers=self._headers(),
                describe=describe,
                **kwargs,
            )
            buckets.extend(b for b in data.get("data", []) if isinstance(b, dict))
            if not data.get("has_more") or not data.get("next_page"):
                return buckets
            page = str(data["next_page"])

    def _scope_params(self) -> list[tuple[str, Any]]:
        return [("workspace_ids[]", w) for w in (self.workspace_ids or [])]

    # ------------------------------------------------------------------ usage
    def fetch_usage(self, window: TimeWindow) -> tuple[list[dict[str, Any]], str, str]:
        params: list[tuple[str, Any]] = [
            ("starting_at", iso_ms(window.start_ms)),
            ("ending_at", iso_ms(window.end_ms)),
            ("bucket_width", "1d"),
            ("limit", 31),
            *[("group_by[]", g) for g in USAGE_GROUP_BY],
            *self._scope_params(),
        ]
        describe = f"GET {self.base_url}/usage_report/messages {dict(params)}"
        buckets = self._paginate("/usage_report/messages", params, describe)
        records = []
        for bucket in buckets:
            start = parse_iso_ms(bucket["starting_at"])
            end = parse_iso_ms(bucket["ending_at"])
            for index, result in enumerate(bucket.get("results", [])):
                dims = {
                    "workspace_id": _safe_dim(
                        result.get("workspace_id")
                    ),  # None = default workspace
                    "model": _safe_dim(result.get("model"))
                    if "model" in USAGE_GROUP_BY
                    else NOT_GROUPED,
                }
                usage: dict[str, Any] = {
                    k: result[k] for k in USAGE_FIELDS if isinstance(result.get(k), int)
                }
                creation = result.get("cache_creation")
                if isinstance(creation, dict):
                    usage["cache_creation"] = {
                        k: v
                        for k, v in creation.items()
                        if k in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
                        and isinstance(v, int)
                    }
                tools = result.get("server_tool_use")
                if isinstance(tools, dict):
                    usage["server_tool_use"] = {
                        k: v for k, v in tools.items() if isinstance(v, int)
                    }
                key = f"{start}_{dims['workspace_id']}_{dims['model']}_{index}"
                records.append(
                    {
                        "record_id": "usage_" + hashlib.sha256(key.encode()).hexdigest()[:24],
                        "window_start_ms": start,
                        "window_end_ms": end,
                        "dimensions_json": dims,
                        "amount_original": None,
                        "amount_unit": None,
                        "currency": None,
                        "usage_json": usage,
                    }
                )
        return records, describe, grain_key("1d", USAGE_GROUP_BY)

    # ------------------------------------------------------------------- cost
    def fetch_costs(self, window: TimeWindow) -> tuple[list[dict[str, Any]], str, str]:
        params: list[tuple[str, Any]] = [
            ("starting_at", iso_ms(window.start_ms)),
            ("ending_at", iso_ms(window.end_ms)),
            ("bucket_width", "1d"),
            ("limit", 31),
            *[("group_by[]", g) for g in COST_GROUP_BY],
        ]
        describe = f"GET {self.base_url}/cost_report {dict(params)}"
        buckets = self._paginate("/cost_report", params, describe)
        records = []
        for bucket in buckets:
            start = parse_iso_ms(bucket["starting_at"])
            end = parse_iso_ms(bucket["ending_at"])
            for index, result in enumerate(bucket.get("results", [])):
                dims: dict[str, str | None] = {
                    "workspace_id": _safe_dim(result.get("workspace_id"))
                }
                for name in COST_DIMENSIONS:
                    dims[name] = _safe_dim(result.get(name))
                currency = str(result.get("currency") or "").upper() or None
                amount = result.get("amount")
                key = f"{start}_{dims['workspace_id']}_{dims['description']}_{index}"
                records.append(
                    {
                        "record_id": "cost_" + hashlib.sha256(key.encode()).hexdigest()[:24],
                        "window_start_ms": start,
                        "window_end_ms": end,
                        "dimensions_json": dims,
                        "amount_original": None if amount is None else str(amount),
                        "amount_unit": "cents",
                        "currency": currency,
                        "usage_json": None,
                    }
                )
        return records, describe, grain_key("1d", COST_GROUP_BY)
