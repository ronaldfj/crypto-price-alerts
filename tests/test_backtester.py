"""
tests/test_backtester.py — Unit tests for backtester.py

Covers: simulate_outcome_with_costs (LONG/SHORT slippage + fee), _build_result
(PnL arithmetic), score_bucket, compute_metrics, and breakdown_by.
"""

from __future__ import annotations

import math
import pandas as pd
import pytest
from backtester import (
    TradeOutcome,
    _build_result,
    breakdown_by,
    compute_metrics,
    score_bucket,
    simulate_outcome_with_costs,
)
from alert import SIDE_LONG, SIDE_SHORT


# ── Helpers ───────────────────────────────────────────────────────────────────

def _candles(highs, lows, closes=None):
    """Construye un DataFrame de velas mínimo."""
    n = len(highs)
    if closes is None:
        closes = [(h + l) / 2 for h, l in zip(highs, lows)]
    return pd.DataFrame({
        "Open":  [(h + l) / 2 for h, l in zip(highs, lows)],
        "High":  highs,
        "Low":   lows,
        "Close": closes,
        "Volume": [100.0] * n,
    })


def _make_trade(outcome, pnl_r_net, pnl_r_gross=None, side=SIDE_LONG, symbol="BTC",
                score=7.0, regime="BULL_STACK", fib="0.382-0.500", is_train=True):
    return TradeOutcome(
        symbol=symbol, side=side, candle_ts=1700000000,
        entry_price=30000.0, stop_loss=29000.0, tp1=32000.0, tp2=34000.0,
        score=score, score_bucket=score_bucket(score),
        adx=30.0, rsi=50.0, fib_zone=fib, regime=regime,
        rr_ratio=2.0, alert_profile="FULL", macro_ok=True, timing_ok=True,
        outcome=outcome,
        pnl_r_net=pnl_r_net,
        pnl_r_gross=pnl_r_net if pnl_r_gross is None else pnl_r_gross,
        is_train=is_train,
    )


# ── score_bucket ──────────────────────────────────────────────────────────────

class TestScoreBucket:
    @pytest.mark.parametrize("score,expected", [
        (4.9, "<5.0"),
        (5.0, "5.0-6.0"),
        (5.9, "5.0-6.0"),
        (6.0, "6.0-7.0"),
        (6.5, "6.0-7.0"),
        (7.0, "7.0-8.0"),
        (7.99, "7.0-8.0"),
        (8.0, "8.0-9.0"),
        (9.0, ">=9.0"),
        (10.0, ">=9.0"),
    ])
    def test_boundaries(self, score, expected):
        assert score_bucket(score) == expected


# ── _build_result (arithmetic) ────────────────────────────────────────────────

class TestBuildResult:
    def test_long_tp1_positive_pnl(self):
        entry = 30_000.0
        fill = 32_000.0   # +2000 gross
        risk = 1_000.0    # stop at 29000
        fee = 0.001
        result = _build_result("TP1_HIT", fill, 3, entry, fee, SIDE_LONG, risk)
        assert result["outcome"] == "TP1_HIT"
        assert result["pnl_r_gross"] == pytest.approx(2.0, abs=1e-3)
        # fees: entry * 0.001 * 2 = 60 → net_move = 2000 - 60 = 1940 → 1.94R
        assert result["pnl_r_net"] == pytest.approx(1.94, abs=1e-2)

    def test_long_sl_negative_pnl(self):
        entry = 30_000.0
        fill = 29_500.0   # -500 gross (SL fill after slippage)
        risk = 1_000.0
        fee = 0.001
        result = _build_result("SL_HIT", fill, 1, entry, fee, SIDE_LONG, risk)
        assert result["pnl_r_gross"] < 0
        assert result["pnl_r_net"] < result["pnl_r_gross"]  # fees make it worse

    def test_short_tp1_positive_pnl(self):
        entry = 30_000.0
        fill = 28_000.0   # SHORT: entry - fill = +2000
        risk = 1_000.0    # stop at 31000 → risk = 1000
        fee = 0.001
        result = _build_result("TP1_HIT", fill, 2, entry, fee, SIDE_SHORT, risk)
        assert result["pnl_r_gross"] == pytest.approx(2.0, abs=1e-3)

    def test_short_sl_negative_pnl(self):
        entry = 30_000.0
        fill = 30_600.0   # SHORT: entry - fill = -600
        risk = 1_000.0
        fee = 0.001
        result = _build_result("SL_HIT", fill, 1, entry, fee, SIDE_SHORT, risk)
        assert result["pnl_r_gross"] < 0

    def test_tp2_flags_both_hit(self):
        result = _build_result("TP2_HIT", 34000.0, 5, 30000.0, 0.001, SIDE_LONG, 1000.0, tp1_hit=True, tp2_hit=True)
        assert result["tp1_hit"] is True
        assert result["tp2_hit"] is True

    def test_fee_cost_positive(self):
        result = _build_result("TP1_HIT", 32000.0, 3, 30000.0, 0.001, SIDE_LONG, 1000.0)
        assert result["fee_cost_pct"] > 0


