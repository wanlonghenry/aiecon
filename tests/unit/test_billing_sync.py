"""T3.2 / V13 / V14 / V16: pollers with pagination, retries, auth failure and staging."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from aiecon import __version__
from aiecon.billing.http import ProviderReportError
from aiecon.billing.sync import default_window, parse_utc_boundary, run_sync
from aiecon.spec import DataKind, Provider, TimeWindow
from aiecon.storage import Storage, Workspace

D1 = 1_790_380_800_000
DAY = 86_400_000
WINDOW = TimeWindow(start_ms=D1, end_ms=D1 + 2 * DAY)
CANARY = "sk-admin-CANARY-KEY-000"


def openai_usage_page(page: int) -> dict:
    start = D1 // 1000 + (page - 1) * 86_400
    return {
        "object": "page",
        "data": [
            {
                "object": "bucket",
                "start_time": start,
                "end_time": start + 86_400,
                "results": [
                    {
                        "object": "organization.usage.completions.result",
                        "input_tokens": 1000,
                        "input_cached_tokens": 400,
                        "input_cache_write_tokens": 100,
                        "input_uncached_tokens": 500,
                        "output_tokens": 500,
                        "num_model_requests": 5,
                        "project_id": "proj_a",
                        "model": "gpt-5-nano",
                    }
                ],
            }
        ],
        "has_more": page == 1,
        "next_page": "page_2" if page == 1 else None,
    }


def openai_cost_page() -> dict:
    return {
        "object": "page",
        "data": [
            {
                "object": "bucket",
                "start_time": D1 // 1000,
                "end_time": D1 // 1000 + 86_400,
                "results": [
                    {
                        "object": "organization.costs.result",
                        "amount": {"value": 0.06, "currency": "usd"},
                        "line_item": "gpt-5-nano, input_tokens",
                        "project_id": "proj_a",
                        "quantity": 10000,
                        "quantity_unit": "tokens",
                    }
                ],
            }
        ],
        "has_more": False,
        "next_page": None,
    }


def anthropic_usage_page() -> dict:
    return {
        "data": [
            {
                "starting_at": "2026-09-26T00:00:00Z",
                "ending_at": "2026-09-27T00:00:00Z",
                "results": [
                    {
                        "uncached_input_tokens": 1500,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 100,
                            "ephemeral_1h_input_tokens": 0,
                        },
                        "cache_read_input_tokens": 200,
                        "output_tokens": 500,
                        "server_tool_use": {"web_search_requests": 0},
                        "workspace_id": None,
                        "model": "claude-haiku-4-5-20251001",
                    }
                ],
            }
        ],
        "has_more": False,
        "next_page": None,
    }


def anthropic_cost_page() -> dict:
    return {
        "data": [
            {
                "starting_at": "2026-09-26T00:00:00Z",
                "ending_at": "2026-09-27T00:00:00Z",
                "results": [
                    {
                        "amount": "123.45",
                        "currency": "USD",
                        "description": "Claude Haiku 4.5 Usage - Input Tokens",
                        "cost_type": "tokens",
                        "context_window": "0-200k",
                        "model": "claude-haiku-4-5-20251001",
                        "service_tier": "standard",
                        "token_type": "uncached_input_tokens",
                        "workspace_id": None,
                    }
                ],
            }
        ],
        "has_more": False,
        "next_page": None,
    }


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.fail_second_page = False
        self.rate_limit_first = False
        self.unauthorized = False

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.unauthorized:
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        path = request.url.path
        page = request.url.params.get("page")
        if "usage/completions" in path:
            if (
                self.rate_limit_first
                and len([r for r in self.requests if "usage/completions" in r.url.path]) == 1
            ):
                return httpx.Response(429, headers={"retry-after": "0.5"}, json={})
            if page == "page_2":
                if self.fail_second_page:
                    return httpx.Response(503, json={})
                return httpx.Response(200, json=openai_usage_page(2))
            return httpx.Response(200, json=openai_usage_page(1))
        if path.endswith("/organization/costs"):
            return httpx.Response(200, json=openai_cost_page())
        if "usage_report/messages" in path:
            return httpx.Response(200, json=anthropic_usage_page())
        if path.endswith("/cost_report"):
            return httpx.Response(200, json=anthropic_cost_page())
        return httpx.Response(404, json={})


@pytest.fixture
def live_ws(tmp_path: Path) -> Workspace:
    ws = Workspace(tmp_path / "live")
    ws.init(data_kind=DataKind.live, now_ms=1, aiecon_version=__version__)
    return ws


def sync(ws: Workspace, provider: Provider, recorder: Recorder, *, now_ms: int = 10):
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    with Storage.open(ws.db_path) as storage:
        result = run_sync(
            storage,
            ws,
            provider=provider,
            window=WINDOW,
            admin_key=CANARY,
            scope_id="proj_a" if provider is Provider.openai else "default_workspace",
            now_ms=now_ms,
            scope_filter=["proj_a"] if provider is Provider.openai else None,
            client=client,
            sleeper=recorder.sleep,
        )
        snapshots = storage.list_snapshots()
        records = storage.list_provider_records()
    return result, snapshots, records


def test_openai_sync_paginates_and_stages_before_activating(live_ws: Workspace) -> None:
    recorder = Recorder()
    result, snapshots, records = sync(live_ws, Provider.openai, recorder)
    usage_requests = [r for r in recorder.requests if "usage/completions" in r.url.path]
    assert len(usage_requests) == 2 and usage_requests[1].url.params["page"] == "page_2"
    assert usage_requests[0].url.params.get_list("group_by[]") == ["project_id", "model"]
    assert usage_requests[0].url.params.get_list("project_ids[]") == ["proj_a"]
    assert usage_requests[0].headers["authorization"] == f"Bearer {CANARY}"
    assert [i.record_count for i in result.imports] == [2, 1]
    assert {s.record_kind.value for s in snapshots} == {"provider_usage", "provider_cost"}
    usage = [r for r in records if r.record_kind.value == "provider_usage"]
    assert usage[0].usage["input_total_tokens"] == 1000 and usage[0].usage["requests"] == 5
    assert usage[0].dimensions == {"project_id": "proj_a", "model": "gpt-5-nano"}
    cost = next(r for r in records if r.record_kind.value == "provider_cost")
    assert cost.amount_usd == Decimal("0.06") and cost.amount_unit == "usd"
    assert cost.dimensions["model"] == "not_grouped"  # OpenAI costs have no model grouping (V14)
    assert cost.dimensions["line_item"] == "gpt-5-nano, input_tokens"
    # staged files exist and never contain the admin key
    staged = list((live_ws.provider_dir / "sync").rglob("*.json"))
    assert staged and all(CANARY not in p.read_text("utf-8") for p in staged)
    manifest = next(p for p in staged if p.name == "manifest.json")
    assert json.loads(manifest.read_text("utf-8"))["source_type"] == "api_poller"


def test_anthropic_cents_and_default_workspace_null(live_ws: Workspace) -> None:
    recorder = Recorder()
    result, snapshots, records = sync(live_ws, Provider.anthropic, recorder)
    request = recorder.requests[0]
    assert (
        request.headers["x-api-key"] == CANARY
        and request.headers["anthropic-version"] == "2023-06-01"
    )
    assert request.url.params["starting_at"] == "2026-09-26T00:00:00Z"
    cost = next(r for r in records if r.record_kind.value == "provider_cost")
    assert cost.amount_original == Decimal("123.45") and cost.amount_unit == "cents"
    assert cost.amount_usd == Decimal("1.2345")  # V13
    assert cost.dimensions["workspace_id"] is None  # default workspace stays null
    assert cost.dimensions["token_type"] == "uncached_input_tokens"
    usage = next(r for r in records if r.record_kind.value == "provider_usage")
    assert usage.usage["input_total_tokens"] == 1800
    assert usage.usage["input_cache_write_5m_tokens"] == 100
    assert "requests" not in usage.usage


def test_failed_second_page_keeps_previous_snapshot_active(live_ws: Workspace) -> None:
    good = Recorder()
    _, before, _ = sync(live_ws, Provider.openai, good, now_ms=10)
    assert len(before) == 2
    bad = Recorder()
    bad.fail_second_page = True
    client = httpx.Client(transport=httpx.MockTransport(bad.handler))
    with Storage.open(live_ws.db_path) as storage:
        with pytest.raises(ProviderReportError, match="503"):
            run_sync(
                storage,
                live_ws,
                provider=Provider.openai,
                window=WINDOW,
                admin_key=CANARY,
                scope_id="proj_a",
                now_ms=20,
                client=client,
                sleeper=bad.sleep,
            )
        after = storage.list_snapshots()
    assert {s.snapshot_id for s in after} == {s.snapshot_id for s in before}
    assert len(bad.sleeps) == 3  # bounded retries with backoff
    assert not any("20" in p.name for p in (live_ws.provider_dir / "sync").iterdir() if p.is_dir())


def test_rate_limit_honours_retry_after_and_auth_fails_fast(live_ws: Workspace) -> None:
    limited = Recorder()
    limited.rate_limit_first = True
    sync(live_ws, Provider.openai, limited)
    assert limited.sleeps[:1] == [0.5]
    denied = Recorder()
    denied.unauthorized = True
    client = httpx.Client(transport=httpx.MockTransport(denied.handler))
    with Storage.open(live_ws.db_path) as storage:
        with pytest.raises(ProviderReportError, match="authentication"):
            run_sync(
                storage,
                live_ws,
                provider=Provider.anthropic,
                window=WINDOW,
                admin_key=CANARY,
                scope_id="ws",
                now_ms=30,
                client=client,
                sleeper=denied.sleep,
            )
    assert len(denied.requests) == 1 and denied.sleeps == []


def test_window_helpers() -> None:
    assert parse_utc_boundary("2026-09-27") == D1 + DAY
    assert parse_utc_boundary("2026-09-27T00:00:00Z") == D1 + DAY
    window = default_window(D1 + DAY + 5000)
    assert window.start_ms == D1 + DAY - 3 * DAY and window.end_ms == D1 + 2 * DAY
