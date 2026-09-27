"""OpenAI Admin usage and cost report poller (PLAN.md §7.3, source S1).

Endpoints (verified 2026-09-26):

* ``GET /v1/organization/usage/completions`` - ``bucket_width`` 1m/1h/1d, ``group_by``
  project_id/user_id/api_key_id/model/batch/service_tier, ``limit`` up to 31 daily buckets
* ``GET /v1/organization/costs`` - ``bucket_width`` only ``1d``, ``group_by``
  project_id/line_item/api_key_id, ``limit`` up to 180

Both need an Admin API key as a bearer token and paginate with ``has_more``/``next_page``.
``amount.value`` is in currency units (``"usd"``); there is no model grouping for costs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from aiecon.billing.http import fetch_json
from aiecon.spec.common import TimeWindow
from aiecon.spec.provider import NOT_GROUPED, grain_key

BASE_URL = "https://api.openai.com/v1"
USAGE_GROUP_BY = ("project_id", "model")
COST_GROUP_BY = ("project_id", "line_item")
USAGE_FIELDS = (
    "input_tokens",
    "input_cached_tokens",
    "input_cache_write_tokens",
    "input_uncached_tokens",
    "output_tokens",
    "input_text_tokens",
    "output_text_tokens",
    "input_cached_text_tokens",
    "input_audio_tokens",
    "input_cached_audio_tokens",
    "output_audio_tokens",
    "input_image_tokens",
    "input_cached_image_tokens",
    "output_image_tokens",
    "num_model_requests",
)


def _safe_dim(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= 200 else hashlib.sha256(text.encode()).hexdigest()[:32]


@dataclass
class OpenAIReportSource:
    admin_key: str = field(repr=False)
    client: httpx.Client
    project_ids: Sequence[str] | None = None
    base_url: str = BASE_URL
    sleeper: Callable[[float], None] | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.admin_key}",
            "Content-Type": "application/json",
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
        return [("project_ids[]", p) for p in (self.project_ids or [])]

    # ------------------------------------------------------------------ usage
    def fetch_usage(self, window: TimeWindow) -> tuple[list[dict[str, Any]], str, str]:
        params: list[tuple[str, Any]] = [
            ("start_time", window.start_ms // 1000),
            ("end_time", window.end_ms // 1000),
            ("bucket_width", "1d"),
            ("limit", 31),
            *[("group_by[]", g) for g in USAGE_GROUP_BY],
            *self._scope_params(),
        ]
        describe = f"GET {self.base_url}/organization/usage/completions {dict(params)}"
        buckets = self._paginate("/organization/usage/completions", params, describe)
        records = []
        for bucket in buckets:
            start = int(bucket["start_time"]) * 1000
            end = int(bucket["end_time"]) * 1000
            for result in bucket.get("results", []):
                dims = {
                    "project_id": _safe_dim(result.get("project_id")),
                    "model": _safe_dim(result.get("model"))
                    if "model" in USAGE_GROUP_BY
                    else NOT_GROUPED,
                }
                usage = {k: result[k] for k in USAGE_FIELDS if isinstance(result.get(k), int)}
                key = f"{start}_{dims['project_id']}_{dims['model']}"
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
            ("start_time", window.start_ms // 1000),
            ("end_time", window.end_ms // 1000),
            ("bucket_width", "1d"),
            ("limit", 180),
            *[("group_by[]", g) for g in COST_GROUP_BY],
            *self._scope_params(),
        ]
        describe = f"GET {self.base_url}/organization/costs {dict(params)}"
        buckets = self._paginate("/organization/costs", params, describe)
        records = []
        for bucket in buckets:
            start = int(bucket["start_time"]) * 1000
            end = int(bucket["end_time"]) * 1000
            for index, result in enumerate(bucket.get("results", [])):
                amount = result.get("amount") or {}
                value = amount.get("value")
                currency = str(amount.get("currency") or "").upper() or None
                dims = {
                    "project_id": _safe_dim(result.get("project_id")),
                    "line_item": _safe_dim(result.get("line_item")),
                    "api_key_id": _safe_dim(result.get("api_key_id"))
                    if "api_key_id" in COST_GROUP_BY
                    else NOT_GROUPED,
                    "model": NOT_GROUPED,
                }
                usage = {}
                if isinstance(result.get("quantity"), int | float | Decimal):
                    usage["quantity"] = int(result["quantity"])
                if result.get("quantity_unit"):
                    dims["quantity_unit"] = _safe_dim(result["quantity_unit"])
                key = f"{start}_{dims['project_id']}_{dims['line_item']}_{index}"
                records.append(
                    {
                        "record_id": "cost_" + hashlib.sha256(key.encode()).hexdigest()[:24],
                        "window_start_ms": start,
                        "window_end_ms": end,
                        "dimensions_json": dims,
                        "amount_original": None if value is None else str(value),
                        "amount_unit": "usd"
                        if currency == "USD"
                        else (currency or "unknown").lower(),
                        "currency": currency,
                        "usage_json": usage or None,
                    }
                )
        return records, describe, grain_key("1d", COST_GROUP_BY)


def dumps_records(records: list[dict[str, Any]]) -> str:
    return json.dumps({"records": records}, indent=2, sort_keys=True, default=str) + "\n"
