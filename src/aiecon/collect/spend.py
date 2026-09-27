"""Local spend fuse for the live workload (PLAN.md section 11.3).

The fuse is a budget control based on the known price model. It reserves a conservative
amount *before* each paid call, settles it with the computed cost afterwards, and keeps
the reservation (and stops) whenever a cost cannot be computed. State is persisted after
every change so a restarted run continues from the same cumulative figures. It is not a
substitute for provider-side spend limits.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from aiecon.pricing.catalog import CatalogIndex
from aiecon.spec.pricing import Resource

MILLION = Decimal(1_000_000)


class SpendStop(Exception):
    """Raised when the fuse refuses to dispatch another paid call."""


@dataclass
class SpendState:
    budget_usd: str
    max_calls: int
    known_spend_usd: str = "0"
    reserved_usd: str = "0"
    calls_sent: int = 0
    calls_completed: int = 0
    stopped_reason: str | None = None
    reservations: dict[str, str] = field(default_factory=dict)
    held: dict[str, str] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)


class SpendFuse:
    def __init__(self, state_path: Path, *, budget_usd: Decimal, max_calls: int):
        self.state_path = Path(state_path)
        if self.state_path.exists():
            raw = json.loads(self.state_path.read_text("utf-8"))
            self.state = SpendState(**raw)
            # a restart may lower, never raise, the limits recorded on disk
            self.state.budget_usd = format(min(Decimal(self.state.budget_usd), budget_usd), "f")
            self.state.max_calls = min(self.state.max_calls, max_calls)
        else:
            self.state = SpendState(budget_usd=format(budget_usd, "f"), max_calls=max_calls)
        self._save()

    # ----------------------------------------------------------- properties
    @property
    def budget(self) -> Decimal:
        return Decimal(self.state.budget_usd)

    @property
    def known(self) -> Decimal:
        return Decimal(self.state.known_spend_usd)

    @property
    def reserved(self) -> Decimal:
        return Decimal(self.state.reserved_usd)

    @property
    def committed(self) -> Decimal:
        """Known spend plus everything reserved or held for unknown-cost calls."""

        return (
            self.known
            + self.reserved
            + sum((Decimal(v) for v in self.state.held.values()), start=Decimal(0))
        )

    @property
    def remaining(self) -> Decimal:
        return self.budget - self.committed

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(asdict(self.state), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    # ------------------------------------------------------------- control
    def check(self, reserve_usd: Decimal) -> tuple[bool, str]:
        if self.state.stopped_reason:
            return False, self.state.stopped_reason
        if self.state.calls_sent >= self.state.max_calls:
            return False, "max_calls_reached"
        if self.committed + reserve_usd > self.budget:
            return False, "budget_would_be_exceeded"
        return True, "ok"

    def reserve(self, call_key: str, reserve_usd: Decimal) -> None:
        ok, reason = self.check(reserve_usd)
        if not ok:
            self.state.stopped_reason = self.state.stopped_reason or reason
            self._save()
            raise SpendStop(reason)
        self.state.reservations[call_key] = format(reserve_usd, "f")
        self.state.reserved_usd = format(self.reserved + reserve_usd, "f")
        self.state.calls_sent += 1
        self._save()

    def settle(self, call_key: str, actual_usd: Decimal | None) -> None:
        """Replace a reservation with the computed cost, or hold it when cost is unknown."""

        reserved = Decimal(self.state.reservations.pop(call_key, "0"))
        self.state.reserved_usd = format(self.reserved - reserved, "f")
        if actual_usd is None:
            self.state.held[call_key] = format(reserved, "f")
            self.state.stopped_reason = "unknown_cost"
        else:
            self.state.known_spend_usd = format(self.known + actual_usd, "f")
            self.state.calls_completed += 1
        self.state.history.append(
            {
                "call_key": call_key,
                "reserved_usd": format(reserved, "f"),
                "actual_usd": None if actual_usd is None else format(actual_usd, "f"),
            }
        )
        self._save()

    def stop(self, reason: str) -> None:
        self.state.stopped_reason = reason
        self._save()

    def summary(self) -> dict[str, Any]:
        return {
            "budget_usd": format(self.budget, "f"),
            "known_spend_usd": format(self.known, "f"),
            "reserved_usd": format(self.reserved, "f"),
            "held_unknown_usd": format(
                sum((Decimal(v) for v in self.state.held.values()), start=Decimal(0)), "f"
            ),
            "remaining_usd": format(self.remaining, "f"),
            "calls_sent": self.state.calls_sent,
            "calls_completed": self.state.calls_completed,
            "max_calls": self.state.max_calls,
            "stopped_reason": self.state.stopped_reason,
        }


def conservative_reserve(
    index: CatalogIndex,
    *,
    provider: str,
    model_id: str,
    ts_ms: int,
    input_tokens_upper: int,
    max_output_tokens: int,
) -> Decimal | None:
    """Worst-case cost of one call: all input at the dearest input rate, full max output.

    Returns ``None`` when any required price is missing so the caller stops instead of
    treating an unknown amount as zero.
    """

    input_rates = []
    for resource in (
        Resource.input_uncached,
        Resource.input_cache_write,
        Resource.input_cache_write_5m,
        Resource.input_cache_write_1h,
    ):
        record = index.price_at(provider, model_id, resource, ts_ms)
        if record is not None:
            input_rates.append(record.unit_price / Decimal(record.unit_quantity))
    output = index.price_at(provider, model_id, Resource.output, ts_ms)
    if not input_rates or output is None:
        return None
    dearest_input = max(input_rates)
    return Decimal(input_tokens_upper) * dearest_input + Decimal(max_output_tokens) * (
        output.unit_price / Decimal(output.unit_quantity)
    )
