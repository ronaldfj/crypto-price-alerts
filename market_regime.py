"""market_regime.py — clasificación de régimen de mercado adaptativa por volatilidad
y estimación de persistencia ("stickiness") vía matriz de transición de Markov sobre
bloques de tiempo sin solapamiento.

Módulo puro: sin I/O, sin dependencia de alert.py. Consume DataFrames ya procesados
por alert.add_indicators() (necesita como mínimo 'Close'; 'atr'/'adx'/'ema20'/'ema50'
mejoran la clasificación pero son opcionales).

Por qué "adaptativo" y no umbrales fijos: en vez de comparar el retorno acumulado de
la ventana contra un % fijo, se lo normaliza por el movimiento esperado dado el ATR
(o la volatilidad intra-bloque) de esa misma ventana — un z-score de tendencia. El
mismo 5% de movimiento es una señal fuerte en un activo de baja volatilidad y ruido
en uno de alta volatilidad.

Por qué bloques sin solapamiento para la matriz de transición: reclasificar cada vela
con una ventana rolling (que se mueve 1 vela a la vez) hace que observaciones
consecutivas compartan ~95% de los mismos datos — la "persistencia" medida así es en
gran parte autocorrelación del propio método, no señal real. Los bloques se trocean
en tramos disjuntos desde el inicio del DataFrame y cada uno se clasifica usando solo
velas físicamente dentro de ese bloque (vol_method="intrablock"), sin fuga de
información entre bloques ni memoria EWM que cruce el límite.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

STATES = ["BEAR_DEEP", "BEAR", "SIDEWAYS_CHOP", "BULL", "BULL_DEEP"]
EPS = 1e-9


def classify_regime_state(
    df: pd.DataFrame,
    lookback: int = 20,
    z_trend: float = 0.8,
    z_deep: float = 1.8,
    min_adx_for_deep: float = 20.0,
    vol_method: str = "atr",
) -> Dict[str, Any]:
    """Clasifica el estado de régimen de las últimas `lookback` velas de `df`.

    vol_method="atr": usa la columna 'atr' ya suavizada (EWM) — barata y adecuada
    para una lectura informativa recalculada en cada vela.
    vol_method="intrablock": ignora 'atr' y calcula la volatilidad únicamente con
    los retornos dentro de la ventana pasada — usado por compute_block_regimes()
    para que los bloques no compartan memoria entre sí.
    """
    window = df.tail(lookback)
    if len(window) < max(2, lookback // 2):
        return {"state": "SIDEWAYS_CHOP", "z_score": 0.0, "net_return": 0.0, "avg_vol_pct": 0.0}

    closes = window["Close"].to_numpy(dtype=float)
    first_close = max(float(closes[0]), EPS)
    net_return = math.log(max(float(closes[-1]), EPS) / first_close)

    if vol_method == "atr" and "atr" in window.columns and window["atr"].notna().any():
        avg_vol_pct = float((window["atr"] / window["Close"].clip(lower=EPS)).mean())
    else:
        log_rets = np.diff(np.log(np.clip(closes, EPS, None)))
        avg_vol_pct = float(np.std(log_rets, ddof=1)) if len(log_rets) > 1 else 0.0

    expected_move = avg_vol_pct * math.sqrt(len(window))
    z_score = net_return / max(expected_move, EPS)

    last = window.iloc[-1]
    adx = float(last["adx"]) if "adx" in window.columns and pd.notna(last.get("adx")) else None
    ema_bull = ema_bear = None
    if {"ema20", "ema50"}.issubset(window.columns):
        ema20 = float(last["ema20"])
        ema50 = float(last["ema50"])
        ema_bull = ema20 >= ema50
        ema_bear = ema20 <= ema50

    strong_enough = adx is None or adx >= min_adx_for_deep

    if abs(z_score) < z_trend:
        state = "SIDEWAYS_CHOP"
    elif z_score >= z_deep and (ema_bull is not False) and strong_enough:
        state = "BULL_DEEP"
    elif z_score >= z_trend:
        state = "BULL"
    elif z_score <= -z_deep and (ema_bear is not False) and strong_enough:
        state = "BEAR_DEEP"
    else:
        state = "BEAR"

    return {
        "state": state,
        "z_score": round(z_score, 3),
        "net_return": round(net_return, 5),
        "avg_vol_pct": round(avg_vol_pct, 6),
    }


def compute_block_regimes(
    df: pd.DataFrame,
    block_size: int = 20,
    z_trend: float = 0.8,
    z_deep: float = 1.8,
    min_adx_for_deep: float = 20.0,
) -> List[Dict[str, Any]]:
    """Trocea `df` en bloques disjuntos de `block_size` velas desde el índice 0 y
    clasifica cada uno con vol_method="intrablock" (sin fuga de información entre
    bloques). El bloque final incompleto (si lo hay) se descarta."""
    n_blocks = len(df) // block_size
    blocks: List[Dict[str, Any]] = []
    for b in range(n_blocks):
        block_df = df.iloc[b * block_size : (b + 1) * block_size]
        result = classify_regime_state(
            block_df,
            lookback=block_size,
            z_trend=z_trend,
            z_deep=z_deep,
            min_adx_for_deep=min_adx_for_deep,
            vol_method="intrablock",
        )
        blocks.append(result)
    return blocks


def compute_transition_matrix(block_states: List[str]) -> Dict[str, Dict[str, float]]:
    """Matriz de transición de primer orden entre estados de bloques consecutivos,
    normalizada por fila (probabilidades)."""
    counts: Dict[str, Dict[str, int]] = {s: {s2: 0 for s2 in STATES} for s in STATES}
    for prev_state, next_state in zip(block_states, block_states[1:]):
        if prev_state in counts and next_state in counts[prev_state]:
            counts[prev_state][next_state] += 1

    matrix: Dict[str, Dict[str, float]] = {}
    for state, row in counts.items():
        total = sum(row.values())
        if total == 0:
            matrix[state] = {s2: 0.0 for s2 in STATES}
        else:
            matrix[state] = {s2: c / total for s2, c in row.items()}
    return matrix


def compute_stickiness_score(matrix: Dict[str, Dict[str, float]], current_state: str) -> float:
    """Probabilidad de auto-transición (diagonal de la matriz) para `current_state`.
    Es el "sticky score": qué tan probable es que el próximo bloque siga en el mismo
    estado, estimado sobre bloques limpios sin solapamiento."""
    row = matrix.get(current_state)
    if not row:
        return 0.0
    return round(row.get(current_state, 0.0), 4)


_BLOCK_CACHE: Dict[str, List[Dict[str, Any]]] = {}


def get_regime_context(
    df: pd.DataFrame,
    lookback: int = 20,
    block_size: int = 20,
    min_blocks: int = 30,
    z_trend: float = 0.8,
    z_deep: float = 1.8,
    min_adx_for_deep: float = 20.0,
    cache_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Orquesta la clasificación en vivo (lookback) + persistencia por bloques.

    Si `cache_key` se provee, reutiliza bloques ya clasificados en llamadas previas
    con la misma clave y solo clasifica los bloques nuevos que se completaron desde
    la última llamada — evita reclasificar millones de bloques redundantes durante
    un walk-forward del backtester. Sin `cache_key` (uso típico en vivo: una sola
    evaluación por símbolo por corrida), no cachea nada.
    """
    live = classify_regime_state(
        df, lookback=lookback, z_trend=z_trend, z_deep=z_deep,
        min_adx_for_deep=min_adx_for_deep, vol_method="atr",
    )

    n_available = len(df) // block_size
    if cache_key is not None:
        cached = _BLOCK_CACHE.setdefault(cache_key, [])
        if n_available > len(cached):
            for b in range(len(cached), n_available):
                block_df = df.iloc[b * block_size : (b + 1) * block_size]
                cached.append(
                    classify_regime_state(
                        block_df, lookback=block_size, z_trend=z_trend, z_deep=z_deep,
                        min_adx_for_deep=min_adx_for_deep, vol_method="intrablock",
                    )
                )
        blocks = cached
    else:
        blocks = compute_block_regimes(
            df, block_size=block_size, z_trend=z_trend, z_deep=z_deep,
            min_adx_for_deep=min_adx_for_deep,
        )

    block_states = [b["state"] for b in blocks]
    matrix = compute_transition_matrix(block_states)
    current_state = live["state"]
    stickiness_score = compute_stickiness_score(matrix, current_state)
    confidence = "OK" if len(blocks) >= min_blocks else "LOW"

    return {
        "regime_detail": current_state,
        "stickiness_score": stickiness_score,
        "regime_confidence": confidence,
        "n_blocks": len(blocks),
        "z_score": live["z_score"],
    }
