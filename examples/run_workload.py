#!/usr/bin/env python
"""Explicit live workload through the LiteLLM example proxy (PLAN.md 10.3, 11.2, 11.3).

Default is a dry run that prints the plan and the conservative reservation per call without
touching the network. Paid requests need ``--live --budget-usd N --yes-spend``.

Every retry and fallback is performed explicitly by this script (router retries are disabled
in examples/litellm/config.yaml) so each paid attempt is exactly one recorded call. The
script names each attempt (``metadata.aiecon.call_id``) so it can label dispositions in the
outcome events it writes itself (``source_type=application_sdk``).

Steps per provider (18 calls in total): one plain call, one streaming call, three calls
sharing a cacheable prefix, one paid attempt superseded by an explicit retry, and one
explicit cross-provider fallback pair (the fallback call goes to the other provider).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from aiecon.adapters.normalize import normalize_usage
from aiecon.collect.spend import SpendFuse, SpendStop, conservative_reserve
from aiecon.collect.writer import JsonlWriter
from aiecon.config import LIVE_MODEL_ENVS, env_value_non_secret, now_ms, resolve_workspace
from aiecon.pricing import CatalogIndex, load_catalog
from aiecon.pricing.estimate import estimate
from aiecon.privacy import fingerprint_key_id, load_fingerprint_key, prefix_fingerprint
from aiecon.spec import (
    ApiFamily,
    CallDisposition,
    CallStatus,
    DataKind,
    Disposition,
    EventContext,
    EventType,
    LineItemStatus,
    ModelCall,
    OutcomePayload,
    OutcomeSource,
    OutcomeStatus,
    Provider,
    RawEnvelope,
    SourceType,
    UsageFormat,
)
from aiecon.spec.common import NORMALIZER_VERSION
from aiecon.storage import Workspace

ROUTES = {"openai": "openai-live", "anthropic": "anthropic-live"}
OTHER = {"openai": "anthropic", "anthropic": "openai"}
PREFIX_LINES = 700  # about 5k tokens; Claude Haiku 4.5 needs at least 4,096 cacheable tokens
MAX_OUTPUT_TOKENS = 120
SOURCE_VERSION = "run_workload-0.1"


@dataclass
class Step:
    call_id: str
    run_id: str
    node_id: str
    node_run_id: str
    provider: str
    attempt_index: int
    kind: str
    messages: list[dict[str, Any]]
    stream: bool
    disposition: Disposition
    reason: str
    removal_safe: bool | None = None
    prefix_text: str | None = None
    cache_policy: str | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)
    cost_usd: Decimal | None = None
    status: str = "pending"


def synthetic_prefix() -> str:
    lines = [
        f"Policy {i:03d}: support agents confirm the order number, restate the customer's "
        "question in one sentence, cite the relevant policy section, and escalate refunds "
        "above the approval threshold to a supervisor before replying."
        for i in range(PREFIX_LINES)
    ]
    return "You are a support agent. Follow every policy below.\n" + "\n".join(lines)


def user_turn(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def build_plan(prefix: str, exec_tag: str | None = None) -> list[Step]:
    """One execution's plan. Run ids carry a per-execution tag so that repeating the script
    never reuses a workflow run id (outcome event ids derive from run ids, and the same id
    with different content is an ingest conflict, not a second run)."""

    exec_tag = exec_tag or uuid.uuid4().hex[:6]
    steps: list[Step] = []
    for provider in ("openai", "anthropic"):
        tag = f"{exec_tag}_{provider[:3]}"

        def step(_tag: str = tag, **kw: Any) -> Step:
            defaults = {
                "call_id": f"call_wl_{_tag}_{uuid.uuid4().hex[:12]}",
                "disposition": Disposition.used,
                "reason": "used_as_final",
                "stream": False,
            }
            defaults.update(kw)
            return Step(**defaults)

        # 1. plain
        run = f"wl_{tag}_plain"
        steps.append(
            step(
                run_id=run,
                node_id="answer",
                node_run_id=f"{run}_answer",
                provider=provider,
                attempt_index=1,
                kind="plain",
                messages=[user_turn("Reply with the single word: ready.")],
            )
        )
        # 2. streaming
        run = f"wl_{tag}_stream"
        steps.append(
            step(
                run_id=run,
                node_id="answer",
                node_run_id=f"{run}_answer",
                provider=provider,
                attempt_index=1,
                kind="stream",
                stream=True,
                messages=[user_turn("Count from one to five, one number per line.")],
            )
        )
        # 3. prefix reuse: three sequential calls sharing the same cacheable prefix
        for i in range(1, 4):
            run = f"wl_{tag}_prefix_{i}"
            if provider == "anthropic":
                system = {
                    "role": "system",
                    "content": [
                        {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}
                    ],
                }
                policy = "ephemeral_5m"
                extra: dict[str, Any] = {}
            else:
                system = {"role": "system", "content": prefix}
                policy = "provider_auto"
                extra = {"prompt_cache_key": "aiecon-workload-prefix"}
            steps.append(
                step(
                    run_id=run,
                    node_id="answer",
                    node_run_id=f"{run}_answer",
                    provider=provider,
                    attempt_index=1,
                    kind="prefix",
                    messages=[
                        system,
                        user_turn(f"Which policy number covers refunds? Question {i}."),
                    ],
                    prefix_text=prefix,
                    cache_policy=policy,
                    extra_body=extra,
                )
            )
        # 4. paid attempt superseded by an explicit retry
        run = f"wl_{tag}_retry"
        node_run = f"{run}_answer"
        steps.append(
            step(
                run_id=run,
                node_id="answer",
                node_run_id=node_run,
                provider=provider,
                attempt_index=1,
                kind="retry_attempt",
                messages=[user_turn("Give a one-line summary of the refund policy.")],
                disposition=Disposition.discarded,
                reason="superseded_by_retry",
                removal_safe=True,
            )
        )
        steps.append(
            step(
                run_id=run,
                node_id="answer",
                node_run_id=node_run,
                provider=provider,
                attempt_index=2,
                kind="retry",
                messages=[user_turn("Give a one-line summary of the refund policy.")],
            )
        )
        # 5. explicit cross-provider fallback pair
        run = f"wl_{tag}_fallback"
        node_run = f"{run}_answer"
        primary_used = provider == "anthropic"
        steps.append(
            step(
                run_id=run,
                node_id="answer",
                node_run_id=node_run,
                provider=provider,
                attempt_index=1,
                kind="fallback_primary",
                messages=[user_turn("Name one reason a refund can be declined.")],
                disposition=Disposition.used if primary_used else Disposition.discarded,
                reason="used_as_final" if primary_used else "superseded_by_fallback",
                removal_safe=None if primary_used else True,
            )
        )
        steps.append(
            step(
                run_id=run,
                node_id="answer",
                node_run_id=node_run,
                provider=OTHER[provider],
                attempt_index=2,
                kind="fallback",
                messages=[user_turn("Name one reason a refund can be declined.")],
                disposition=Disposition.discarded if primary_used else Disposition.used,
                reason="redundant_fallback" if primary_used else "used_as_final",
                removal_safe=True if primary_used else None,
            )
        )
    return steps


def approx_tokens(messages: list[dict[str, Any]]) -> int:
    chars = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            chars += sum(len(part.get("text", "")) for part in content if isinstance(part, dict))
    return chars // 3 + 32  # conservative: 3 chars per token plus framing


def request_body(step: Step, scope_ids: dict[str, str], fp_key: bytes | None) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "workflow_id": "workload",
        "workflow_run_id": step.run_id,
        "node_id": step.node_id,
        "node_run_id": step.node_run_id,
        "call_id": step.call_id,
        "scope_id": scope_ids[step.provider],
    }
    if step.prefix_text is not None:
        meta["cache_policy"] = step.cache_policy
        meta["prefix_tokens"] = len(step.prefix_text) // 4
        meta["prefix_token_count_method"] = "estimated_chars_div_4"
        if fp_key is not None:
            meta["prefix_fingerprint"] = prefix_fingerprint(fp_key, [step.prefix_text])
            meta["fingerprint_key_id"] = fingerprint_key_id(fp_key)
    body: dict[str, Any] = {
        "model": ROUTES[step.provider],
        "messages": step.messages,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "stream": step.stream,
        "metadata": {"aiecon": meta},
        **step.extra_body,
    }
    if step.provider == "openai":
        # seen live: gpt-5-nano spent the whole 120-token output budget on reasoning and
        # returned no visible text; minimal effort keeps the answers (and the demo) readable
        body["reasoning_effort"] = "minimal"
    if step.stream:
        body["stream_options"] = {"include_usage": True}
    return body


def send(
    client: httpx.Client, proxy_url: str, body: dict[str, Any]
) -> tuple[dict | None, str | None]:
    url = f"{proxy_url.rstrip('/')}/v1/chat/completions"
    if not body.get("stream"):
        response = client.post(url, json=body)
        response.raise_for_status()
        data = response.json()
        return data.get("usage"), data.get("model")
    usage = None
    model = None
    with client.stream("POST", url, json=body) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            model = chunk.get("model") or model
            if chunk.get("usage"):
                usage = chunk["usage"]  # the final chunk carries the whole request's usage
    return usage, model


def compute_cost(
    index: CatalogIndex, provider: str, model_id: str, usage: dict | None, ts_ms: int
) -> Decimal | None:
    normalized = normalize_usage(UsageFormat.litellm_standard, usage)
    if normalized.completeness.value != "complete":
        return None
    call = ModelCall(
        dataset_id="spend-check",
        call_id="c",
        data_kind=DataKind.live,
        scope_id="s",
        normalizer_version=NORMALIZER_VERSION,
        provider=Provider(provider),
        api_family=ApiFamily.unknown,
        model_requested=model_id,
        model_resolved=model_id,
        status=CallStatus.success,
        started_at_ms=ts_ms,
        ended_at_ms=ts_ms,
        usage_format=UsageFormat.litellm_standard,
        input_total_tokens=normalized.input_total,
        input_uncached_tokens=normalized.input_uncached,
        input_cache_read_tokens=normalized.input_cache_read,
        input_cache_write_tokens=normalized.input_cache_write,
        output_tokens=normalized.output,
        cache_write_breakdown=normalized.cache_write_breakdown,
        usage_completeness=normalized.completeness,
        usage_notes=list(normalized.notes),
    )
    items = estimate(call, index, "spend-check")
    if any(item.status is LineItemStatus.unpriced for item in items):
        return None
    return sum((item.line_cost for item in items if item.line_cost is not None), start=Decimal(0))


def outcome_envelope(
    dataset_id: str, run_id: str, steps: list[Step], scope_id: str, ts_ms: int
) -> RawEnvelope:
    all_done = all(s.status == "ok" for s in steps)
    dispositions = [
        CallDisposition(
            call_id=s.call_id,
            disposition=s.disposition if s.status == "ok" else Disposition.discarded,
            reason=s.reason if s.status == "ok" else "attempt_failed",
            removal_safe_in_scenario=s.removal_safe,
        )
        for s in steps
    ]
    return RawEnvelope(
        event_id=f"ev_{run_id}_outcome_1",
        event_type=EventType.outcome,
        dataset_id=dataset_id,
        data_kind=DataKind.live,
        source_type=SourceType.application_sdk,
        source_version=SOURCE_VERSION,
        observed_at_ms=ts_ms,
        occurred_at_ms=ts_ms,
        context=EventContext(workflow_id="workload", workflow_run_id=run_id, scope_id=scope_id),
        payload=OutcomePayload(
            status=OutcomeStatus.succeeded if all_done else OutcomeStatus.failed,
            success=all_done,
            outcome_source=OutcomeSource.application_label,
            terminal_at_ms=ts_ms,
            call_dispositions=dispositions,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--catalog", type=Path, default=Path("catalogs/live-demo.json"))
    parser.add_argument("--proxy-url", default="http://127.0.0.1:4000")
    parser.add_argument("--dataset-id", default="live-demo")
    parser.add_argument("--scope-openai", default="live_openai_project")
    parser.add_argument("--scope-anthropic", default="live_anthropic_workspace")
    parser.add_argument("--dry-run", action="store_true", help="default; never sends requests")
    parser.add_argument("--live", action="store_true", help="send paid requests")
    parser.add_argument("--budget-usd", type=Decimal, default=None)
    parser.add_argument("--max-calls", type=int, default=40)
    parser.add_argument(
        "--yes-spend", action="store_true", help="confirm that real money will be spent"
    )
    parser.add_argument(
        "--providers",
        default="openai,anthropic",
        help=(
            "comma-separated subset of openai,anthropic; runs that involve another provider "
            "(the cross-provider fallback pairs) are dropped as a whole"
        ),
    )
    parser.add_argument(
        "--clear-stop",
        action="store_true",
        help=(
            "lift a stop latch persisted by an earlier run (e.g. unknown_cost); held amounts "
            "stay committed against the budget"
        ),
    )
    args = parser.parse_args()
    allowed = {p.strip() for p in args.providers.split(",") if p.strip()}
    if not allowed or not allowed <= set(ROUTES):
        print(f"--providers must be a subset of {','.join(ROUTES)}", file=sys.stderr)
        return 2

    workspace = Workspace(resolve_workspace(args.workspace))
    index = CatalogIndex(load_catalog(args.catalog))
    scope_ids = {"openai": args.scope_openai, "anthropic": args.scope_anthropic}
    models: dict[str, str | None] = {}
    for provider, env_name in LIVE_MODEL_ENVS.items():
        requested = env_value_non_secret(env_name)
        resolved, basis = index.resolve_model(provider, requested, requested)
        models[provider] = resolved
        print(f"{provider:10s} model={requested or '<unset>'} -> {resolved or 'UNKNOWN'} ({basis})")

    prefix = synthetic_prefix()
    steps = build_plan(prefix)
    by_run: dict[str, list[Step]] = {}
    for s in steps:
        by_run.setdefault(s.run_id, []).append(s)
    steps = [s for s in steps if all(t.provider in allowed for t in by_run[s.run_id])]
    if len(allowed) < len(ROUTES):
        print(f"providers restricted to {', '.join(sorted(allowed))}: {len(steps)} calls kept")
    ts = now_ms()
    print(f"\nplan: {len(steps)} calls, max output {MAX_OUTPUT_TOKENS} tokens each")
    total_reserve = Decimal(0)
    reserve_by_step: dict[str, Decimal | None] = {}
    for s in steps:
        model_id = models[s.provider]
        reserve = None
        if model_id:
            reserve = conservative_reserve(
                index,
                provider=s.provider,
                model_id=model_id,
                ts_ms=ts,
                input_tokens_upper=approx_tokens(s.messages),
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
        reserve_by_step[s.call_id] = reserve
        total_reserve += reserve or Decimal(0)
        shown = "unknown" if reserve is None else format(reserve, "f")
        mode = "stream " if s.stream else ""
        print(f"  {s.kind:16s} {s.provider:9s} attempt {s.attempt_index} {mode}reserve={shown}")
    print(f"total conservative reserve: ${total_reserve:f}")

    if not args.live:
        print("\nDRY RUN: no requests were sent. Add --live --budget-usd N --yes-spend to spend.")
        return 0
    if not args.yes_spend or args.budget_usd is None or args.budget_usd <= 0:
        print(
            "refusing to spend: --live requires --budget-usd > 0 and --yes-spend", file=sys.stderr
        )
        return 2
    if any(models[p] is None for p in allowed):
        print("refusing to spend: a live model is unknown to the catalog", file=sys.stderr)
        return 2
    if not workspace.exists:
        print(f"refusing to spend: workspace {workspace.root} is not initialised", file=sys.stderr)
        return 2
    if os.environ.get("CI"):
        print("refusing to spend inside CI", file=sys.stderr)
        return 2

    fuse = SpendFuse(
        workspace.state_dir / "spend.json", budget_usd=args.budget_usd, max_calls=args.max_calls
    )
    if args.clear_stop:
        cleared = fuse.clear_stop()
        if cleared:
            print(
                f"stop latch '{cleared}' cleared on request; committed so far "
                f"${fuse.committed:f} of ${fuse.budget:f}"
            )
    elif fuse.state.stopped_reason:
        print(
            f"refusing to spend: earlier run stopped ({fuse.state.stopped_reason}); "
            "add --clear-stop to continue against the same budget",
            file=sys.stderr,
        )
        return 2
    writer = JsonlWriter(workspace.raw_dir, file_prefix="workload")
    fp_key = load_fingerprint_key()
    client = httpx.Client(timeout=httpx.Timeout(60.0))
    runs: dict[str, list[Step]] = {}
    try:
        for s in steps:
            runs.setdefault(s.run_id, []).append(s)
            reserve = reserve_by_step[s.call_id]
            if reserve is None:
                fuse.stop("unknown_price")
                print(f"stop: no price for {s.provider} {models[s.provider]}")
                break
            try:
                fuse.reserve(s.call_id, reserve)
            except SpendStop as exc:
                print(f"stop before dispatch: {exc}")
                break
            body = request_body(s, scope_ids, fp_key)
            try:
                usage, _model = send(client, args.proxy_url, body)
            except httpx.HTTPError as exc:
                s.status = "error"
                fuse.settle(s.call_id, None)
                print(f"  {s.kind:16s} {s.provider:9s} HTTP error {type(exc).__name__}; stopping")
                break
            cost = compute_cost(index, s.provider, models[s.provider] or "", usage, now_ms())
            s.cost_usd = cost
            s.status = "ok"
            fuse.settle(s.call_id, cost)
            print(
                f"  {s.kind:16s} {s.provider:9s} attempt {s.attempt_index} "
                f"cost={'unknown' if cost is None else format(cost, 'f')}"
            )
            if cost is None:
                print("stop: cost could not be computed from usage; reservation kept")
                break
            if writer.failed:
                print("stop: outcome writer reported failures")
                break
    finally:
        client.close()
        for run_id, run_steps in runs.items():
            if any(s.status != "pending" for s in run_steps):
                writer.write(
                    outcome_envelope(
                        args.dataset_id,
                        run_id,
                        run_steps,
                        scope_ids[run_steps[0].provider],
                        now_ms(),
                    )
                )
        writer.close()

    print("\nspend:", json.dumps(fuse.summary(), indent=2))
    print("outcome writer:", json.dumps(writer.health()))
    print(
        "\nnext: aiecon --workspace",
        workspace.root,
        "ingest --input",
        workspace.raw_dir,
        "&& aiecon --workspace",
        workspace.root,
        "estimate --catalog",
        args.catalog,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