# ── simulate_outcome_with_costs ───────────────────────────────────────────────

class TestSimulateOutcomeWithCosts:
    def test_no_data_returns_no_data(self):
        result = simulate_outcome_with_costs(pd.DataFrame(), 30000, 29000, 32000, 34000, SIDE_LONG, 0.001, 0.0005)
        assert result["outcome"] == "NO_DATA"

    def test_invalid_risk_long(self):
        df = _candles([31000], [28000])
        # stop_loss > entry → invalid risk para LONG
        result = simulate_outcome_with_costs(df, 30000, 31000, 32000, 34000, SIDE_LONG, 0.001, 0.0005)
        assert result["outcome"] == "INVALID_RISK"

    def test_invalid_risk_short(self):
        df = _candles([31000], [28000])
        # stop_loss < entry → invalid risk para SHORT
        result = simulate_outcome_with_costs(df, 30000, 29000, 28000, 26000, SIDE_SHORT, 0.001, 0.0005)
        assert result["outcome"] == "INVALID_RISK"

    # LONG scenarios
    def test_long_sl_hit(self):
        df = _candles([30500], [28500])  # Low < stop_loss=29000
        result = simulate_outcome_with_costs(df, 30000, 29000, 32000, 34000, SIDE_LONG, 0.001, 0.0)
        assert result["outcome"] == "SL_HIT"
        assert result["bars_to_exit"] == 1

    def test_long_tp1_hit(self):
        df = _candles([32500], [30200])  # High >= tp1=32000, Low > SL=29000
        result = simulate_outcome_with_costs(df, 30000, 29000, 32000, 34000, SIDE_LONG, 0.001, 0.0)
        assert result["outcome"] == "TP1_HIT"

    def test_long_tp2_hit(self):
        df = _candles([35000], [30200])  # High >= tp2=34000
        result = simulate_outcome_with_costs(df, 30000, 29000, 32000, 34000, SIDE_LONG, 0.001, 0.0)
        assert result["outcome"] == "TP2_HIT"
        assert result["tp1_hit"] is True
        assert result["tp2_hit"] is True

    def test_long_sl_beats_tp_conservative(self):
        # Same candle: both SL and TP touched → SL first (conservative)
        df = _candles([32500], [28500])  # Low<=29000 AND High>=32000
        result = simulate_outcome_with_costs(df, 30000, 29000, 32000, 34000, SIDE_LONG, 0.001, 0.0)
        assert result["outcome"] == "SL_HIT"

    def test_long_expired(self):
        # Price never touches SL or TP
        df = _candles([31000, 31000], [29500, 29500])
        result = simulate_outcome_with_costs(df, 30000, 29000, 32000, 34000, SIDE_LONG, 0.001, 0.0)
        assert result["outcome"] == "EXPIRED"
        assert result["bars_to_exit"] == 2

    def test_long_slippage_worsens_sl_fill(self):
        df = _candles([30500], [28500])
        result_no_slip = simulate_outcome_with_costs(df, 30000, 29000, 32000, 34000, SIDE_LONG, 0.0, 0.0)
        result_with_slip = simulate_outcome_with_costs(df, 30000, 29000, 32000, 34000, SIDE_LONG, 0.0, 0.01)
        # With slippage, fill is worse (lower) → lower exit price → worse PnL
        assert result_with_slip["exit_price"] < result_no_slip["exit_price"]

    # SHORT scenarios
    def test_short_sl_hit(self):
        df = _candles([31500], [29500])  # High > stop_loss=31000
        result = simulate_outcome_with_costs(df, 30000, 31000, 28000, 26000, SIDE_SHORT, 0.001, 0.0)
        assert result["outcome"] == "SL_HIT"

    def test_short_tp1_hit(self):
        df = _candles([30500], [27500])  # Low <= tp1=28000, High < SL=31000
        result = simulate_outcome_with_costs(df, 30000, 31000, 28000, 26000, SIDE_SHORT, 0.001, 0.0)
        assert result["outcome"] == "TP1_HIT"

    def test_short_tp2_hit(self):
        df = _candles([30500], [25000])  # Low <= tp2=26000
        result = simulate_outcome_with_costs(df, 30000, 31000, 28000, 26000, SIDE_SHORT, 0.001, 0.0)
        assert result["outcome"] == "TP2_HIT"

    def test_short_slippage_worsens_tp_fill(self):
        df = _candles([30500], [27500])
        result_no_slip = simulate_outcome_with_costs(df, 30000, 31000, 28000, 26000, SIDE_SHORT, 0.0, 0.0)
        result_with_slip = simulate_outcome_with_costs(df, 30000, 31000, 28000, 26000, SIDE_SHORT, 0.0, 0.01)
        # SHORT TP fill with slippage = tp * (1+slip) → worse (higher) exit price
        assert result_with_slip["exit_price"] > result_no_slip["exit_price"]

    def test_multi_bar_exit(self):
        # Bars 1 and 2 don't hit; bar 3 hits TP1
        df = _candles(
            [30800, 31000, 32500],
            [29100, 29200, 30100],
        )
        result = simulate_outcome_with_costs(df, 30000, 29000, 32000, 34000, SIDE_LONG, 0.001, 0.0)
        assert result["outcome"] == "TP1_HIT"
        assert result["bars_to_exit"] == 3


