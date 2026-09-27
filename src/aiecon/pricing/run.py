"""A pricing run: deterministic id from its inputs, line items, manifest (PLAN.md 4.5, 6)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from aiecon.pricing.catalog import CatalogIndex
from aiecon.pricing.estimate import estimate_many
from aiecon.spec.common import NORMALIZER_VERSION, sha256_hex
from aiecon.spec.pricing import CostLineItem, LineItemStatus, PriceCatalog, PricingRunManifest
from aiecon.storage import Storage


@dataclass
class PricingRunResult:
    manifest: PricingRunManifest
    line_items: list[CostLineItem]
    replaced_existing: bool


def calls_hash(storage: Storage, dataset_id: str) -> tuple[int, str]:
    calls = storage.list_calls(dataset_id)
    digest_input = "\n".join(sorted(f"{c.call_id}:{c.content_hash()}" for c in calls))
    return len(calls), sha256_hex(digest_input)


def pricing_run_id_for(catalog_hash: str, calls_digest: str) -> str:
    return f"pr_{catalog_hash[:12]}_{calls_digest[:12]}_{NORMALIZER_VERSION.replace('.', '')}"


def run_pricing(
    storage: Storage, dataset_id: str, catalog: PriceCatalog, *, now_ms: int
) -> PricingRunResult:
    """Estimate every call in ``dataset_id`` and register the run as the active one.

    Re-running with identical calls and catalog produces the same ``pricing_run_id`` and
    replaces its line items, so repeated estimation never accumulates cost.
    """

    index = CatalogIndex(catalog)
    calls = storage.list_calls(dataset_id)
    count, digest = calls_hash(storage, dataset_id)
    run_id = pricing_run_id_for(index.catalog_hash, digest)
    items = estimate_many(calls, index, run_id)

    per_call_status: dict[str, set[LineItemStatus]] = defaultdict(set)
    subtotal = Decimal(0)
    for item in items:
        per_call_status[item.call_id].add(item.status)
        if item.line_cost is not None:
            subtotal += item.line_cost
    priced = sum(1 for s in per_call_status.values() if s == {LineItemStatus.priced})
    partial = sum(1 for s in per_call_status.values() if len(s) == 2)
    unpriced = sum(1 for s in per_call_status.values() if s == {LineItemStatus.unpriced})

    manifest = PricingRunManifest(
        pricing_run_id=run_id,
        dataset_id=dataset_id,
        catalog_version=catalog.catalog_version,
        catalog_hash=index.catalog_hash,
        catalog_kind=catalog.catalog_kind,
        normalizer_version=NORMALIZER_VERSION,
        created_at_ms=now_ms,
        call_count=count,
        calls_hash=digest,
        priced_call_count=priced,
        partially_priced_call_count=partial,
        unpriced_call_count=unpriced,
        line_item_count=len(items),
        known_cost_subtotal_usd=subtotal,
        cost_complete=(partial == 0 and unpriced == 0 and count > 0),
    )
    existed = storage.active_run("pricing", dataset_id)
    replaced = existed is not None and existed[0] == run_id
    with storage.transaction():
        storage.replace_line_items(run_id, items)
        storage.register_run(
            run_id=run_id,
            run_kind="pricing",
            dataset_id=dataset_id,
            created_at_ms=now_ms,
            manifest_json=manifest.model_dump_json(),
            activate=True,
        )
        # keep the exact catalog so later analysis (context economics, report) can reload it
        storage.set_meta(f"catalog:{run_id}", catalog.model_dump_json())
    return PricingRunResult(manifest=manifest, line_items=items, replaced_existing=replaced)


def load_run_catalog(storage: Storage, pricing_run_id: str) -> PriceCatalog | None:
    """The catalog document persisted with a pricing run, if any."""

    text = storage.get_meta(f"catalog:{pricing_run_id}")
    return None if text is None else PriceCatalog.model_validate_json(text)
