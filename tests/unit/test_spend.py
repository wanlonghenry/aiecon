"""V27: the spend fuse stops before sending, keeps unknown reservations, survives restarts."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from aiecon.collect.spend import SpendFuse, SpendStop, conservative_reserve
from aiecon.pricing import CatalogIndex, load_catalog

REPO = Path(__file__).resolve().parents[2]


def test_fuse_stops_before_the_call_that_would_exceed_budget(tmp_path: Path) -> None:
    fuse = SpendFuse(tmp_path / "spend.json", budget_usd=Decimal("0.010"), max_calls=10)
    fuse.reserve("c1", Decimal("0.006"))
    fuse.settle("c1", Decimal("0.004"))  # actual lower than reserved
    assert fuse.known == Decimal("0.004") and fuse.reserved == 0
    ok, reason = fuse.check(Decimal("0.005"))
    assert ok is True and reason == "ok"
    fuse.reserve("c2", Decimal("0.005"))
    with pytest.raises(SpendStop, match="budget_would_be_exceeded"):
        fuse.reserve("c3", Decimal("0.002"))  # 0.004 + 0.005 + 0.002 > 0.010
    assert fuse.summary()["stopped_reason"] == "budget_would_be_exceeded"


def test_unknown_cost_keeps_the_reservation_and_stops(tmp_path: Path) -> None:
    fuse = SpendFuse(tmp_path / "spend.json", budget_usd=Decimal("1"), max_calls=10)
    fuse.reserve("c1", Decimal("0.05"))
    fuse.settle("c1", None)  # usage missing / price unknown
    assert fuse.reserved == 0
    assert fuse.summary()["held_unknown_usd"] == "0.05"
    assert fuse.committed == Decimal("0.05")  # not released to zero
    ok, reason = fuse.check(Decimal("0.01"))
    assert ok is False and reason == "unknown_cost"


def test_restart_reuses_cumulative_state_and_cannot_raise_limits(tmp_path: Path) -> None:
    path = tmp_path / "spend.json"
    fuse = SpendFuse(path, budget_usd=Decimal("0.5"), max_calls=3)
    fuse.reserve("c1", Decimal("0.1"))
    fuse.settle("c1", Decimal("0.1"))
    again = SpendFuse(path, budget_usd=Decimal("5"), max_calls=100)
    assert again.known == Decimal("0.1")
    assert again.budget == Decimal("0.5") and again.state.max_calls == 3
    assert again.state.calls_sent == 1


def test_max_calls_is_enforced(tmp_path: Path) -> None:
    fuse = SpendFuse(tmp_path / "spend.json", budget_usd=Decimal("10"), max_calls=1)
    fuse.reserve("c1", Decimal("0.001"))
    with pytest.raises(SpendStop, match="max_calls_reached"):
        fuse.reserve("c2", Decimal("0.001"))


def test_conservative_reserve_uses_dearest_input_rate_and_full_output() -> None:
    index = CatalogIndex(load_catalog(REPO / "catalogs" / "live-demo.json"))
    ts = 1_790_467_200_000
    reserve = conservative_reserve(
        index,
        provider="anthropic",
        model_id="claude-haiku-4-5-20251001",
        ts_ms=ts,
        input_tokens_upper=5000,
        max_output_tokens=200,
    )
    # dearest input rate is the 1h cache write ($2/M); output $5/M
    assert (
        reserve
        == Decimal("5000") * Decimal("2") / 1_000_000 + Decimal("200") * Decimal("5") / 1_000_000
    )
    assert (
        conservative_reserve(
            index,
            provider="openai",
            model_id="not-in-catalog",
            ts_ms=ts,
            input_tokens_upper=1,
            max_output_tokens=1,
        )
        is None
    )


def test_clear_stop_lifts_the_latch_but_keeps_held_amounts_committed(tmp_path: Path) -> None:
    path = tmp_path / "spend.json"
    fuse = SpendFuse(path, budget_usd=Decimal("1"), max_calls=10)
    fuse.reserve("c1", Decimal("0.4"))
    fuse.settle("c1", None)  # unknown cost: reservation held, latch set
    assert fuse.check(Decimal("0.1")) == (False, "unknown_cost")
    with pytest.raises(SpendStop):
        fuse.reserve("c2", Decimal("0.1"))
    # a restart keeps the latch; only an explicit clear lifts it
    again = SpendFuse(path, budget_usd=Decimal("1"), max_calls=10)
    assert again.check(Decimal("0.1")) == (False, "unknown_cost")
    assert again.clear_stop() == "unknown_cost"
    assert again.committed == Decimal("0.4")  # the held amount still counts
    assert again.check(Decimal("0.5")) == (True, "ok")
    assert again.check(Decimal("0.7")) == (False, "budget_would_be_exceeded")
    assert SpendFuse(path, budget_usd=Decimal("1"), max_calls=10).state.stopped_reason is None
