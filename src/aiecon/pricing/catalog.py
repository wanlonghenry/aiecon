"""Catalog loading, explicit alias resolution and time-effective price lookup."""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from importlib import resources
from pathlib import Path

from aiecon.spec.common import ApiFamily, Provider
from aiecon.spec.pricing import ModelCacheContract, PriceCatalog, PriceRecord, Resource


class CatalogError(Exception):
    """The catalog itself is inconsistent (two prices match the same call)."""


def load_catalog(path: Path | str) -> PriceCatalog:
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text, parse_float=Decimal)
    return PriceCatalog.model_validate(data)


def load_synthetic_catalog() -> PriceCatalog:
    text = (
        resources.files("aiecon.data")
        .joinpath("demo_support_v1")
        .joinpath("synthetic-catalog.json")
        .read_text("utf-8")
    )
    return PriceCatalog.model_validate(json.loads(text, parse_float=Decimal))


class CatalogIndex:
    """Read-only lookup structure over a :class:`PriceCatalog`."""

    def __init__(self, catalog: PriceCatalog):
        self.catalog = catalog
        self.catalog_hash = catalog.catalog_hash()
        self._prices: dict[tuple[str, str, Resource], list[PriceRecord]] = defaultdict(list)
        self._models: set[tuple[str, str]] = set()
        for record in catalog.records:
            key = (record.provider.value, record.model_id, record.resource)
            self._prices[key].append(record)
            self._models.add((record.provider.value, record.model_id))
        for records in self._prices.values():
            records.sort(key=lambda r: r.effective_from_ms)
        self._aliases: dict[tuple[str, str], str] = {
            (a.provider.value, a.alias): a.model_id for a in catalog.aliases
        }
        self._contracts: dict[tuple[str, str], ModelCacheContract] = {
            (c.provider.value, c.model_id): c for c in catalog.cache_contracts
        }

    # ------------------------------------------------------------------ models
    def known_model(self, provider: Provider | str, model_id: str) -> bool:
        return (str(getattr(provider, "value", provider)), model_id) in self._models

    def resolve_model(
        self, provider: Provider | str, model_resolved: str | None, model_requested: str | None
    ) -> tuple[str | None, str]:
        """Explicit resolution only: exact id, then declared alias. Never similarity."""

        prov = str(getattr(provider, "value", provider))
        for candidate, basis in ((model_resolved, "resolved"), (model_requested, "requested")):
            if not candidate:
                continue
            if (prov, candidate) in self._models:
                return candidate, f"exact_{basis}"
            alias_target = self._aliases.get((prov, candidate))
            if alias_target is not None and (prov, alias_target) in self._models:
                return alias_target, f"alias_{basis}"
        return None, "unknown_model"

    def contract(self, provider: Provider | str, model_id: str) -> ModelCacheContract | None:
        prov = str(getattr(provider, "value", provider))
        return self._contracts.get((prov, model_id))

    def resources_for(self, provider: Provider | str, model_id: str) -> set[Resource]:
        prov = str(getattr(provider, "value", provider))
        return {res for (p, m, res) in self._prices if p == prov and m == model_id}

    # ------------------------------------------------------------------ prices
    def price_at(
        self,
        provider: Provider | str,
        model_id: str,
        resource: Resource,
        ts_ms: int,
        *,
        api_family: ApiFamily | None = None,
        service_tier: str = "standard",
        region: str = "global",
        context_band: str = "default",
    ) -> PriceRecord | None:
        prov = str(getattr(provider, "value", provider))
        # An unknown api_family may only use family-agnostic prices; a known family prefers
        # its own price and falls back to the generic one. Never the other way round.
        candidates = [
            r
            for r in self._prices.get((prov, model_id, resource), ())
            if r.is_effective_at(ts_ms)
            and r.service_tier == service_tier
            and r.region == region
            and r.context_band == context_band
            and (r.api_family is None or (api_family is not None and r.api_family is api_family))
        ]
        if not candidates:
            return None
        specific = [r for r in candidates if r.api_family is not None]
        chosen = specific or candidates
        if len(chosen) > 1:
            ids = ", ".join(sorted(r.price_id for r in chosen))
            raise CatalogError(f"ambiguous prices for {model_id}/{resource.value}: {ids}")
        return chosen[0]
