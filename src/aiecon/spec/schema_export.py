"""Export every spec model as JSON Schema (``aiecon schema export``)."""

from __future__ import annotations

import json
from pathlib import Path

from aiecon.spec.call import ModelCall
from aiecon.spec.common import SCHEMA_VERSION, SpecModel
from aiecon.spec.envelope import RawEnvelope
from aiecon.spec.finding import ContextEconGroup, Finding
from aiecon.spec.outcome import Outcome
from aiecon.spec.pricing import CostLineItem, PriceCatalog, PricingRunManifest
from aiecon.spec.provider import ProviderRecord, ProviderSnapshotManifest
from aiecon.spec.reconcile import ReconciliationBucket
from aiecon.spec.runtime import RUNTIME_MODELS

CORE_MODELS: tuple[type[SpecModel], ...] = (
    RawEnvelope,
    ModelCall,
    Outcome,
    ProviderRecord,
    ProviderSnapshotManifest,
    PriceCatalog,
    CostLineItem,
    PricingRunManifest,
    ReconciliationBucket,
    Finding,
    ContextEconGroup,
)


def _extra_models() -> tuple[type[SpecModel], ...]:
    try:
        from aiecon.spec.report import Report, ReportManifest
    except ImportError:  # report model lands with T5.1
        return ()
    return (Report, ReportManifest)


def export_json_schemas(out_dir: Path) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for model in CORE_MODELS + _extra_models() + RUNTIME_MODELS:
        schema = model.model_json_schema(mode="serialization")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://aiecon.dev/schema/{SCHEMA_VERSION}/{model.__name__}.json"
        schema["x-aiecon-schema-version"] = SCHEMA_VERSION
        if model in RUNTIME_MODELS:
            schema["x-experimental"] = True
        path = out_dir / f"{model.__name__}.schema.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written
