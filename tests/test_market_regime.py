"""
tests/test_market_regime.py — Unit tests for market_regime.py

Cubre: clasificación adaptativa por volatilidad (el mismo % de movimiento debe
clasificar distinto según ruido), bloques disjuntos para la matriz de transición,
stickiness, y la invariante de estabilidad point-in-time de la que depende la
caché incremental usada por backtester.py.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from market_regime import (
    _BLOCK_CACHE,
    classify_regime_state,
    compute_block_regimes,
    compute_stickiness_score,
    compute_transition_matrix,
    get_regime_context,
)


@pytest.fixture(autouse=True)
def _clear_block_cache():
    _BLOCK_CACHE.clear()
    yield
    _BLOCK_CACHE.clear()


def _bull_block_df(n_blocks: int, block_size: int = 20, trailing: int = 0) -> pd.DataFrame:
    """n_blocks bloques limpios de tendencia alcista de baja volatilidad + un
    bloque final incompleto de `trailing` velas (debe descartarse)."""
    closes = []
    for b in range(n_blocks):
        closes.extend(np.linspace(100 + b * 10, 105 + b * 10, block_size))
    if trailing:
        last = closes[-1]
        closes.extend(np.linspace(last, last + 1, trailing))
    return pd.DataFrame({"Close": closes})


# ── classify_regime_state ───────────────────────────────────────────────────

class TestClassifyRegimeState:
    def test_same_net_return_low_vol_vs_high_vol_classify_differently(self):
        """La prueba central de 'adaptativo, no un % fijo': mismo retorno neto
        (100 -> 105 sobre 20 velas), pero el camino ruidoso tiene mucha más
        volatilidad intra-ventana -> debe clasificar distinto que el camino suave."""
        rng = np.random.default_rng(7)
        base = np.linspace(100, 105, 20)

        low_vol = pd.DataFrame({"Close": base})
        noisy_close = base.copy()
        noisy_close[1:-1] += rng.normal(0, 3.0, 18)  # endpoints fijos -> mismo net_return
        noisy = pd.DataFrame({"Close": noisy_close})

        r_low = classify_regime_state(low_vol, lookback=20, vol_method="intrablock")
        r_noisy = classify_regime_state(noisy, lookback=20, vol_method="intrablock")

        assert r_low["net_return"] == pytest.approx(r_noisy["net_return"], abs=1e-6)
        assert r_low["state"] in ("BULL", "BULL_DEEP")
        assert r_noisy["state"] == "SIDEWAYS_CHOP"
        assert abs(r_low["z_score"]) > abs(r_noisy["z_score"])

    def test_flat_noisy_series_is_sideways_chop(self):
        rng = np.random.default_rng(1)
        closes = 100 + rng.normal(0, 1.0, 20)
        df = pd.DataFrame({"Close": closes})
        result = classify_regime_state(df, lookback=20, vol_method="intrablock")
        assert result["state"] == "SIDEWAYS_CHOP"

    def test_strong_downtrend_low_vol_is_bear_deep(self):
        closes = np.linspace(105, 100, 20)
        df = pd.DataFrame({"Close": closes})
        result = classify_regime_state(df, lookback=20, vol_method="intrablock")
        assert result["state"] == "BEAR_DEEP"

    def test_too_short_window_defaults_to_sideways_chop(self):
        df = pd.DataFrame({"Close": [100.0, 101.0]})
        result = classify_regime_state(df, lookback=20, vol_method="intrablock")
        assert result["state"] == "SIDEWAYS_CHOP"

    def test_atr_vol_method_uses_atr_column_when_present(self):
        closes = np.linspace(100, 105, 20)
        df = pd.DataFrame({"Close": closes, "atr": np.full(20, 5.0)})
        result = classify_regime_state(df, lookback=20, vol_method="atr")
        # ATR alto relativo al precio (5 sobre ~100-105) domina el movimiento neto
        # (+5%) -> normaliza a un z-score bajo, mismo régimen que "ruido".
        assert result["state"] == "SIDEWAYS_CHOP"
        assert result["avg_vol_pct"] == pytest.approx(5.0 / closes.mean(), rel=0.05)


# ── compute_block_regimes ────────────────────────────────────────────────────

class TestComputeBlockRegimes:
    def test_blocks_are_disjoint_and_trailing_partial_dropped(self):
        df = _bull_block_df(n_blocks=3, block_size=20, trailing=5)
        blocks = compute_block_regimes(df, block_size=20)
        assert len(blocks) == 3  # el bloque parcial de 5 velas se descarta

    def test_block_states_reflect_trend(self):
        df = _bull_block_df(n_blocks=3, block_size=20)
        blocks = compute_block_regimes(df, block_size=20)
        assert all(b["state"] in ("BULL", "BULL_DEEP") for b in blocks)


# ── compute_transition_matrix / compute_stickiness_score ────────────────────

class TestTransitionMatrixAndStickiness:
    def test_known_sequence_transition_probabilities(self):
        states = ["BULL", "BULL", "BULL", "SIDEWAYS_CHOP"]
        matrix = compute_transition_matrix(states)
        # BULL -> BULL dos veces, BULL -> SIDEWAYS_CHOP una vez, de 3 transiciones desde BULL.
        assert matrix["BULL"]["BULL"] == pytest.approx(2 / 3)
        assert matrix["BULL"]["SIDEWAYS_CHOP"] == pytest.approx(1 / 3)

    def test_stickiness_is_self_transition_probability(self):
        states = ["BULL", "BULL", "BULL", "SIDEWAYS_CHOP"]
        matrix = compute_transition_matrix(states)
        assert compute_stickiness_score(matrix, "BULL") == pytest.approx(2 / 3, abs=1e-4)

    def test_stickiness_zero_for_unseen_state(self):
        matrix = compute_transition_matrix(["BULL", "BULL"])
        assert compute_stickiness_score(matrix, "BEAR_DEEP") == 0.0


# ── get_regime_context: caché incremental y estabilidad point-in-time ───────

class TestGetRegimeContextCache:
    def test_cache_extends_without_reclassifying_existing_blocks(self):
        """Invariante de la que depende el backtester: al crecer el DataFrame,
        los bloques ya clasificados no cambian — solo se agregan los nuevos."""
        df = _bull_block_df(n_blocks=4, block_size=20)
        df60 = df.iloc[:60].reset_index(drop=True)
        get_regime_context(df60, lookback=20, block_size=20, min_blocks=2, cache_key="TEST:4h")
        blocks_before = list(_BLOCK_CACHE["TEST:4h"])
        assert len(blocks_before) == 3

        df80 = df.iloc[:80].reset_index(drop=True)
        get_regime_context(df80, lookback=20, block_size=20, min_blocks=2, cache_key="TEST:4h")
        blocks_after = _BLOCK_CACHE["TEST:4h"]

        assert len(blocks_after) == 4
        assert blocks_after[:3] == blocks_before

    def test_no_cache_key_does_not_persist_across_calls(self):
        df = _bull_block_df(n_blocks=2, block_size=20)
        get_regime_context(df, lookback=20, block_size=20, min_blocks=2, cache_key=None)
        assert _BLOCK_CACHE == {}

    def test_confidence_low_below_min_blocks(self):
        df = _bull_block_df(n_blocks=2, block_size=20)
        ctx = get_regime_context(df, lookback=20, block_size=20, min_blocks=30, cache_key="LOW:4h")
        assert ctx["regime_confidence"] == "LOW"

    def test_confidence_ok_at_or_above_min_blocks(self):
        df = _bull_block_df(n_blocks=3, block_size=20)
        ctx = get_regime_context(df, lookback=20, block_size=20, min_blocks=3, cache_key="OK:4h")
        assert ctx["regime_confidence"] == "OK"

    def test_context_returns_expected_keys(self):
        df = _bull_block_df(n_blocks=3, block_size=20)
        ctx = get_regime_context(df, lookback=20, block_size=20, min_blocks=2, cache_key="KEYS:4h")
        assert set(ctx.keys()) == {
            "regime_detail", "stickiness_score", "regime_confidence", "n_blocks", "z_score",
        }
        assert ctx["regime_detail"] in ("BEAR_DEEP", "BEAR", "SIDEWAYS_CHOP", "BULL", "BULL_DEEP")
