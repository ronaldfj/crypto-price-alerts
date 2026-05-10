"""
tests/test_alert.py — Unit tests for alert.py

Covers: indicator math (RSI/ATR/ADX), fibonacci_context, helper utilities,
estimate_qty, URL builders, DB helpers (in-memory SQLite), and cooldown logic.
"""

from __future__ import annotations

import math
import sqlite3
import pandas as pd
import numpy as np
import pytest
from unittest.mock import MagicMock, patch

import alert
from alert import (
    SIDE_LONG, SIDE_SHORT,
    binance_spot_url,
    build_setup_hash,
    build_setup_key,
    compute_atr,
    compute_adx,
    compute_rsi,
    estimate_qty,
    fibonacci_context,
    fib_zone,
    get_meta,
    get_regime,
    init_db,
    load_market_context,
    normalize_context,
    parse_allowed_sides,
    rsi_bucket,
    set_meta,
    should_send_alert,
    timeframe_to_seconds,
    tradingview_url,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_close_series(values):
    return pd.Series(values, dtype=float)


def _make_ohlc_df(n=50, trend="up"):
    """Genera un DataFrame OHLCV simple con un trend configurable."""
    rng = np.random.default_rng(42)
    base = 30000.0
    if trend == "up":
        closes = base + np.cumsum(rng.uniform(0, 100, n))
    elif trend == "down":
        closes = base - np.cumsum(rng.uniform(0, 100, n))
    else:
        closes = base + rng.uniform(-200, 200, n)

    highs = closes + rng.uniform(50, 300, n)
    lows = closes - rng.uniform(50, 300, n)
    opens = closes - rng.uniform(-100, 100, n)

    return pd.DataFrame({
        "Open": opens, "High": highs, "Low": lows,
        "Close": closes, "Volume": rng.uniform(100, 500, n),
    })


def _in_memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    init_db(conn)
    return conn


# ── compute_rsi ───────────────────────────────────────────────────────────────

class TestComputeRsi:
    def test_returns_series_same_length(self):
        close = _make_close_series(range(100, 150))
        rsi = compute_rsi(close, period=14)
        assert len(rsi) == len(close)

    def test_rsi_in_0_100_range(self):
        close = _make_close_series(np.linspace(100, 200, 100))
        rsi = compute_rsi(close).dropna()
        assert (rsi >= 0).all() and (rsi <= 100).all()

    def test_constant_price_rsi_near_zero(self):
        close = _make_close_series([100.0] * 50)
        rsi = compute_rsi(close).dropna()
        # Constant prices: no gains, avg_loss=0 → RS=0/eps≈0 → RSI near 0
        assert rsi.iloc[-1] == pytest.approx(0.0, abs=5.0)

    def test_rising_prices_rsi_above_50(self):
        close = _make_close_series(np.linspace(100, 200, 60))
        rsi = compute_rsi(close).dropna()
        assert rsi.iloc[-1] > 50

    def test_falling_prices_rsi_below_50(self):
        close = _make_close_series(np.linspace(200, 100, 60))
        rsi = compute_rsi(close).dropna()
        assert rsi.iloc[-1] < 50


# ── compute_atr ───────────────────────────────────────────────────────────────

class TestComputeAtr:
    def test_returns_series(self):
        df = _make_ohlc_df(50)
        atr = compute_atr(df)
        assert isinstance(atr, pd.Series)
        assert len(atr) == len(df)

    def test_atr_non_negative(self):
        df = _make_ohlc_df(50)
        atr = compute_atr(df).dropna()
        assert (atr >= 0).all()

    def test_atr_reflects_volatility(self):
        # High-volatility series should have higher ATR than low-volatility
        rng = np.random.default_rng(0)
        n = 50
        low_vol = pd.DataFrame({"High": 100 + rng.uniform(0, 1, n), "Low": 100 - rng.uniform(0, 1, n), "Close": [100.0]*n})
        high_vol = pd.DataFrame({"High": 100 + rng.uniform(0, 20, n), "Low": 100 - rng.uniform(0, 20, n), "Close": [100.0]*n})
        assert compute_atr(high_vol).dropna().mean() > compute_atr(low_vol).dropna().mean()


# ── compute_adx ───────────────────────────────────────────────────────────────

class TestComputeAdx:
    def test_returns_three_series(self):
        df = _make_ohlc_df(60)
        adx, plus_di, minus_di = compute_adx(df)
        assert all(isinstance(s, pd.Series) for s in [adx, plus_di, minus_di])

    def test_adx_non_negative(self):
        df = _make_ohlc_df(60)
        adx, _, _ = compute_adx(df)
        assert (adx.dropna() >= 0).all()

    def test_uptrend_plus_di_dominates(self):
        df = _make_ohlc_df(80, trend="up")
        _, plus_di, minus_di = compute_adx(df)
        # In a strong uptrend, +DI should be >= -DI on average
        assert plus_di.dropna().mean() >= minus_di.dropna().mean()


# ── fibonacci_context ─────────────────────────────────────────────────────────

class TestFibonacciContext:
    def test_returns_dict_with_required_keys(self):
        df = _make_ohlc_df(60)
        result = fibonacci_context(df)
        for key in ("swing_low", "swing_high", "retracement", "pullback_from_low",
                    "fib_382", "fib_500", "fib_618", "amplitude"):
            assert key in result

    def test_retracement_between_0_and_1(self):
        df = _make_ohlc_df(60)
        result = fibonacci_context(df)
        assert 0.0 <= result["retracement"] <= 1.0

    def test_amplitude_positive(self):
        df = _make_ohlc_df(60)
        result = fibonacci_context(df)
        assert result["amplitude"] > 0

    def test_fib_levels_ordered(self):
        df = _make_ohlc_df(60)
        result = fibonacci_context(df)
        # 38.2% > 50% > 61.8% when measured from high downward
        assert result["fib_382"] > result["fib_500"] > result["fib_618"]

    def test_swing_bounds(self):
        df = _make_ohlc_df(60)
        result = fibonacci_context(df)
        assert result["swing_low"] <= result["swing_high"]


# ── fib_zone ──────────────────────────────────────────────────────────────────

class TestFibZone:
    @pytest.mark.parametrize("value,expected", [
        (0.382, "0.382-0.500"),
        (0.499, "0.382-0.500"),
        (0.500, "0.500-0.618"),
        (0.617, "0.500-0.618"),
        (0.618, "0.618-0.786"),
        (0.786, "0.618-0.786"),
        (0.100, "OUTSIDE"),
        (0.900, "OUTSIDE"),
    ])
    def test_zone_boundaries(self, value, expected):
        assert fib_zone(value) == expected


# ── rsi_bucket ────────────────────────────────────────────────────────────────

class TestRsiBucket:
    @pytest.mark.parametrize("rsi_val,expected", [
        (0.0, "00-04"),
        (5.0, "05-09"),
        (50.0, "50-54"),
        (70.0, "70-74"),
        (99.0, "95-99"),
    ])
    def test_buckets(self, rsi_val, expected):
        assert rsi_bucket(rsi_val) == expected

    def test_clamps_to_valid_range(self):
        assert rsi_bucket(-5.0) == "00-04"
        assert rsi_bucket(105.0) == "95-99"


# ── get_regime ────────────────────────────────────────────────────────────────

class TestGetRegime:
    def _row(self, ema20, ema50, ema200):
        return pd.Series({"ema20": ema20, "ema50": ema50, "ema200": ema200})

    def test_bull_stack(self):
        assert get_regime(self._row(200, 150, 100)) == "BULL_STACK"

    def test_bear_stack(self):
        assert get_regime(self._row(100, 150, 200)) == "BEAR_STACK"

    def test_mixed(self):
        assert get_regime(self._row(150, 200, 100)) == "MIXED"


# ── estimate_qty ──────────────────────────────────────────────────────────────

class TestEstimateQty:
    def test_high_price(self):
        qty = estimate_qty(30000.0, 10.0)
        assert "." in qty  # should be a decimal
        assert float(qty) == pytest.approx(10.0 / 30000.0, rel=0.01)

    def test_low_price(self):
        qty = estimate_qty(0.001, 10.0)
        # price < 0.01 → integer precision
        assert "." not in qty or qty.split(".")[1] == ""

    def test_zero_price_returns_zero(self):
        assert estimate_qty(0.0) == "0"

    def test_negative_price_returns_zero(self):
        assert estimate_qty(-100.0) == "0"

    def test_precision_tiers(self):
        # price >= 1000 → 6 decimals
        assert len(estimate_qty(2000.0, 10.0).split(".")[-1]) == 6
        # price in [1, 1000) → 4 decimals
        assert len(estimate_qty(100.0, 10.0).split(".")[-1]) == 4


# ── URL builders ──────────────────────────────────────────────────────────────

class TestUrlBuilders:
    def test_binance_spot_url_known_symbol(self):
        url = binance_spot_url("BTC")
        assert url is not None
        assert "binance.com" in url
        assert "BTC" in url

    def test_binance_spot_url_unknown_returns_none(self):
        assert binance_spot_url("FAKE") is None

    def test_tradingview_url_known_symbol(self):
        url = tradingview_url("BTC")
        assert "tradingview.com" in url
        assert "BTCUSDT" in url

    def test_tradingview_url_unknown_symbol_fallback(self):
        url = tradingview_url("FAKE")
        assert "tradingview.com" in url


# ── timeframe_to_seconds ──────────────────────────────────────────────────────

class TestTimeframeToSeconds:
    @pytest.mark.parametrize("tf,expected", [
        ("15m", 900),
        ("15min", 900),
        ("1h", 3600),
        ("4h", 14400),
        ("1d", 86400),
        ("1w", 604800),
    ])
    def test_known_timeframes(self, tf, expected):
        assert timeframe_to_seconds(tf) == expected

    def test_unknown_defaults_to_4h(self):
        assert timeframe_to_seconds("99x") == 14400


# ── normalize_context ─────────────────────────────────────────────────────────

class TestNormalizeContext:
    def test_global_merged(self):
        ctx = {"GLOBAL": {"macro_bias": "BULLISH"}}
        result = normalize_context(ctx, "BTC")
        assert result["macro_bias"] == "BULLISH"

    def test_asset_overrides_global(self):
        ctx = {
            "GLOBAL": {"macro_bias": "BULLISH"},
            "BTC": {"macro_bias": "BEARISH"},
        }
        result = normalize_context(ctx, "BTC")
        assert result["macro_bias"] == "BEARISH"

    def test_missing_symbol_uses_global(self):
        ctx = {"GLOBAL": {"caution_level": "HIGH"}}
        result = normalize_context(ctx, "UNKNOWN")
        assert result["caution_level"] == "HIGH"

    def test_empty_context(self):
        result = normalize_context({}, "BTC")
        assert result == {}


# ── parse_allowed_sides ───────────────────────────────────────────────────────

class TestParseAllowedSides:
    def test_both_sides_from_list(self):
        ctx = {"allowed_sides": ["LONG", "SHORT"]}
        result = parse_allowed_sides(ctx)
        assert "LONG" in result
        assert "SHORT" in result

    def test_string_input(self):
        ctx = {"allowed_sides": "LONG,SHORT"}
        result = parse_allowed_sides(ctx)
        assert "LONG" in result

    def test_invalid_side_ignored(self):
        ctx = {"allowed_sides": ["LONG", "INVALID", "SHORT"]}
        result = parse_allowed_sides(ctx)
        assert "INVALID" not in result

    def test_defaults_to_long_when_empty(self):
        # DEFAULT_ALLOWED_SIDES env default is "LONG"
        result = parse_allowed_sides({})
        assert "LONG" in result


# ── load_market_context ───────────────────────────────────────────────────────

class TestLoadMarketContext:
    def test_loads_valid_json(self, tmp_path):
        ctx_file = tmp_path / "ctx.json"
        ctx_file.write_text('{"GLOBAL": {"macro_bias": "BULLISH"}}')
        result = load_market_context(str(ctx_file))
        assert result["GLOBAL"]["macro_bias"] == "BULLISH"

    def test_missing_file_returns_empty(self):
        result = load_market_context("/nonexistent/path.json")
        assert result == {}

    def test_invalid_json_returns_empty(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json")
        result = load_market_context(str(bad))
        assert result == {}


# ── SQLite helpers (in-memory) ────────────────────────────────────────────────

class TestDbHelpers:
    def test_init_db_creates_tables(self):
        conn = _in_memory_db()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "alerts" in tables
        assert "meta" in tables

    def test_get_set_meta(self):
        conn = _in_memory_db()
        set_meta(conn, "test_key", "test_value")
        assert get_meta(conn, "test_key") == "test_value"

    def test_get_meta_default(self):
        conn = _in_memory_db()
        assert get_meta(conn, "nonexistent", "default_val") == "default_val"

    def test_set_meta_overwrite(self):
        conn = _in_memory_db()
        set_meta(conn, "key", "v1")
        set_meta(conn, "key", "v2")
        assert get_meta(conn, "key") == "v2"

    def test_alerts_table_has_required_columns(self):
        conn = _in_memory_db()
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()}
        for col in ("symbol", "side", "entry_price", "stop_loss", "score", "rr_ratio",
                    "tp1", "tp2", "validation_status", "status"):
            assert col in cols, f"Missing column: {col}"


# ── build_setup_key / build_setup_hash ────────────────────────────────────────

class TestSetupKeyHash:
    def _candidate(self, **overrides):
        base = {
            "symbol": "BTC", "side": "LONG", "timeframe": "4h",
            "regime": "BULL_STACK", "rsi_bucket": "50-54",
            "fib_zone": "0.500-0.618", "price_bucket": "1234",
        }
        base.update(overrides)
        return base

    def test_build_setup_key_format(self):
        key = build_setup_key(self._candidate())
        parts = key.split("|")
        assert len(parts) == 7
        assert parts[0] == "BTC"

    def test_different_symbols_different_hash(self):
        h1 = build_setup_hash(build_setup_key(self._candidate(symbol="BTC")))
        h2 = build_setup_hash(build_setup_key(self._candidate(symbol="ETH")))
        assert h1 != h2

    def test_same_inputs_same_hash(self):
        c = self._candidate()
        assert build_setup_hash(build_setup_key(c)) == build_setup_hash(build_setup_key(c))

    def test_hash_is_hex_string(self):
        key = build_setup_key(self._candidate())
        h = build_setup_hash(key)
        assert len(h) == 64
        int(h, 16)  # should not raise


# ── should_send_alert ─────────────────────────────────────────────────────────

class TestShouldSendAlert:
    def _candidate(self, symbol="BTC", side=SIDE_LONG, score=7.0):
        key = f"{symbol}|{side}|4h|BULL_STACK|50-54|0.500-0.618|1234"
        return {
            "symbol": symbol, "side": side, "timeframe": "4h",
            "regime": "BULL_STACK", "rsi_bucket": "50-54",
            "fib_zone": "0.500-0.618", "price_bucket": "1234",
            "setup_key": key, "setup_hash": build_setup_hash(key),
            "score": score, "entry_price": 30000.0,
            "rr_ratio": 2.0, "adx": 30.0,
        }

    def test_no_prior_alert_allows_send(self):
        conn = _in_memory_db()
        ok, _, reason = should_send_alert(conn, self._candidate())
        assert ok is True

    def test_same_hash_within_cooldown_blocks(self):
        conn = _in_memory_db()
        import time
        now = int(time.time())
        cand = self._candidate()
        # Insert a recent alert with same setup_hash
        conn.execute(
            """INSERT INTO alerts
               (symbol, cg_id, side, timeframe, setup_key, setup_hash, regime,
                rsi_bucket, fib_zone, price_bucket, candle_ts, entry_price,
                stop_loss, take_profit, rr_ratio, score, adx, rsi, atr,
                reasons_json, status, sent_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cand["symbol"], "bitcoin", cand["side"], cand["timeframe"],
             cand["setup_key"], cand["setup_hash"], "BULL_STACK",
             "50-54", "0.500-0.618", "1234",
             now - 100, 30000.0, 29000.0, 32000.0, 2.0, 7.0, 30.0, 50.0, 300.0,
             "[]", "ACTIVE", now - 100),
        )
        conn.commit()
        ok, _, reason = should_send_alert(conn, cand)
        assert ok is False
        assert reason != ""
