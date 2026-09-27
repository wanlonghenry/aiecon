"""Shared primitives for every aiecon spec model.

Rules encoded here (see PLAN.md §2.1, §4, §5.3):

* Money is ``Decimal`` in Python and a decimal *string* in JSON.
* Times are UTC epoch milliseconds; windows are half-open ``[start, end)``.
* Identifiers are opaque, whitespace-free tokens. Free prose is never an ID.
* Unknown values are ``None``, never a silent zero.

Validators raise ``ValueError`` only (pydantic does not convert ``TypeError``) and their
messages never echo the offending value, so a rejected event cannot leak content.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer

SCHEMA_VERSION = "0.1"
NORMALIZER_VERSION = "0.1.0"

# --------------------------------------------------------------------------- ids
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}$"
CODE_PATTERN = r"^[a-z0-9][a-z0-9_.\-]{0,63}$"
VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._+\-=]{0,99}$"
HEX_PATTERN = r"^[0-9a-f]{16,128}$"
CURRENCY_PATTERN = r"^[A-Z]{3}$"

_CODE_RE = re.compile(CODE_PATTERN)

IdStr = Annotated[str, Field(pattern=ID_PATTERN, description="Opaque identifier; no whitespace")]
CodeStr = Annotated[
    str, Field(pattern=CODE_PATTERN, description="Controlled lowercase code, never prose")
]
VersionStr = Annotated[str, Field(pattern=VERSION_PATTERN)]
HexStr = Annotated[str, Field(pattern=HEX_PATTERN)]
CurrencyStr = Annotated[str, Field(pattern=CURRENCY_PATTERN, description="ISO 4217 code")]
UtcMs = Annotated[
    int,
    Field(ge=0, le=4_102_444_800_000, description="UTC epoch milliseconds"),
]
NonNegInt = Annotated[int, Field(ge=0)]
PosInt = Annotated[int, Field(ge=1)]
ShortText = Annotated[str, Field(max_length=500)]


# ------------------------------------------------------------------------ decimal
def to_decimal(value: Any) -> Any:
    """Coerce JSON-ish input into ``Decimal`` without silently accepting garbage.

    Strings and ints are the supported wire forms. Floats are accepted through their
    shortest repr so that a fixture author's ``0.5`` does not explode into binary noise,
    but fixtures and APIs should always send strings.
    """

    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("boolean is not a decimal amount")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("empty string is not a decimal amount")
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError("invalid decimal string") from exc
        if not result.is_finite():
            raise ValueError("decimal amount must be finite")
        return result
    raise ValueError(f"unsupported decimal input type {type(value).__name__}")


def decimal_to_str(value: Decimal) -> str:
    """Serialize without exponent notation (``1E-7`` -> ``0.0000001``)."""

    return format(value, "f")


DecimalStr = Annotated[
    Decimal,
    BeforeValidator(to_decimal),
    PlainSerializer(decimal_to_str, return_type=str, when_used="json"),
]


# ------------------------------------------------------------------- safe usage
def validate_safe_usage(value: Any, depth: int = 0) -> dict[str, Any]:
    """Allow only integer metrics keyed by controlled names, at most two levels deep.

    This is the last line of defence that keeps prompt/response content out of the
    telemetry: strings, floats and lists are rejected outright. Error messages never
    echo the offending value, only the key name and the value type.
    """

    if not isinstance(value, dict):
        raise ValueError("usage must be an object of integer metrics")
    if depth > 1:
        raise ValueError("usage nesting deeper than two levels is not allowed")
    out: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not _CODE_RE.fullmatch(key):
            raise ValueError("usage key is not an allowed metric name")
        if item is None:
            out[key] = None
        elif isinstance(item, bool):
            raise ValueError(f"usage field {key!r} must be an integer, not a boolean")
        elif isinstance(item, int):
            if item < 0:
                raise ValueError(f"usage field {key!r} must be non-negative")
            out[key] = item
        elif isinstance(item, dict):
            out[key] = validate_safe_usage(item, depth + 1)
        else:
            raise ValueError(
                f"usage field {key!r} must be an integer, null or nested object; "
                f"got {type(item).__name__}"
            )
    return out


def _safe_usage_validator(value: Any) -> dict[str, Any]:
    return validate_safe_usage(value)


SafeUsage = Annotated[dict[str, Any], BeforeValidator(_safe_usage_validator)]


def validate_int_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError("expected an object of integer counts")
    out: dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 200 or any(ch.isspace() for ch in key):
            raise ValueError("count key must be a whitespace-free path")
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError(f"count for {key!r} must be a non-negative integer")
        out[key] = item
    return out


IntMap = Annotated[dict[str, int], BeforeValidator(validate_int_map)]


# ------------------------------------------------------------------- base model
def canonical_dumps(data: Any) -> str:
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def sha256_hex(payload: str | bytes) -> str:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class SpecModel(BaseModel):
    """Base for all aiecon contracts: unknown fields are errors, not silently dropped."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def canonical_json(self, *, exclude: set[str] | None = None) -> str:
        return canonical_dumps(self.model_dump(mode="json", exclude=exclude))

    def content_hash(self, *, exclude: set[str] | None = None) -> str:
        return sha256_hex(self.canonical_json(exclude=exclude))


# ------------------------------------------------------------------------- enums
class DataKind(StrEnum):
    synthetic = "synthetic"
    live = "live"


class Provider(StrEnum):
    openai = "openai"
    anthropic = "anthropic"
    other = "other"


class ApiFamily(StrEnum):
    chat_completions = "chat_completions"
    responses = "responses"
    messages = "messages"
    unknown = "unknown"


class EvidenceClass(StrEnum):
    """PLAN.md §4.6 amount provenance classes. These never auto-upgrade."""

    A = "A"  # provider cost / settlement data at its real grain
    B = "B"  # provider-reported usage x verified price
    C = "C"  # measured runtime resource usage (reserved)
    D = "D"  # estimated usage or assumption-laden computation
    E = "E"  # heuristic allocation (not produced in v0.1)


class TimeWindow(SpecModel):
    """Half-open UTC window ``[start_ms, end_ms)``."""

    start_ms: UtcMs
    end_ms: UtcMs

    def contains(self, ts_ms: int) -> bool:
        return self.start_ms <= ts_ms < self.end_ms

    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms
