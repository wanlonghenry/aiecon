"""Allowlist construction, HMAC prefix fingerprints and log safety (PLAN.md §5.3–§5.4).

Everything that touches provider payloads goes through :func:`build_safe_usage`: it copies
only allowlisted integer metrics out of a usage object and counts unknown field *names*.
Nothing here ever serializes a whole response, prompt, error body or metadata blob.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aiecon.spec.common import CODE_PATTERN, ID_PATTERN
from aiecon.spec.envelope import sanitized_validation_summary

_ID_RE = re.compile(ID_PATTERN)
_CODE_RE = re.compile(CODE_PATTERN)

# Field names that must never be persisted or logged, whatever object they appear on.
FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "messages",
        "prompt",
        "input",
        "output",
        "choices",
        "content",
        "text",
        "tool_calls",
        "tools",
        "functions",
        "arguments",
        "headers",
        "api_key",
        "authorization",
        "metadata",
        "error",
        "exception",
        "traceback",
        "response",
        "request",
        "system",
        "instructions",
    }
)

# Types we allow through build_safe_usage. Nested allowlists are Mappings; leaves are True.
UsageAllowlist = Mapping[str, Any]


# ------------------------------------------------------------------ fingerprints
def fingerprint_key_id(key: bytes) -> str:
    """Public identifier of a fingerprint key that reveals nothing about the key."""

    return "fpk_" + hashlib.sha256(key).hexdigest()[:16]


def load_fingerprint_key(
    *, env_var: str = "AIECON_FINGERPRINT_KEY", path: Path | None = None
) -> bytes | None:
    """Read the HMAC key from a restricted file or the environment. Never logged."""

    if path is not None and path.exists():
        data = path.read_bytes().strip()
        return data or None
    value = os.environ.get(env_var)
    if value:
        return value.encode("utf-8")
    return None


def prefix_fingerprint(key: bytes, parts: Iterable[str | bytes]) -> str:
    """HMAC-SHA256 over an ordered, length-prefixed sequence of prefix parts.

    The application decides which contiguous prefix (tools, system, leading messages,
    rendering-relevant config identifiers) it hashes. Whitespace and order are preserved
    exactly; no semantic normalization happens here. The result reduces content exposure
    but is not an anonymization guarantee.
    """

    mac = hmac.new(key, digestmod=hashlib.sha256)
    for part in parts:
        data = part.encode("utf-8") if isinstance(part, str) else bytes(part)
        mac.update(len(data).to_bytes(8, "big"))
        mac.update(data)
    return mac.hexdigest()


# ---------------------------------------------------------------- safe objects
def _as_items(obj: Any) -> list[tuple[Any, Any]] | None:
    if isinstance(obj, Mapping):
        return list(obj.items())
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            dumped = dump()
        except Exception:  # pragma: no cover - defensive
            return None
        if isinstance(dumped, Mapping):
            return list(dumped.items())
    if hasattr(obj, "__dict__") and not isinstance(obj, type):
        return list(vars(obj).items())
    return None


def _drift_key(path: tuple[str, ...], suffix: str = "") -> str:
    safe_parts = [p if _CODE_RE.fullmatch(p) else "unnamed" for p in path]
    return ".".join(safe_parts) + suffix


def build_safe_usage(raw: Any, allowed: UsageAllowlist) -> tuple[dict[str, Any], dict[str, int]]:
    """Copy allowlisted non-negative integer metrics; count everything else by *name*.

    Returns ``(safe_usage, schema_drift)``. Values that are not integers (strings, floats
    with fractions, lists, objects where an integer was expected) are dropped and counted
    under their field path with a ``:type`` suffix. Field names that are not valid metric
    codes are reported as ``unnamed`` so that arbitrary provider text cannot leak through
    a key.
    """

    safe: dict[str, Any] = {}
    drift: dict[str, int] = {}
    if raw is None:
        return safe, drift
    _walk(raw, allowed, (), safe, drift)
    return safe, drift


def _bump(drift: dict[str, int], key: str) -> None:
    drift[key] = drift.get(key, 0) + 1


def _walk(
    raw: Any,
    allowed: UsageAllowlist,
    path: tuple[str, ...],
    safe: dict[str, Any],
    drift: dict[str, int],
) -> None:
    items = _as_items(raw)
    if items is None:
        _bump(drift, _drift_key(path or ("usage",), ":not_an_object"))
        return
    for key, value in items:
        if not isinstance(key, str):
            _bump(drift, _drift_key(path + ("unnamed",)))
            continue
        spec = allowed.get(key)
        if spec is None:
            if value is None:
                continue  # absent-but-null unknown fields are not drift
            _bump(drift, _drift_key(path + (key,)))
            continue
        if isinstance(spec, Mapping):
            if value is None:
                safe[key] = None
                continue
            sub: dict[str, Any] = {}
            _walk(value, spec, path + (key,), sub, drift)
            safe[key] = sub
            continue
        if value is None:
            safe[key] = None
        elif isinstance(value, bool):
            _bump(drift, _drift_key(path + (key,), ":bool"))
        elif isinstance(value, int) and value >= 0:
            safe[key] = value
        elif isinstance(value, float) and value.is_integer() and value >= 0:
            safe[key] = int(value)
        else:
            _bump(drift, _drift_key(path + (key,), f":{type(value).__name__}"))


# -------------------------------------------------------------------- logging
def safe_log_value(value: Any) -> Any:
    """Return ``value`` only if it is a number, bool, None or an opaque ID/code string."""

    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str) and (_ID_RE.fullmatch(value) or _CODE_RE.fullmatch(value)):
        return value
    return f"<redacted:{type(value).__name__}>"


def safe_log_fields(**fields: Any) -> dict[str, Any]:
    """Build a log record whose values cannot carry content."""

    return {
        key: safe_log_value(value) for key, value in fields.items() if key not in FORBIDDEN_FIELDS
    }


def safe_error_summary(exc: BaseException) -> str:
    """Describe an exception without its message text."""

    if isinstance(exc, ValidationError):
        return f"validation_error({sanitized_validation_summary(exc)})"
    return type(exc).__name__


_STATUS_CLASSES: dict[int, str] = {
    400: "invalid_request",
    401: "auth",
    403: "auth",
    404: "not_found",
    408: "timeout",
    413: "invalid_request",
    422: "invalid_request",
    429: "rate_limit",
    529: "overloaded",
}

_NAME_HINTS: tuple[tuple[str, str], ...] = (
    ("ratelimit", "rate_limit"),
    ("authentication", "auth"),
    ("permission", "auth"),
    ("contextwindow", "context_length"),
    ("contextlength", "context_length"),
    ("contentpolicy", "content_filter"),
    ("contentfilter", "content_filter"),
    ("notfound", "not_found"),
    ("badrequest", "invalid_request"),
    ("invalidrequest", "invalid_request"),
    ("unprocessable", "invalid_request"),
    ("timeout", "timeout"),
    ("connection", "connection"),
    ("cancel", "cancelled"),
    ("overloaded", "overloaded"),
    ("serviceunavailable", "server_error"),
    ("internalserver", "server_error"),
    ("apierror", "server_error"),
)


def classify_exception(exc: BaseException) -> str:
    """Map an exception to a controlled error class using its type and status code only."""

    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in _STATUS_CLASSES:
            return _STATUS_CLASSES[status]
        if 500 <= status < 600:
            return "server_error"
    name = type(exc).__name__.lower()
    for needle, code in _NAME_HINTS:
        if needle in name:
            return code
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionError):
        return "connection"
    return "unknown"
