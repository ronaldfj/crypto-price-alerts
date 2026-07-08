"""
tests/test_alert.py — Unit tests for alert.py

Covers: indicator math (RSI/ATR/ADX), fibonacci_context, helper utilities,
estimate_qty, URL builders, DB helpers (in-memory SQLite), and cooldown logic.
"""

from __future__ import annotations

import math
import sqlite3
import time
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
    evaluate_macro_confirmation,
    evaluate_setup_confirmation,
    fibonacci_context,
    fib_zone,
    get_meta,
    get_regime,
    init_db,
    is_circuit_broken,
    load_market_context,
    normalize_context,
    parse_allowed_sides,
    record_data_health_failure,
    record_data_health_success,
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


def _make_4h_df(seed=3, n=250):
    """DataFrame OHLCV con columna 'ts' (requerida por evaluate_setup_confirmation)
    y suficiente historia (>=210 velas tras el dropna de add_indicators) para
    ejercitar evaluate_macro_confirmation/evaluate_setup_confirmation end-to-end.
    seed=3 produce, de forma determinística, un RSI final ~72 (zona 69-74) que
    solo recibe el bonus de score si la banda RSI está ensanchada por régimen."""
    rng = np.random.default_rng(seed)
    base = 30000.0
    steps = rng.normal(30, 80, n)
    closes = base + np.cumsum(steps)
    highs = closes + rng.uniform(20, 100, n)
    lows = closes - rng.uniform(20, 100, n)
    opens = closes - rng.uniform(-50, 50, n)
    ts = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
    return pd.DataFrame({
        "Open": opens, "High": highs, "Low": lows, "Close": closes,
        "Volume": rng.uniform(100, 500, n), "ts": ts,
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

    def test_matches_hand_computed_reference_value(self):
        # 14 variaciones alternadas +2/-1 (ganancia=2, pérdida=1 por vela).
        # compute_rsi usa gain.ewm(alpha=1/14, adjust=False, min_periods=14): la recursión
        # arranca en avg_1=2 (primera observación no nula) y sigue avg_t=avg_{t-1}+(x_t-avg_{t-1})/14.
        # avg_gain final=1.330422, avg_loss final=0.334789 -> RSI=100*RS/(1+RS)=79.8951.
        close = _make_close_series([100, 102, 101, 103, 102, 104, 103, 105, 104, 106, 105, 107, 106, 108, 107])
        rsi = compute_rsi(close, period=14)
        assert rsi.iloc[-1] == pytest.approx(79.8951, abs=0.01)


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

    def test_matches_hand_computed_reference_value(self):
        # 14 velas con True Range alterno 5/3 (Close=100 constante; High-Low=5 en pares, 3
        # en impares). compute_atr aplica la misma recursión de Wilder arrancando en
        # avg_0=TR_0=5 -> ATR final = 4.330422.
        highs = [103.0 if t % 2 == 0 else 101.5 for t in range(14)]
        lows = [98.0 if t % 2 == 0 else 98.5 for t in range(14)]
        df = pd.DataFrame({"High": highs, "Low": lows, "Close": [100.0] * 14})
        atr = compute_atr(df, period=14)
        assert atr.iloc[-1] == pytest.approx(4.330422, abs=0.001)


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

    def test_perfect_uptrend_channel_gives_adx_100(self):
        # Canal alcista perfecto (High y Low suben +2/vela, ancho constante): Low nunca
        # retrocede -> minus_dm ≡ 0 -> minus_di ≡ 0. Con plus_di>0, dx=100 en cada vela
        # válida, y el EMA de una constante es esa constante -> ADX == 100.0 exacto.
        # 27 velas: dos máscaras min_periods=14 en cascada (ATR/±DM, luego DX->ADX).
        n = 27
        closes = [100.0 + 2 * t for t in range(n)]
        highs = [c + 1.0 for c in closes]
        lows = [c - 1.0 for c in closes]
        df = pd.DataFrame({"High": highs, "Low": lows, "Close": closes})
        adx, plus_di, minus_di = compute_adx(df, period=14)
        assert adx.iloc[-1] == pytest.approx(100.0, abs=1e-6)
        assert minus_di.iloc[-1] == pytest.approx(0.0, abs=1e-9)


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


# ── Régimen adaptativo (market_regime.py wiring) ────────────────────────────

class TestRegimeDetailWiring:
    def test_macro_and_setup_confirmation_expose_regime_fields(self, monkeypatch):
        """ENABLE_REGIME_DETAIL=true (default) agrega los 4 campos nuevos sin
        romper la evaluación real, tanto en 1D como en 4H."""
        monkeypatch.setattr(alert, "ENABLE_REGIME_DETAIL", True)
        monkeypatch.setattr(alert, "ENABLE_REGIME_ADAPTIVE_THRESHOLDS", False)
        df = _make_4h_df()

        macro = evaluate_macro_confirmation(df, "BTC", {}, side=SIDE_LONG)
        setup = evaluate_setup_confirmation(df, "BTC", "bitcoin", side=SIDE_LONG)

        for result in (macro, setup):
            assert result is not None
            for key in ("regime_detail", "stickiness_score", "regime_confidence", "n_blocks"):
                assert key in result

    def test_adaptive_thresholds_off_by_default_is_behaviorally_inert(self, monkeypatch):
        """El test de compatibilidad más importante: con
        ENABLE_REGIME_ADAPTIVE_THRESHOLDS=False (default), activar o desactivar
        ENABLE_REGIME_DETAIL no debe cambiar score/setup_ok/reasons en absoluto
        — la clasificación informativa nunca debe filtrarse a la lógica de gating."""
        monkeypatch.setattr(alert, "ENABLE_REGIME_ADAPTIVE_THRESHOLDS", False)
        df = _make_4h_df()

        monkeypatch.setattr(alert, "ENABLE_REGIME_DETAIL", True)
        with_detail = evaluate_setup_confirmation(df, "BTC", "bitcoin", side=SIDE_LONG)

        monkeypatch.setattr(alert, "ENABLE_REGIME_DETAIL", False)
        without_detail = evaluate_setup_confirmation(df, "BTC", "bitcoin", side=SIDE_LONG)

        ignore_keys = {"regime_detail", "stickiness_score", "regime_confidence", "n_blocks"}
        for key in with_detail:
            if key in ignore_keys:
                continue
            assert with_detail[key] == without_detail[key], f"campo '{key}' difirió con el flag apagado/prendido"

    def test_regime_loosening_widens_rsi_band_when_active(self, monkeypatch):
        """Con ENABLE_REGIME_ADAPTIVE_THRESHOLDS=True y un contexto de régimen
        BULL_DEEP de alta stickiness, un RSI que cae en la zona 69-74 (fuera de
        la banda base, dentro de la banda ensanchada +5) debe sumar el bonus de
        score que con el flag apagado no recibiría."""
        df = _make_4h_df(seed=3)  # RSI final ~72, determinístico

        monkeypatch.setattr(alert, "ENABLE_REGIME_ADAPTIVE_THRESHOLDS", False)
        baseline = evaluate_setup_confirmation(df, "BTC", "bitcoin", side=SIDE_LONG)
        assert 69 < baseline["rsi"] < 74  # confirma que el fixture cae en la zona de interés

        monkeypatch.setattr(
            alert,
            "_regime_context_or_fallback",
            lambda work, symbol, timeframe: {
                "regime_detail": "BULL_DEEP", "stickiness_score": 0.9,
                "regime_confidence": "OK", "n_blocks": 40,
            },
        )
        monkeypatch.setattr(alert, "ENABLE_REGIME_ADAPTIVE_THRESHOLDS", True)
        widened = evaluate_setup_confirmation(df, "BTC", "bitcoin", side=SIDE_LONG)

        assert widened["score"] > baseline["score"]
        assert any("ensanchadas" in reason for reason in widened["reasons"])

    def test_regime_loosening_stays_off_with_low_confidence(self, monkeypatch):
        """Aunque el régimen sea BULL_DEEP y sticky, una lectura de baja
        confianza (pocos bloques) no debe activar el aflojamiento."""
        df = _make_4h_df(seed=3)
        monkeypatch.setattr(
            alert,
            "_regime_context_or_fallback",
            lambda work, symbol, timeframe: {
                "regime_detail": "BULL_DEEP", "stickiness_score": 0.9,
                "regime_confidence": "LOW", "n_blocks": 2,
            },
        )
        monkeypatch.setattr(alert, "ENABLE_REGIME_ADAPTIVE_THRESHOLDS", True)
        result = evaluate_setup_confirmation(df, "BTC", "bitcoin", side=SIDE_LONG)
        assert not any("ensanchadas" in reason for reason in result["reasons"])


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


# ── invalidate_old_alerts / resolve_price_outcome_since_alert ─────────────────

class TestInvalidateOldAlerts:
    """
    invalidate_old_alerts() solía marcar INVALIDATED sin nunca comprobar qué
    hizo el precio, dejando outcome_rr=NULL en el ~90% de las alertas y
    haciendo imposible medir si la invalidación temprana ahorra pérdidas o
    corta ganadores a tiempo. Estos tests cubren el fix: reconstruir el
    outcome real (o mark-to-market) antes de cerrar la fila.
    """

    def _insert_active_alert(self, conn, side=SIDE_LONG, entry=30000.0, stop=29000.0,
                              tp1=30600.0, tp2=31200.0, candle_ts=None):
        import time
        now = int(time.time())
        candle_ts = candle_ts if candle_ts is not None else now - 3600 * 6
        key = f"BTC|{side}|4h|BULL_STACK|50-54|0.500-0.618|1234"
        conn.execute(
            """INSERT INTO alerts
               (symbol, cg_id, side, timeframe, setup_key, setup_hash, regime,
                rsi_bucket, fib_zone, price_bucket, candle_ts, entry_price,
                stop_loss, take_profit, tp1, tp2, rr_ratio, score, adx, rsi, atr,
                reasons_json, status, sent_at, validation_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "BTC", "bitcoin", side, "4h", key, build_setup_hash(key), "BULL_STACK",
                "50-54", "0.500-0.618", "1234",
                candle_ts, entry, stop, tp2, tp1, tp2, 2.0, 8.0, 30.0, 50.0, 300.0,
                "[]", alert.ACTIVE, candle_ts, alert.VALIDATION_PENDING,
            ),
        )
        conn.commit()
        return conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 1").fetchone()

    def _candidate_breaking_thesis(self, side=SIDE_LONG):
        # macro_ok=False es motivo suficiente para que invalidate_old_alerts
        # dispare "Confirmación macro perdida" sin importar el resto de campos.
        return {
            "symbol": "BTC", "side": side, "timeframe": "4h",
            "regime": "BULL_STACK" if side == SIDE_LONG else "BEAR_STACK",
            "macro_ok": False, "timing_ok": True,
            "entry_price": 30000.0, "rsi": 50.0, "atr": 300.0,
        }

    def _fake_candles(self, rows):
        """rows: list of (ts_epoch, open, high, low, close)"""
        return pd.DataFrame({
            "ts": pd.to_datetime([r[0] for r in rows], unit="s", utc=True),
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
        })

    def test_invalidation_records_sl_hit_as_closed(self):
        """Si el precio ya tocó el stop antes de invalidar, debe cerrarse como
        CLOSED/SL_HIT con outcome_rr real (~ -1R), no como invalidación ciega."""
        conn = _in_memory_db()
        row = self._insert_active_alert(conn, side=SIDE_LONG, entry=30000.0, stop=29000.0)
        candle_ts = int(row["candle_ts"])

        candles = self._fake_candles([
            (candle_ts + 3600, 30000, 30100, 28900, 28950),  # low <= stop
        ])
        with patch.object(alert, "fetch_klines", return_value=candles):
            alert.invalidate_old_alerts(conn, self._candidate_breaking_thesis(SIDE_LONG))

        updated = conn.execute("SELECT * FROM alerts WHERE id = ?", (row["id"],)).fetchone()
        assert updated["status"] == alert.CLOSED
        assert updated["validation_result"] == "SL_HIT"
        assert updated["outcome_rr"] == pytest.approx(-1.0, abs=0.01)
        assert updated["validation_status"] == alert.VALIDATION_RESOLVED

    def test_invalidation_without_sl_tp_marks_to_market(self):
        """Si nunca tocó SL/TP, debe seguir INVALIDATED pero con outcome_rr
        a mercado (mark-to-market) en vez de NULL."""
        conn = _in_memory_db()
        row = self._insert_active_alert(conn, side=SIDE_LONG, entry=30000.0, stop=29000.0,
                                         tp1=30600.0, tp2=31200.0)
        candle_ts = int(row["candle_ts"])

        # Precio se mueve a favor pero sin tocar TP1 (30600) ni el stop (29000)
        candles = self._fake_candles([
            (candle_ts + 3600, 30000, 30400, 29900, 30300),
        ])
        with patch.object(alert, "fetch_klines", return_value=candles):
            alert.invalidate_old_alerts(conn, self._candidate_breaking_thesis(SIDE_LONG))

        updated = conn.execute("SELECT * FROM alerts WHERE id = ?", (row["id"],)).fetchone()
        assert updated["status"] == alert.INVALIDATED
        assert updated["validation_result"] == "INVALIDATED_EARLY"
        assert updated["outcome_rr"] is not None
        expected_rr = (30300.0 - 30000.0) / (30000.0 - 29000.0)
        assert updated["outcome_rr"] == pytest.approx(expected_rr, abs=0.01)
        assert updated["validation_status"] == alert.VALIDATION_RESOLVED

    def test_invalidation_falls_back_when_price_fetch_fails(self):
        """Si no se puede reconstruir el precio (API caída), no debe romper la
        invalidación: cae al comportamiento anterior sin outcome_rr."""
        conn = _in_memory_db()
        row = self._insert_active_alert(conn, side=SIDE_LONG)

        with patch.object(alert, "fetch_klines", return_value=None):
            alert.invalidate_old_alerts(conn, self._candidate_breaking_thesis(SIDE_LONG))

        updated = conn.execute("SELECT * FROM alerts WHERE id = ?", (row["id"],)).fetchone()
        assert updated["status"] == alert.INVALIDATED
        assert updated["outcome_rr"] is None
        assert updated["validation_status"] == alert.VALIDATION_PENDING


# ── is_circuit_broken / should_send_alert circuit breaker gate ───────────────

class TestCircuitBreaker:
    """Circuit breaker estilo freqtrade Protections/StoplossGuard: bloquea
    symbol+side tras N invalidaciones discrecionales recientes, cortando el
    ciclo de re-alertar un side que sigue "mejorando" (is_material_improvement)
    para luego invalidarse de nuevo en mercado choppy."""

    def _candidate(self, symbol="BTC", side=SIDE_SHORT, score=7.0):
        key = f"{symbol}|{side}|4h|BEAR_STACK|50-54|0.500-0.618|1234"
        return {
            "symbol": symbol, "side": side, "timeframe": "4h",
            "regime": "BEAR_STACK", "rsi_bucket": "50-54",
            "fib_zone": "0.500-0.618", "price_bucket": "1234",
            "setup_key": key, "setup_hash": build_setup_hash(key),
            "score": score, "entry_price": 30000.0,
            "rr_ratio": 2.0, "adx": 30.0,
        }

    def _insert_invalidated_alert(self, conn, symbol, side, invalidated_at):
        now = int(time.time())
        key = f"{symbol}|{side}|4h|BEAR_STACK|50-54|0.500-0.618|1234"
        conn.execute(
            """INSERT INTO alerts
               (symbol, cg_id, side, timeframe, setup_key, setup_hash, regime,
                rsi_bucket, fib_zone, price_bucket, candle_ts, entry_price,
                stop_loss, take_profit, rr_ratio, score, adx, rsi, atr,
                reasons_json, status, sent_at, invalidated_at, invalidation_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, symbol.lower(), side, "4h", key, build_setup_hash(key), "BEAR_STACK",
             "50-54", "0.500-0.618", "1234",
             now - 7200, 30000.0, 31000.0, 28000.0, 2.0, 7.0, 30.0, 50.0, 300.0,
             "[]", alert.INVALIDATED, now - 7200, invalidated_at, "Confirmación macro perdida"),
        )
        conn.commit()

    def test_no_invalidations_allows_send(self):
        conn = _in_memory_db()
        broken, count = is_circuit_broken(conn, "BTC", SIDE_SHORT, int(time.time()))
        assert broken is False
        assert count == 0

    def test_threshold_invalidations_within_window_blocks(self):
        conn = _in_memory_db()
        now_ts = int(time.time())
        for _ in range(alert.CIRCUIT_BREAKER_MAX_INVALIDATIONS):
            self._insert_invalidated_alert(conn, "BTC", SIDE_SHORT, now_ts - 3600)

        ok, _, reason = should_send_alert(conn, self._candidate("BTC", SIDE_SHORT))
        assert ok is False
        assert "Circuit breaker" in reason

    def test_below_threshold_does_not_block(self):
        conn = _in_memory_db()
        now_ts = int(time.time())
        for _ in range(alert.CIRCUIT_BREAKER_MAX_INVALIDATIONS - 1):
            self._insert_invalidated_alert(conn, "BTC", SIDE_SHORT, now_ts - 3600)

        broken, count = is_circuit_broken(conn, "BTC", SIDE_SHORT, now_ts)
        assert broken is False
        assert count == alert.CIRCUIT_BREAKER_MAX_INVALIDATIONS - 1

    def test_only_applies_to_matching_side(self):
        """3 invalidaciones en SHORT no deben bloquear LONG del mismo symbol —
        la granularidad es symbol+side, no todo el activo."""
        conn = _in_memory_db()
        now_ts = int(time.time())
        for _ in range(alert.CIRCUIT_BREAKER_MAX_INVALIDATIONS):
            self._insert_invalidated_alert(conn, "BTC", SIDE_SHORT, now_ts - 3600)

        broken, _ = is_circuit_broken(conn, "BTC", SIDE_LONG, now_ts)
        assert broken is False

    def test_invalidations_outside_window_do_not_block(self):
        conn = _in_memory_db()
        now_ts = int(time.time())
        old_ts = now_ts - (alert.CIRCUIT_BREAKER_WINDOW_HOURS + 1) * 3600
        for _ in range(alert.CIRCUIT_BREAKER_MAX_INVALIDATIONS):
            self._insert_invalidated_alert(conn, "BTC", SIDE_SHORT, old_ts)

        broken, count = is_circuit_broken(conn, "BTC", SIDE_SHORT, now_ts)
        assert broken is False
        assert count == 0

    def test_closed_status_does_not_count_toward_breaker(self):
        """Un CLOSED (SL/TP real) no es una invalidación discrecional — no debe
        contar para el circuit breaker aunque haya varios."""
        conn = _in_memory_db()
        now_ts = int(time.time())
        key = "BTC|SHORT|4h|BEAR_STACK|50-54|0.500-0.618|1234"
        for _ in range(alert.CIRCUIT_BREAKER_MAX_INVALIDATIONS):
            conn.execute(
                """INSERT INTO alerts
                   (symbol, cg_id, side, timeframe, setup_key, setup_hash, regime,
                    rsi_bucket, fib_zone, price_bucket, candle_ts, entry_price,
                    stop_loss, take_profit, rr_ratio, score, adx, rsi, atr,
                    reasons_json, status, sent_at, invalidated_at, invalidation_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("BTC", "bitcoin", SIDE_SHORT, "4h", key, build_setup_hash(key), "BEAR_STACK",
                 "50-54", "0.500-0.618", "1234",
                 now_ts - 7200, 30000.0, 31000.0, 28000.0, 2.0, 7.0, 30.0, 50.0, 300.0,
                 "[]", alert.CLOSED, now_ts - 7200, now_ts - 3600, "Stop técnico vulnerado"),
            )
        conn.commit()

        broken, count = is_circuit_broken(conn, "BTC", SIDE_SHORT, now_ts)
        assert broken is False
        assert count == 0


# ── record_data_health_failure / record_data_health_success ──────────────────

class TestDataHealth:
    """Data-health check estilo freqtrade Pairlist: detecta y avisa cuando un
    símbolo lleva N corridas seguidas sin datos de ningún proveedor (caso TON:
    antes de esto, el fallo quedaba silencioso salvo en logs)."""

    def test_first_failure_increments_and_does_not_alert(self):
        conn = _in_memory_db()
        now_ts = int(time.time())
        consecutive, should_alert = record_data_health_failure(conn, "TON", now_ts)
        assert consecutive == 1
        assert should_alert is False

    def test_crossing_threshold_alerts_once(self):
        conn = _in_memory_db()
        now_ts = int(time.time())
        for i in range(alert.DATA_HEALTH_ALERT_THRESHOLD - 1):
            consecutive, should_alert = record_data_health_failure(conn, "TON", now_ts + i)
            assert should_alert is False

        consecutive, should_alert = record_data_health_failure(
            conn, "TON", now_ts + alert.DATA_HEALTH_ALERT_THRESHOLD
        )
        assert consecutive == alert.DATA_HEALTH_ALERT_THRESHOLD
        assert should_alert is True

    def test_does_not_realert_immediately_after_crossing(self):
        conn = _in_memory_db()
        now_ts = int(time.time())
        for i in range(alert.DATA_HEALTH_ALERT_THRESHOLD):
            record_data_health_failure(conn, "TON", now_ts + i)

        # Una falla más, inmediatamente después de haber avisado: no debe repetir.
        _, should_alert = record_data_health_failure(
            conn, "TON", now_ts + alert.DATA_HEALTH_ALERT_THRESHOLD + 1
        )
        assert should_alert is False

    def test_realerts_after_seven_days(self):
        conn = _in_memory_db()
        now_ts = int(time.time())
        for i in range(alert.DATA_HEALTH_ALERT_THRESHOLD):
            record_data_health_failure(conn, "TON", now_ts + i)

        later = now_ts + alert.DATA_HEALTH_ALERT_THRESHOLD + 8 * 24 * 3600
        _, should_alert = record_data_health_failure(conn, "TON", later)
        assert should_alert is True

    def test_success_resets_consecutive_failures(self):
        conn = _in_memory_db()
        now_ts = int(time.time())
        record_data_health_failure(conn, "TON", now_ts)
        record_data_health_failure(conn, "TON", now_ts + 1)

        record_data_health_success(conn, "TON", now_ts + 2)

        consecutive, should_alert = record_data_health_failure(conn, "TON", now_ts + 3)
        assert consecutive == 1
        assert should_alert is False