# ── compute_metrics ───────────────────────────────────────────────────────────

class TestComputeMetrics:
    def test_empty_list_returns_zero_total(self):
        result = compute_metrics([])
        assert result == {"total": 0}

    def test_win_rate_calculation(self):
        trades = [
            _make_trade("TP1_HIT", 2.0),
            _make_trade("TP1_HIT", 1.5),
            _make_trade("SL_HIT", -1.0),
            _make_trade("SL_HIT", -1.0),
        ]
        result = compute_metrics(trades)
        assert result["win_rate_pct"] == pytest.approx(50.0)

    def test_expectancy_calculation(self):
        trades = [
            _make_trade("TP1_HIT", 2.0),
            _make_trade("SL_HIT", -1.0),
        ]
        result = compute_metrics(trades)
        # (2.0 + -1.0) / 2 = 0.5
        assert result["expectancy_r"] == pytest.approx(0.5, abs=1e-3)

    def test_profit_factor(self):
        trades = [
            _make_trade("TP1_HIT", 3.0),
            _make_trade("SL_HIT", -1.0),
            _make_trade("SL_HIT", -1.0),
        ]
        result = compute_metrics(trades)
        # gross_profit=3.0, gross_loss=2.0 → PF = 1.5
        assert result["profit_factor"] == pytest.approx(1.5, abs=1e-2)

    def test_profit_factor_infinite_when_no_losers(self):
        trades = [_make_trade("TP1_HIT", 2.0), _make_trade("TP2_HIT", 3.0)]
        result = compute_metrics(trades)
        assert result["profit_factor"] == float("inf")

    def test_max_drawdown(self):
        trades = [
            _make_trade("TP1_HIT", 2.0),   # cumul=2
            _make_trade("SL_HIT", -3.0),    # cumul=-1, peak=2, dd=3
            _make_trade("TP1_HIT", 1.0),    # cumul=0
        ]
        result = compute_metrics(trades)
        assert result["max_drawdown_r"] == pytest.approx(3.0, abs=1e-3)

    def test_expired_counted_separately(self):
        trades = [
            _make_trade("TP1_HIT", 2.0),
            _make_trade("EXPIRED", -0.1),
        ]
        result = compute_metrics(trades)
        assert result["expired"] == 1

    def test_use_gross_flag(self):
        trades = [_make_trade("TP1_HIT", pnl_r_net=1.5, pnl_r_gross=2.0)]
        net_result = compute_metrics(trades, use_net=True)
        gross_result = compute_metrics(trades, use_net=False)
        assert net_result["expectancy_r"] == pytest.approx(1.5, abs=1e-3)
        assert gross_result["expectancy_r"] == pytest.approx(2.0, abs=1e-3)

    def test_total_pnl(self):
        trades = [_make_trade("TP1_HIT", 2.0), _make_trade("SL_HIT", -1.0)]
        result = compute_metrics(trades)
        assert result["total_pnl_r"] == pytest.approx(1.0, abs=1e-3)


# ── breakdown_by ──────────────────────────────────────────────────────────────

class TestBreakdownBy:
    def test_breakdown_by_symbol(self):
        trades = [
            _make_trade("TP1_HIT", 2.0, symbol="BTC"),
            _make_trade("SL_HIT", -1.0, symbol="ETH"),
            _make_trade("TP1_HIT", 1.5, symbol="BTC"),
        ]
        result = breakdown_by(trades, "symbol")
        assert "BTC" in result
        assert "ETH" in result
        assert result["BTC"]["total"] == 2
        assert result["ETH"]["total"] == 1

    def test_breakdown_by_side(self):
        trades = [
            _make_trade("TP1_HIT", 2.0, side=SIDE_LONG),
            _make_trade("SL_HIT", -1.0, side=SIDE_SHORT),
        ]
        result = breakdown_by(trades, "side")
        assert SIDE_LONG in result
        assert SIDE_SHORT in result

    def test_breakdown_by_regime(self):
        trades = [
            _make_trade("TP1_HIT", 2.0, regime="BULL_STACK"),
            _make_trade("SL_HIT", -1.0, regime="BEAR_STACK"),
        ]
        result = breakdown_by(trades, "regime")
        assert "BULL_STACK" in result
        assert "BEAR_STACK" in result

    def test_empty_input(self):
        result = breakdown_by([], "symbol")
        assert result == {}
