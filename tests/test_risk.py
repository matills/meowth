"""Tests for deterministic RiskManager rules and kill switches."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from meowth.config import Settings
from meowth.models import OrderRequest, Quote, Side, TokenCandidate
from meowth.risk.manager import PortfolioState, RiskManager


def _valid_token(now: datetime) -> TokenCandidate:
    return TokenCandidate(
        mint="MintGood111",
        symbol="GOOD",
        liquidity_usd=120_000.0,
        mint_authority_revoked=True,
        freeze_authority_revoked=True,
        created_at=now - timedelta(hours=3),
    )


def _valid_order(amount_sol: float = 0.2, impact_pct: float = 0.01) -> OrderRequest:
    return OrderRequest(
        signal_id="sig-1",
        mint="MintGood111",
        side=Side.BUY,
        amount_sol=amount_sol,
        strategy_name="breakout",
        reason="volume spike",
        quote=Quote(
            input_mint="SOL",
            output_mint="MintGood111",
            in_amount=200_000_000,
            out_amount=1_000_000,
            price_sol=0.0002,
            price_impact_pct=impact_pct,
        ),
    )


def _healthy_portfolio(now: datetime) -> PortfolioState:
    return PortfolioState(
        total_equity_sol=10.0,
        peak_equity_sol=10.0,
        daily_pnl_sol=0.0,
        open_positions_count=1,
        total_exposure_sol=0.2,
        last_data_ts=now - timedelta(seconds=5),
    )


def test_approves_valid_order_and_caps_size(settings: Settings) -> None:
    now = datetime.now(UTC)
    rm = RiskManager(settings)
    # Requesting 1.0 SOL on 10 SOL equity -> capped at 2% = 0.2 SOL
    decision = rm.evaluate_entry(
        _valid_order(amount_sol=1.0),
        _valid_token(now),
        _healthy_portfolio(now),
        now=now,
    )
    assert decision.approved is True
    assert decision.approved_amount_sol == 0.2


def test_rejects_excessive_price_impact(settings: Settings) -> None:
    now = datetime.now(UTC)
    rm = RiskManager(settings)
    decision = rm.evaluate_entry(
        _valid_order(impact_pct=0.034),
        _valid_token(now),
        _healthy_portfolio(now),
        now=now,
    )
    assert decision.approved is False
    assert "RECHAZADA:" in decision.reason
    assert "3.4%" in decision.reason


def test_rejects_unrevoked_mint_authority(settings: Settings) -> None:
    now = datetime.now(UTC)
    token = _valid_token(now)
    token.mint_authority_revoked = False
    rm = RiskManager(settings)
    decision = rm.evaluate_entry(_valid_order(), token, _healthy_portfolio(now), now=now)
    assert decision.approved is False
    assert "mint authority" in decision.reason


def test_rejects_low_liquidity_and_young_token(settings: Settings) -> None:
    now = datetime.now(UTC)
    rm = RiskManager(settings)

    low_liq = _valid_token(now)
    low_liq.liquidity_usd = 20_000.0
    res_liq = rm.evaluate_entry(_valid_order(), low_liq, _healthy_portfolio(now), now=now)
    assert res_liq.approved is False

    young = _valid_token(now)
    young.created_at = now - timedelta(minutes=15)
    res_age = rm.evaluate_entry(_valid_order(), young, _healthy_portfolio(now), now=now)
    assert res_age.approved is False


def test_kill_switches_trigger_on_all_conditions(settings: Settings) -> None:
    now = datetime.now(UTC)
    rm = RiskManager(settings)

    paused = _healthy_portfolio(now)
    paused.manually_paused = True
    res_pause = rm.evaluate_entry(_valid_order(), _valid_token(now), paused, now=now)
    assert res_pause.kill_switch_triggered is True

    dd = _healthy_portfolio(now)
    dd.total_equity_sol = 7.9
    dd.peak_equity_sol = 10.0
    res_dd = rm.evaluate_entry(_valid_order(), _valid_token(now), dd, now=now)
    assert res_dd.kill_switch_triggered is True

    daily_loss = _healthy_portfolio(now)
    daily_loss.daily_pnl_sol = -0.6
    res_daily = rm.evaluate_entry(_valid_order(), _valid_token(now), daily_loss, now=now)
    assert res_daily.kill_switch_triggered is True

    stale = _healthy_portfolio(now)
    stale.last_data_ts = now - timedelta(seconds=120)
    res_stale = rm.evaluate_entry(_valid_order(), _valid_token(now), stale, now=now)
    assert res_stale.kill_switch_triggered is True
