"""Experimental runtime placeholders (PLAN.md §17).

These schemas exist so later versions can attach measured runtime resource usage to a
call. v0.1 creates no tables, collectors, GPU allocation or fake measurements for them.
JSON Schema export marks every model here as ``x-experimental``.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from aiecon.spec.common import CodeStr, DecimalStr, IdStr, NonNegInt, SpecModel, UtcMs

EXPERIMENTAL = True


class MeasurementSource(StrEnum):
    vllm_metrics = "vllm_metrics"
    sglang_metrics = "sglang_metrics"
    dcgm = "dcgm"
    application = "application"
    synthetic = "synthetic"
    unknown = "unknown"


class RuntimeRef(SpecModel):
    execution_id: IdStr
    call_id: IdStr | None = None
    window_start_ms: UtcMs
    window_end_ms: UtcMs
    measurement_source: MeasurementSource = MeasurementSource.unknown


class RuntimeExecution(RuntimeRef):
    engine: CodeStr | None = None
    engine_version: str | None = Field(default=None, max_length=100)
    model_id: IdStr | None = None
    node_id: IdStr | None = None


class PrefillUsage(RuntimeRef):
    prefill_tokens: NonNegInt | None = None
    prefill_ms: NonNegInt | None = None


class DecodeUsage(RuntimeRef):
    decode_tokens: NonNegInt | None = None
    decode_ms: NonNegInt | None = None


class CacheEvent(RuntimeRef):
    event: CodeStr
    tokens: NonNegInt | None = None


class GPUUsage(RuntimeRef):
    device_id: IdStr | None = None
    quantity: DecimalStr | None = None
    unit: CodeStr = "gpu_second"


class MemoryResidency(RuntimeRef):
    quantity: DecimalStr | None = None
    unit: CodeStr = "gib_second"


class ResourceCost(RuntimeRef):
    resource: CodeStr
    quantity: DecimalStr | None = None
    unit: CodeStr
    unit_price: DecimalStr | None = None
    cost_usd: DecimalStr | None = None
    evidence_class: CodeStr = "c"


RUNTIME_MODELS: tuple[type[SpecModel], ...] = (
    RuntimeExecution,
    PrefillUsage,
    DecodeUsage,
    CacheEvent,
    GPUUsage,
    MemoryResidency,
    ResourceCost,
)
