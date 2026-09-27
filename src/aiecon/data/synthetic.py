"""Deterministic synthetic ``support_agent`` dataset (PLAN.md section 11.1).

The generator is the *specification* of the demo data. It writes:

* ``events.jsonl`` - 313 calls (300 base + 8 retries + 5 fallbacks) as 626 start/finish
  events plus 100 outcome events
* ``provider/<provider>_<kind>.json`` + ``.manifest.json`` - synthetic provider usage and
  cost snapshots in the file-import format of PLAN.md section 7.4
* ``expected_metrics.json`` - counts, token totals, costs and scenario values computed here
  with plain Decimal arithmetic, independently of the estimator, reconciler and detectors
* ``synthetic-catalog.json`` - artificial prices for the ``fixture-*`` models

Everything is derived from a fixed seed; regenerating must reproduce the committed files
byte for byte (tests/unit/test_synthetic_fixtures.py).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

DATASET_ID = "demo-support-v1"
SEED = 20260926
SOURCE_VERSION = "synthetic-generator-0.1"
WORKFLOW_ID = "support_agent"
SCOPE_OPENAI = "demo_scope_openai"
SCOPE_ANTHROPIC = "demo_scope_anthropic"
CATALOG_VERSION = "synthetic-0.1"
FINGERPRINT_KEY_ID = "fpk_synthetic0001"

MS = 1000
MINUTE = 60 * MS
DAY = 86_400 * MS
DAY1_MS = 1_790_380_800_000  # 2026-09-26T00:00:00Z
DAY2_MS = DAY1_MS + DAY  # 2026-09-27T00:00:00Z
FIRST_RUN_MS = DAY1_MS + 23 * 3600 * MS + 5 * MINUTE + 30 * MS  # 2026-09-26T23:05:30Z

# Artificial per-million-token rates for fixture-* models. They deliberately reuse the
# PLAN.md section 13.1 sample so the arithmetic can be checked by hand. Not real prices.
RATES: dict[str, dict[str, Decimal]] = {
    "fixture-openai-v1": {
        "input_uncached": Decimal("2"),
        "input_cache_read": Decimal("0.5"),
        "output": Decimal("8"),
    },
    "fixture-anthropic-v1": {
        "input_uncached": Decimal("2"),
        "input_cache_read": Decimal("0.5"),
        "input_cache_write_5m": Decimal("2.5"),
        "input_cache_write_1h": Decimal("4"),
        "output": Decimal("8"),
    },
}
MILLION = Decimal(1_000_000)
TWELVE_PLACES = Decimal("1.000000000000")

FAILED_RUNS = frozenset(range(10, 101, 10))
PAID_RETRY_RUNS = frozenset({3, 31, 58, 86, 100})
ERROR_RETRY_RUNS = frozenset({17, 45, 72})
REDUNDANT_FALLBACK_RUNS = frozenset({8, 64, 100})
USED_FALLBACK_RUNS = frozenset({41, 77})
BOUNDARY_RUN = 55  # its reasoning call straddles the UTC midnight boundary

# Anthropic provider fixtures deviate from the local truth on purpose (section 11.1 billing
# scenarios): day 1 carries one uncaptured call (explainable), day 2 an unexplained residue.
PHANTOM_CALL_TOKENS = {"input_uncached": 20000, "output": 4000}  # $0.072 at fixture rates
UNEXPLAINED_RESIDUE_USD = Decimal("0.0137")


@dataclass
class PrefixGroup:
    key: str
    fingerprint: str
    prefix_tokens: int
    cache_policy: str
    runs: range


PREFIX_GROUPS: tuple[PrefixGroup, ...] = (
    PrefixGroup("group_a_uncached_repeat", "a" * 64, 3000, "none", range(1, 41)),
    PrefixGroup("group_b_already_cached", "b" * 64, 2500, "ephemeral_5m", range(41, 71)),
    PrefixGroup("group_c_cross_ttl", "c" * 64, 3000, "none", range(71, 76)),
    PrefixGroup("group_d_single_call", "d" * 64, 1500, "none", range(76, 77)),
)


@dataclass
class Call:
    call_id: str
    run: int
    node_id: str
    node_run_id: str
    attempt_index: int
    provider: str
    scope_id: str
    model_requested: str
    model_resolved: str
    usage_format: str
    started_at_ms: int
    ended_at_ms: int
    status: str = "success"
    error_class: str | None = None
    usage: dict[str, Any] | None = None
    tokens: dict[str, int] = field(default_factory=dict)  # normalized truth
    retry_of_call_id: str | None = None
    fallback_of_call_id: str | None = None
    prefix_fingerprint: str | None = None
    prefix_tokens: int | None = None
    cache_policy: str | None = None
    disposition: str = "used"
    disposition_reason: str = "used_in_flow"
    removal_safe: bool | None = None

    def cost_usd(self) -> Decimal | None:
        if self.usage is None:
            return None
        rates = RATES[self.model_resolved]
        total = Decimal(0)
        for resource, quantity in self.tokens.items():
            if quantity:
                total += Decimal(quantity) * rates[resource] / MILLION
        return total

    def day_ms(self) -> int:
        return DAY1_MS if self.started_at_ms < DAY2_MS else DAY2_MS


def _run_schedule() -> dict[int, int]:
    """Run start times: 60 s apart, except group C which is spaced 8 minutes apart."""

    schedule: dict[int, int] = {}
    t = FIRST_RUN_MS
    for run in range(1, 101):
        schedule[run] = t
        t += 8 * MINUTE if 71 <= run <= 75 else MINUTE
    return schedule


def _prefix_group(run: int) -> PrefixGroup | None:
    for group in PREFIX_GROUPS:
        if run in group.runs:
            return group
    return None


def _openai_usage(uncached: int, cached: int, output: int) -> tuple[dict[str, Any], dict[str, int]]:
    usage = {
        "input_tokens": uncached + cached,
        "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": 0},
        "output_tokens": output,
        "output_tokens_details": {"reasoning_tokens": 0},
    }
    return usage, {"input_uncached": uncached, "input_cache_read": cached, "output": output}


def _anthropic_usage(
    uncached: int, read: int, write_5m: int, output: int
) -> tuple[dict[str, Any], dict[str, int]]:
    usage = {
        "input_tokens": uncached,
        "cache_creation_input_tokens": write_5m,
        "cache_read_input_tokens": read,
        "cache_creation": {"ephemeral_5m_input_tokens": write_5m, "ephemeral_1h_input_tokens": 0},
        "output_tokens": output,
    }
    return usage, {
        "input_uncached": uncached,
        "input_cache_read": read,
        "input_cache_write_5m": write_5m,
        "output": output,
    }


def _anthropic_call(**kwargs: Any) -> Call:
    return Call(
        provider="anthropic",
        scope_id=SCOPE_ANTHROPIC,
        model_requested="fixture-anthropic",
        model_resolved="fixture-anthropic-v1",
        usage_format="anthropic_messages",
        **kwargs,
    )


def _openai_call(**kwargs: Any) -> Call:
    return Call(
        provider="openai",
        scope_id=SCOPE_OPENAI,
        model_requested="fixture-openai",
        model_resolved="fixture-openai-v1",
        usage_format="openai_responses",
        **kwargs,
    )


def build_calls(rng: random.Random) -> list[Call]:
    schedule = _run_schedule()
    calls: list[Call] = []
    for run in range(1, 101):
        rid = f"run_{run:03d}"
        t = schedule[run]

        # classification (OpenAI, provider-side automatic caching of an 800-token preamble)
        usage, tokens = _openai_usage(400, 800, rng.randint(60, 120))
        calls.append(
            _openai_call(
                call_id=f"call_{run:03d}_cls_1",
                run=run,
                node_id="classification",
                node_run_id=f"{rid}_classification",
                attempt_index=1,
                started_at_ms=t,
                ended_at_ms=t + 900 + rng.randint(0, 300),
                usage=usage,
                tokens=tokens,
            )
        )

        # reasoning (Anthropic, prefix groups)
        group = _prefix_group(run)
        variable = rng.randint(400, 900)
        output = rng.randint(300, 600)
        if group is None:
            intended = (3400 + variable, 0, 0)
        elif group.cache_policy == "ephemeral_5m":
            first = run == group.runs.start
            intended = (
                variable,
                0 if first else group.prefix_tokens,
                group.prefix_tokens if first else 0,
            )
        else:
            intended = (group.prefix_tokens + variable, 0, 0)
        usage, tokens = _anthropic_usage(*intended, output)
        r_start = t + 2 * MS
        r_end = r_start + 2500 + rng.randint(0, 1500)
        if run == BOUNDARY_RUN:
            r_start, r_end = DAY2_MS - 3 * MS, DAY2_MS + 2 * MS
        reasoning_kwargs: dict[str, Any] = dict(
            run=run,
            node_id="reasoning",
            node_run_id=f"{rid}_reasoning",
            prefix_fingerprint=group.fingerprint if group else None,
            prefix_tokens=group.prefix_tokens if group else None,
            cache_policy=group.cache_policy if group else None,
        )
        if run in PAID_RETRY_RUNS or run in ERROR_RETRY_RUNS:
            first_id = f"call_{run:03d}_rsn_1"
            if run in PAID_RETRY_RUNS:
                first = _anthropic_call(
                    call_id=first_id,
                    attempt_index=1,
                    started_at_ms=r_start,
                    ended_at_ms=r_end,
                    usage=usage,
                    tokens=tokens,
                    disposition="discarded",
                    disposition_reason="superseded_by_retry",
                    removal_safe=True,
                    **reasoning_kwargs,
                )
            else:
                first = _anthropic_call(
                    call_id=first_id,
                    attempt_index=1,
                    started_at_ms=r_start,
                    ended_at_ms=r_start + 30 * MS,
                    status="timeout",
                    error_class="timeout",
                    usage=None,
                    tokens={},
                    disposition="discarded",
                    disposition_reason="attempt_failed",
                    **reasoning_kwargs,
                )
            calls.append(first)
            # the retry sends the same prefix and variable part again, with a new answer
            usage2, tokens2 = _anthropic_usage(*intended, rng.randint(300, 600))
            retry_start = first.ended_at_ms + 3 * MS
            calls.append(
                _anthropic_call(
                    call_id=f"call_{run:03d}_rsn_2",
                    attempt_index=2,
                    started_at_ms=retry_start,
                    ended_at_ms=retry_start + 2500 + rng.randint(0, 1500),
                    usage=usage2,
                    tokens=tokens2,
                    retry_of_call_id=first_id,
                    **reasoning_kwargs,
                )
            )
        else:
            calls.append(
                _anthropic_call(
                    call_id=f"call_{run:03d}_rsn_1",
                    attempt_index=1,
                    started_at_ms=r_start,
                    ended_at_ms=r_end,
                    usage=usage,
                    tokens=tokens,
                    **reasoning_kwargs,
                )
            )

        # finalize (OpenAI, optional fallback to Anthropic)
        f_start = calls[-1].ended_at_ms + 1 * MS
        f_node = f"{rid}_finalize"
        usage, tokens = _openai_usage(rng.randint(900, 1400), 0, rng.randint(150, 300))
        primary_id = f"call_{run:03d}_fin_1"
        if run in USED_FALLBACK_RUNS:
            primary = _openai_call(
                call_id=primary_id,
                run=run,
                node_id="finalize",
                node_run_id=f_node,
                attempt_index=1,
                started_at_ms=f_start,
                ended_at_ms=f_start + 800,
                status="error",
                error_class="server_error",
                usage=None,
                tokens={},
                disposition="discarded",
                disposition_reason="attempt_failed",
            )
            calls.append(primary)
            fb_usage, fb_tokens = _anthropic_usage(
                tokens["input_uncached"], 0, 0, rng.randint(150, 300)
            )
            calls.append(
                _anthropic_call(
                    call_id=f"call_{run:03d}_fin_2",
                    run=run,
                    node_id="finalize",
                    node_run_id=f_node,
                    attempt_index=2,
                    started_at_ms=primary.ended_at_ms + MS,
                    ended_at_ms=primary.ended_at_ms + MS + 1800,
                    usage=fb_usage,
                    tokens=fb_tokens,
                    fallback_of_call_id=primary_id,
                    disposition_reason="used_as_final",
                )
            )
        else:
            primary = _openai_call(
                call_id=primary_id,
                run=run,
                node_id="finalize",
                node_run_id=f_node,
                attempt_index=1,
                started_at_ms=f_start,
                ended_at_ms=f_start + 1200 + rng.randint(0, 600),
                usage=usage,
                tokens=tokens,
                disposition_reason="used_as_final",
            )
            calls.append(primary)
            if run in REDUNDANT_FALLBACK_RUNS:
                fb_usage, fb_tokens = _anthropic_usage(
                    tokens["input_uncached"], 0, 0, rng.randint(150, 300)
                )
                calls.append(
                    _anthropic_call(
                        call_id=f"call_{run:03d}_fin_2",
                        run=run,
                        node_id="finalize",
                        node_run_id=f_node,
                        attempt_index=2,
                        started_at_ms=primary.started_at_ms + 400,
                        ended_at_ms=primary.started_at_ms + 400 + 1700,
                        usage=fb_usage,
                        tokens=fb_tokens,
                        fallback_of_call_id=primary_id,
                        disposition="discarded",
                        disposition_reason="redundant_fallback",
                        removal_safe=True,
                    )
                )
    return calls


# ------------------------------------------------------------------------ events
def _context(call: Call) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "workflow_id": WORKFLOW_ID,
        "workflow_run_id": f"run_{call.run:03d}",
        "node_id": call.node_id,
        "node_run_id": call.node_run_id,
        "call_id": call.call_id,
        "scope_id": call.scope_id,
        "attempt_index": call.attempt_index,
    }
    if call.retry_of_call_id:
        ctx["retry_of_call_id"] = call.retry_of_call_id
    if call.fallback_of_call_id:
        ctx["fallback_of_call_id"] = call.fallback_of_call_id
    return ctx


def _envelope(
    event_id: str, event_type: str, occurred_at_ms: int, context: dict, payload: dict
) -> dict:
    return {
        "schema_version": "0.1",
        "event_id": event_id,
        "event_type": event_type,
        "revision": 1,
        "dataset_id": DATASET_ID,
        "data_kind": "synthetic",
        "source_type": "synthetic_generator",
        "source_version": SOURCE_VERSION,
        "observed_at_ms": occurred_at_ms + 50,
        "occurred_at_ms": occurred_at_ms,
        "context": context,
        "payload": payload,
    }


def _prefix_fields(call: Call) -> dict[str, Any]:
    if not call.prefix_fingerprint:
        return {}
    return {
        "cache_policy": call.cache_policy,
        "prefix_fingerprint": call.prefix_fingerprint,
        "fingerprint_key_id": FINGERPRINT_KEY_ID,
        "prefix_tokens": call.prefix_tokens,
        "prefix_token_count_method": "provider_count_tokens",
    }


def build_events(calls: list[Call]) -> list[dict[str, Any]]:
    events: list[tuple[int, int, dict[str, Any]]] = []
    for call in calls:
        api_family = "responses" if call.provider == "openai" else "messages"
        started_payload: dict[str, Any] = {
            "provider": call.provider,
            "api_family": api_family,
            "model_requested": call.model_requested,
            "stream": False,
            **_prefix_fields(call),
        }
        events.append(
            (
                call.started_at_ms,
                0,
                _envelope(
                    f"ev_{call.call_id}_started",
                    "call_started",
                    call.started_at_ms,
                    _context(call),
                    started_payload,
                ),
            )
        )
        finished_payload: dict[str, Any] = {
            "provider": call.provider,
            "api_family": api_family,
            "model_requested": call.model_requested,
            "model_resolved": call.model_resolved,
            "stream": False,
            "status": call.status,
            "usage_format": call.usage_format,
            "usage": call.usage,
            **_prefix_fields(call),
        }
        if call.error_class:
            finished_payload["error_class"] = call.error_class
        events.append(
            (
                call.ended_at_ms,
                1,
                _envelope(
                    f"ev_{call.call_id}_finished_1",
                    "call_finished",
                    call.ended_at_ms,
                    _context(call),
                    finished_payload,
                ),
            )
        )

    by_run: dict[int, list[Call]] = defaultdict(list)
    for call in calls:
        by_run[call.run].append(call)
    for run, run_calls in sorted(by_run.items()):
        terminal = max(c.ended_at_ms for c in run_calls) + 500
        failed = run in FAILED_RUNS
        dispositions = []
        for c in run_calls:
            item: dict[str, Any] = {
                "call_id": c.call_id,
                "disposition": c.disposition,
                "reason": c.disposition_reason,
            }
            if c.removal_safe is not None:
                item["removal_safe_in_scenario"] = c.removal_safe
            dispositions.append(item)
        rid = f"run_{run:03d}"
        events.append(
            (
                terminal,
                2,
                _envelope(
                    f"ev_{rid}_outcome_1",
                    "outcome",
                    terminal,
                    {
                        "workflow_id": WORKFLOW_ID,
                        "workflow_run_id": rid,
                        "scope_id": SCOPE_ANTHROPIC,
                    },
                    {
                        "status": "failed" if failed else "succeeded",
                        "success": not failed,
                        "outcome_source": "synthetic_driver",
                        "terminal_at_ms": terminal,
                        "call_dispositions": dispositions,
                    },
                ),
            )
        )
    events.sort(key=lambda item: (item[0], item[1], item[2]["event_id"]))
    return [e for _, _, e in events]


# ------------------------------------------------------------- provider fixtures
def _dec(value: Decimal) -> str:
    return format(value, "f")


def _dec12(value: Decimal) -> str:
    return format(value.quantize(TWELVE_PLACES), "f")


def _day_label(day_ms: int) -> str:
    return "2026-09-26" if day_ms == DAY1_MS else "2026-09-27"


def _openai_records(
    day_ms: int, model: str, totals: dict[str, int], scope: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    window = {"window_start_ms": day_ms, "window_end_ms": day_ms + DAY}
    usage_record = {
        "record_id": f"usage_openai_{_day_label(day_ms)}_{model}",
        **window,
        "dimensions_json": {"project_id": scope, "model": model},
        "amount_original": None,
        "amount_unit": None,
        "currency": None,
        "usage_json": {
            "input_tokens": totals["input_uncached"] + totals["input_cache_read"],
            "input_cached_tokens": totals["input_cache_read"],
            "input_cache_write_tokens": 0,
            "input_uncached_tokens": totals["input_uncached"],
            "output_tokens": totals["output"],
            "num_model_requests": totals["requests"],
        },
    }
    line_items = {
        f"{model}, input_tokens": ("input_uncached", totals["input_uncached"]),
        f"{model}, input_cached_tokens": ("input_cache_read", totals["input_cache_read"]),
        f"{model}, output_tokens": ("output", totals["output"]),
    }
    cost_records = []
    for line_item, (resource, quantity) in line_items.items():
        amount = Decimal(quantity) * RATES[model][resource] / MILLION
        line_key = line_item.replace(", ", "_")
        cost_records.append(
            {
                "record_id": f"cost_openai_{_day_label(day_ms)}_{line_key}",
                **window,
                "dimensions_json": {"project_id": scope, "line_item": line_item},
                "amount_original": _dec(amount),
                "amount_unit": "usd",
                "currency": "USD",
                "usage_json": {"quantity": quantity, "quantity_unit": "tokens"},
            }
        )
    return usage_record, cost_records


ANTHROPIC_TOKEN_TYPES: dict[str, tuple[str, str]] = {
    "uncached_input_tokens": ("input_uncached", "Input Tokens"),
    "cache_read_input_tokens": ("input_cache_read", "Cache Read Input Tokens"),
    "cache_creation.ephemeral_5m_input_tokens": (
        "input_cache_write_5m",
        "Cache Write 5m Input Tokens",
    ),
    "output_tokens": ("output", "Output Tokens"),
}


def _anthropic_records(
    day_ms: int, model: str, totals: dict[str, int], scope: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    window = {"window_start_ms": day_ms, "window_end_ms": day_ms + DAY}
    usage_record = {
        "record_id": f"usage_anthropic_{_day_label(day_ms)}_{model}",
        **window,
        "dimensions_json": {"workspace_id": scope, "model": model},
        "amount_original": None,
        "amount_unit": None,
        "currency": None,
        "usage_json": {
            "uncached_input_tokens": totals["input_uncached"],
            "cache_creation": {
                "ephemeral_5m_input_tokens": totals["input_cache_write_5m"],
                "ephemeral_1h_input_tokens": 0,
            },
            "cache_read_input_tokens": totals["input_cache_read"],
            "output_tokens": totals["output"],
        },
    }
    cost_records = []
    for token_type, (resource, label) in ANTHROPIC_TOKEN_TYPES.items():
        quantity = totals[resource]
        amount = Decimal(quantity) * RATES[model][resource] / MILLION
        if day_ms == DAY2_MS and resource == "output":
            amount += UNEXPLAINED_RESIDUE_USD
        cost_records.append(
            {
                "record_id": f"cost_anthropic_{_day_label(day_ms)}_{token_type}",
                **window,
                "dimensions_json": {
                    "workspace_id": scope,
                    "description": f"Fixture Anthropic Usage - {label}",
                    "model": model,
                    "token_type": token_type,
                    "cost_type": "tokens",
                },
                "amount_original": _dec(amount * 100),
                "amount_unit": "cents",
                "currency": "USD",
                "usage_json": None,
            }
        )
    return usage_record, cost_records


def build_provider_fixtures(calls: list[Call]) -> dict[str, dict[str, Any]]:
    """Synthetic usage/cost snapshots per provider in the section 7.4 import format."""

    usage_totals: dict[tuple[str, int, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for call in calls:
        if call.usage is None:
            continue
        key = (call.provider, call.day_ms(), call.model_resolved)
        for resource, quantity in call.tokens.items():
            usage_totals[key][resource] += quantity
        usage_totals[key]["requests"] += 1

    # the phantom call the collector never saw (Anthropic, day 1)
    phantom_key = ("anthropic", DAY1_MS, "fixture-anthropic-v1")
    for resource, quantity in PHANTOM_CALL_TOKENS.items():
        usage_totals[phantom_key][resource] += quantity
    usage_totals[phantom_key]["requests"] += 1

    fetched_at = DAY2_MS + DAY + 6 * 3600 * MS  # pulled the morning after the last day
    fixtures: dict[str, dict[str, Any]] = {}
    grains = {
        ("openai", "usage"): "1d/model,project_id",
        ("openai", "cost"): "1d/line_item,project_id",
        ("anthropic", "usage"): "1d/model,workspace_id",
        ("anthropic", "cost"): "1d/description,workspace_id",
    }

    for provider in ("openai", "anthropic"):
        scope = SCOPE_OPENAI if provider == "openai" else SCOPE_ANTHROPIC
        usage_records: list[dict[str, Any]] = []
        cost_records: list[dict[str, Any]] = []
        for (prov, day_ms, model), totals in sorted(usage_totals.items()):
            if prov != provider:
                continue
            builder = _openai_records if provider == "openai" else _anthropic_records
            usage_record, costs = builder(day_ms, model, totals, scope)
            usage_records.append(usage_record)
            cost_records.extend(costs)
        for kind, records in (("usage", usage_records), ("cost", cost_records)):
            raw = json.dumps({"records": records}, indent=2, sort_keys=True) + "\n"
            manifest = {
                "schema_version": "0.1",
                "snapshot_id": f"snap_{DATASET_ID}_{provider}_{kind}",
                "provider": provider,
                "scope_id": scope,
                "record_kind": "provider_usage" if kind == "usage" else "provider_cost",
                "data_kind": "synthetic",
                "grain": grains[(provider, kind)],
                "query_window": {"start_ms": DAY1_MS, "end_ms": DAY2_MS + DAY},
                "fetched_at_ms": fetched_at,
                "source_ref": f"synthetic://{DATASET_ID}/{provider}/{kind}",
                "source_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                "finality": "provisional",
                "snapshot_complete": True,
                "record_count": len(records),
                "currency": "USD",
                "notes": (
                    "Synthetic fixture. Structure mimics the provider report; "
                    "amounts are artificial."
                ),
                "source_type": "file_import",
                "scope_dedicated": True,
            }
            fixtures[f"{provider}_{kind}"] = {"body_text": raw, "manifest": manifest}
    return fixtures


# -------------------------------------------------------------- expected values
def _scenario(n: int, t: int, pu: Decimal, pw: Decimal, pr: Decimal, segments: int) -> dict:
    """PLAN.md section 8.3 with ``segments`` cold starts (each writes once)."""

    no_cache = Decimal(n) * t * pu / MILLION
    cached = Decimal(segments) * t * pw / MILLION + Decimal(n - segments) * t * pr / MILLION
    return {
        "no_cache_cost_usd": _dec(no_cache),
        "cache_cost_usd": _dec(cached),
        "savings_usd": _dec(no_cache - cached),
    }


def build_expected(calls: list[Call], fixtures: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_provider_day: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    tokens: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total = Decimal(0)
    missing_usage = 0
    for call in calls:
        cost = call.cost_usd()
        if cost is None:
            missing_usage += 1
            continue
        total += cost
        by_provider_day[(call.provider, _day_label(call.day_ms()))] += cost
        for resource, quantity in call.tokens.items():
            tokens[call.provider][resource] += quantity

    provider_cost: dict[str, dict[str, str]] = defaultdict(dict)
    for key, fixture in fixtures.items():
        provider, kind = key.split("_")
        if kind != "cost":
            continue
        per_day: dict[str, Decimal] = defaultdict(Decimal)
        for record in json.loads(fixture["body_text"])["records"]:
            amount = Decimal(record["amount_original"])
            if record["amount_unit"] == "cents":
                amount /= 100
            per_day[_day_label(record["window_start_ms"])] += amount
        for day, amount in sorted(per_day.items()):
            provider_cost[provider][day] = _dec(amount)

    rates = RATES["fixture-anthropic-v1"]
    pu, pr = rates["input_uncached"], rates["input_cache_read"]
    pw5, pw1 = rates["input_cache_write_5m"], rates["input_cache_write_1h"]

    def group_calls(fingerprint: str) -> tuple[int, int]:
        members = [c for c in calls if c.prefix_fingerprint == fingerprint]
        return len(members), sum(1 for c in members if c.usage is not None)

    a_all, a_usage = group_calls("a" * 64)
    b_all, b_usage = group_calls("b" * 64)
    c_all, c_usage = group_calls("c" * 64)
    context = {
        "group_a_uncached_repeat": {
            "appearances": a_all,
            "calls_with_usage": a_usage,
            "prefix_tokens": 3000,
            "ephemeral_5m": _scenario(a_usage, 3000, pu, pw5, pr, 1),
            "break_even_reuses": 2,
            "expected_recommendation": "beneficial",
        },
        "group_b_already_cached": {
            "appearances": b_all,
            "calls_with_usage": b_usage,
            "observed_cache_read_tokens": sum(
                c.tokens.get("input_cache_read", 0)
                for c in calls
                if c.prefix_fingerprint == "b" * 64
            ),
            "observed_cache_write_tokens": sum(
                c.tokens.get("input_cache_write_5m", 0)
                for c in calls
                if c.prefix_fingerprint == "b" * 64
            ),
            "expected_recommendation": "already_cached",
        },
        "group_c_cross_ttl": {
            "appearances": c_all,
            "calls_with_usage": c_usage,
            "prefix_tokens": 3000,
            "ephemeral_5m": _scenario(c_usage, 3000, pu, pw5, pr, c_usage),
            "ephemeral_1h": _scenario(c_usage, 3000, pu, pw1, pr, 1),
            "expected_recommendation": "beneficial",
            "expected_policy": "ephemeral_1h",
        },
        "group_d_single_call": {
            "appearances": 1,
            "calls_with_usage": 1,
            "prefix_tokens": 1500,
            "ephemeral_5m": _scenario(1, 1500, pu, pw5, pr, 1),
            "expected_recommendation": "not_beneficial",
        },
        "reasoning_calls_without_prefix_evidence": sum(
            1 for c in calls if c.node_id == "reasoning" and c.prefix_fingerprint is None
        ),
    }

    discarded_paid = [c for c in calls if c.disposition == "discarded" and c.usage is not None]
    failed_run_calls = [c for c in calls if c.run in FAILED_RUNS]
    flagged_ids = {c.call_id for c in discarded_paid} | {
        c.call_id for c in failed_run_calls if c.usage is not None
    }
    unique_flagged = sum((c.cost_usd() or Decimal(0)) for c in calls if c.call_id in flagged_ids)
    phantom_cost = sum(
        Decimal(q) * RATES["fixture-anthropic-v1"][r] / MILLION
        for r, q in PHANTOM_CALL_TOKENS.items()
    )

    return {
        "dataset_id": DATASET_ID,
        "seed": SEED,
        "source_version": SOURCE_VERSION,
        "catalog_version": CATALOG_VERSION,
        "runs": 100,
        "succeeded_runs": 100 - len(FAILED_RUNS),
        "failed_runs": len(FAILED_RUNS),
        "calls": len(calls),
        "call_events": 2 * len(calls),
        "outcome_events": 100,
        "retry_runs": len(PAID_RETRY_RUNS | ERROR_RETRY_RUNS),
        "fallback_runs": len(REDUNDANT_FALLBACK_RUNS | USED_FALLBACK_RUNS),
        "calls_with_missing_usage": missing_usage,
        "boundary_calls": 1,
        "tokens": {p: dict(sorted(t.items())) for p, t in sorted(tokens.items())},
        "estimated_known_cost_usd": _dec(total),
        "estimated_cost_by_provider_day_usd": {
            f"{p}/{d}": _dec(v) for (p, d), v in sorted(by_provider_day.items())
        },
        "provider_cost_usd": {p: dict(v) for p, v in sorted(provider_cost.items())},
        "phantom_call_cost_usd": _dec(phantom_cost),
        "unexplained_residue_usd": _dec(UNEXPLAINED_RESIDUE_USD),
        "cost_per_successful_outcome_usd": _dec12(total / Decimal(100 - len(FAILED_RUNS))),
        "cost_per_successful_outcome_is_lower_bound": missing_usage > 0,
        "waste": {
            "discarded_paid_attempts": len(discarded_paid),
            "discarded_paid_cost_usd": _dec(
                sum(c.cost_usd() or Decimal(0) for c in discarded_paid)
            ),
            "failed_run_known_cost_usd": _dec(
                sum(c.cost_usd() or Decimal(0) for c in failed_run_calls)
            ),
            "failed_run_unknown_cost_calls": sum(1 for c in failed_run_calls if c.usage is None),
            "unique_flagged_cost_usd": _dec(unique_flagged),
        },
        "context_economics": context,
    }


def build_catalog() -> dict[str, Any]:
    records = []
    for model, rates in RATES.items():
        provider = "openai" if "openai" in model else "anthropic"
        for resource, price in rates.items():
            records.append(
                {
                    "price_id": f"{model}-{resource}",
                    "catalog_version": CATALOG_VERSION,
                    "provider": provider,
                    "model_id": model,
                    "api_family": None,
                    "resource": resource,
                    "service_tier": "standard",
                    "region": "global",
                    "context_band": "default",
                    "effective_from_ms": 0,
                    "effective_to_ms": None,
                    "currency": "USD",
                    "unit": "token",
                    "unit_quantity": 1_000_000,
                    "unit_price": _dec(price),
                    "source_url": "synthetic://aiecon/fixture-rates",
                    "retrieved_at": "2026-09-26",
                    "effective_date_basis": "synthetic",
                    "notes": "Artificial rate for the synthetic demo. Not a real provider price.",
                }
            )
    aliases = [
        {
            "provider": "openai",
            "alias": "fixture-openai",
            "model_id": "fixture-openai-v1",
            "source_url": "synthetic://aiecon/fixture-rates",
            "retrieved_at": "2026-09-26",
            "notes": None,
        },
        {
            "provider": "anthropic",
            "alias": "fixture-anthropic",
            "model_id": "fixture-anthropic-v1",
            "source_url": "synthetic://aiecon/fixture-rates",
            "retrieved_at": "2026-09-26",
            "notes": None,
        },
    ]
    cache_contracts = [
        {
            "provider": "openai",
            "model_id": "fixture-openai-v1",
            "cache_write_tiers": [],
            "supported_cache_policies": ["provider_auto"],
            "min_cacheable_prefix_tokens": 1024,
            "source_url": "synthetic://aiecon/fixture-rates",
            "retrieved_at": "2026-09-26",
            "notes": "Synthetic contract: automatic caching, no write charge.",
        },
        {
            "provider": "anthropic",
            "model_id": "fixture-anthropic-v1",
            "cache_write_tiers": ["input_cache_write_5m", "input_cache_write_1h"],
            "supported_cache_policies": ["ephemeral_5m", "ephemeral_1h"],
            "min_cacheable_prefix_tokens": 1024,
            "source_url": "synthetic://aiecon/fixture-rates",
            "retrieved_at": "2026-09-26",
            "notes": "Synthetic contract: explicit 5m and 1h cache tiers.",
        },
    ]
    return {
        "catalog_version": CATALOG_VERSION,
        "catalog_kind": "synthetic",
        "description": (
            "Artificial rates for fixture-* models used by the offline demo. Never real prices."
        ),
        "currency": "USD",
        "records": records,
        "aliases": aliases,
        "cache_contracts": cache_contracts,
    }


# --------------------------------------------------------------------- writing
def generate() -> dict[str, Any]:
    rng = random.Random(SEED)
    calls = build_calls(rng)
    events = build_events(calls)
    fixtures = build_provider_fixtures(calls)
    expected = build_expected(calls, fixtures)
    return {"calls": calls, "events": events, "fixtures": fixtures, "expected": expected}


def render_files(bundle: dict[str, Any]) -> dict[str, str]:
    files: dict[str, str] = {}
    files["events.jsonl"] = "".join(
        json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n"
        for event in bundle["events"]
    )
    for key, fixture in bundle["fixtures"].items():
        files[f"provider/{key}.json"] = fixture["body_text"]
        files[f"provider/{key}.manifest.json"] = (
            json.dumps(fixture["manifest"], indent=2, sort_keys=True) + "\n"
        )
    files["expected_metrics.json"] = json.dumps(bundle["expected"], indent=2, sort_keys=True) + "\n"
    files["synthetic-catalog.json"] = json.dumps(build_catalog(), indent=2, sort_keys=True) + "\n"
    return files


def write_fixtures(out_dir: Path) -> list[Path]:
    out_dir = Path(out_dir)
    written = []
    for relative, text in render_files(generate()).items():
        path = out_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Regenerate the synthetic demo fixtures.")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    for path in write_fixtures(args.out):
        print(path)


if __name__ == "__main__":
    main()
