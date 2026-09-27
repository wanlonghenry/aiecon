"""Workspace resolution, thresholds and environment access (PLAN.md section 10.4).

This module is the only place that reads the clock or the environment. Secrets are never
returned as values; callers may only ask whether a variable is present.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

DEFAULT_WORKSPACE = Path(".aiecon/live")
WORKSPACE_ENV = "AIECON_WORKSPACE"

MODEL_KEY_ENVS: tuple[str, ...] = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")
ADMIN_KEY_ENVS: dict[str, str] = {
    "openai": "OPENAI_ADMIN_API_KEY",
    "anthropic": "ANTHROPIC_ADMIN_API_KEY",
}
LIVE_MODEL_ENVS: dict[str, str] = {
    "openai": "AIECON_OPENAI_MODEL",
    "anthropic": "AIECON_ANTHROPIC_MODEL",
}
FINGERPRINT_KEY_ENV = "AIECON_FINGERPRINT_KEY"


@dataclass(frozen=True)
class Settings:
    """Tunable thresholds. Tolerances are display classification only (section 7.6)."""

    tolerance_abs_usd: Decimal = Decimal("0.01")
    tolerance_pct: Decimal = Decimal("1")
    sync_lookback_days: int = 3
    http_timeout_s: float = 30.0
    http_max_retries: int = 3


DEFAULT_SETTINGS = Settings()


def now_ms() -> int:
    return int(time.time() * 1000)


def resolve_workspace(cli_value: Path | None, env: Mapping[str, str] | None = None) -> Path:
    if cli_value is not None:
        return cli_value
    env = os.environ if env is None else env
    value = env.get(WORKSPACE_ENV)
    return Path(value) if value else DEFAULT_WORKSPACE


def env_present(name: str, env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get(name))


def env_value_non_secret(name: str, env: Mapping[str, str] | None = None) -> str | None:
    """Read a non-secret setting (model ids, workspace). Never use for keys."""

    if name.endswith("_KEY") or "SECRET" in name or "TOKEN" in name:
        raise ValueError(f"{name} looks like a secret; only presence checks are allowed")
    env = os.environ if env is None else env
    value = env.get(name)
    return value or None


def debug_enabled(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get("AIECON_DEBUG", "") not in ("", "0", "false")
