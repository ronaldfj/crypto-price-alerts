"""
sentinel_shared.py — Utilidades y caché compartidas entre las páginas de
Crypto Sentinel Inspector (vista detalle en inspector.py + resumen en
pages/1_Resumen.py). Sin UI propia: solo fetchers cacheados y el pipeline
de evaluación (idéntico al que usa alert.py en producción).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).parent))

import streamlit as st

from alert import (
    CRYPTO_IDS,
    TRADING_TIMEFRAME,
    ENTRY_TIMEFRAME,
    MARKET_CONTEXT_FILE,
    SIDE_LONG,
    SIDE_SHORT,
    evaluate_macro_confirmation,
    evaluate_setup_confirmation,
    evaluate_timing_confirmation,
    build_candidate,
    apply_execution_quality_gate,
    load_market_context,
    normalize_context,
    fetch_btc_dominance,
    parse_allowed_sides,
)
from data_source import fetch_klines, fetch_latest_price, SYMBOL_TO_BASE

SYMBOLS = sorted(CRYPTO_IDS.values())
SYMBOL_TO_CGID = {sym: cg for cg, sym in CRYPTO_IDS.items()}

# ── CSS + helpers de presentación compartidos ──────────────────────────────
# Ambas páginas (inspector.py y pages/1_Resumen.py) son scripts Streamlit
# independientes: sin esto, cada una necesitaría reinyectar el mismo CSS y
# reconstruir las mismas tarjetas a mano, y terminarían divergiendo visualmente.

CUSTOM_CSS = """
<style>
.card {
    background: #f8f9fa;
    border-radius: 10px;
    padding: 1.2rem 1.4rem;
    margin-bottom: 0.8rem;
}
.card-alert  { border-left: 5px solid #28a745; background: #f0fff4; }
.card-block  { border-left: 5px solid #dc3545; background: #fff5f5; }
.card-warn   { border-left: 5px solid #fd7e14; background: #fff8f0; }
.card-neutral{ border-left: 5px solid #6c757d; background: #f8f9fa; }

.score-big { font-size: 3rem; font-weight: 700; line-height: 1; margin: 0; }
.score-label { font-size: 0.85rem; color: #666; margin-top: 2px; }

.bar-wrap  { background: #e9ecef; border-radius: 6px; height: 14px; overflow: hidden; margin: 4px 0 2px; }
.bar-fill  { height: 100%; border-radius: 6px; transition: width 0.3s; }
.bar-green  { background: linear-gradient(90deg, #28a745, #5cb85c); }
.bar-orange { background: linear-gradient(90deg, #fd7e14, #ffc107); }
.bar-red    { background: linear-gradient(90deg, #dc3545, #e06c75); }
.bar-blue   { background: linear-gradient(90deg, #0066cc, #4dabf7); }

.trade-row { display: flex; justify-content: space-between; gap: 0.5rem; margin: 0.5rem 0; }
.trade-cell { flex: 1; background: #fff; border-radius: 8px; padding: 0.7rem; text-align: center; border: 1px solid #dee2e6; }
.trade-cell .val { font-size: 1.2rem; font-weight: 700; }
.trade-cell .lbl { font-size: 0.75rem; color: #888; }
.trade-cell .stop-val { color: #dc3545; }
.trade-cell .tp-val   { color: #28a745; }

.tf-badge {
    display: inline-flex; align-items: center; gap: 0.35rem;
    padding: 0.35rem 0.7rem; border-radius: 8px; margin-right: 0.5rem;
    font-size: 0.85rem; font-weight: 600; border: 1px solid #dee2e6;
}
.tf-on  { background: #f0fff4; border-color: #c3e6cb; color: #1e7e34; }
.tf-off { background: #fff5f5; border-color: #f5c6cb; color: #a71d2a; }

.signal-item {
    padding: 0.45rem 0.75rem;
    border-radius: 6px;
    margin-bottom: 0.4rem;
    font-size: 0.9rem;
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
}
.signal-ok   { background: #f0fff4; border: 1px solid #c3e6cb; }
.signal-warn { background: #fff8f0; border: 1px solid #ffe0b2; }
.signal-block{ background: #fff5f5; border: 1px solid #f5c6cb; }

.formula {
    font-family: monospace;
    font-size: 0.9rem;
    background: #272822;
    color: #f8f8f2;
    padding: 0.6rem 1rem;
    border-radius: 6px;
    margin: 0.5rem 0 1rem;
}
.formula .set  { color: #a9dc76; }
.formula .mac  { color: #66d9e8; }
.formula .tim  { color: #ffd866; }
.formula .tot  { color: #fff; font-weight: bold; }

.tooltip-wrap {
    position: relative;
    display: inline-block;
    cursor: help;
    border-bottom: 1px dotted #999;
}
.tooltip-icon { font-size: 0.72em; color: #888; margin-left: 2px; }
.tooltip-wrap .tooltip-box {
    visibility: hidden;
    opacity: 0;
    position: absolute;
    top: 135%;
    left: 50%;
    transform: translateX(-50%);
    background: #272822;
    color: #f8f8f2;
    text-align: left;
    padding: 0.55rem 0.75rem;
    border-radius: 6px;
    font-size: 0.78rem;
    font-weight: 400;
    line-height: 1.35;
    width: 240px;
    z-index: 999;
    transition: opacity 0.15s ease;
    box-shadow: 0 2px 10px rgba(0,0,0,0.3);
}
.tooltip-wrap .tooltip-box::after {
    content: "";
    position: absolute;
    bottom: 100%;
    left: 50%;
    margin-left: -5px;
    border-width: 5px;
    border-style: solid;
    border-color: transparent transparent #272822 transparent;
}
.tooltip-wrap.tooltip-right .tooltip-box { left: auto; right: 0; transform: none; }
.tooltip-wrap.tooltip-right .tooltip-box::after { left: auto; right: 10px; margin-left: 0; }
.tooltip-wrap:hover .tooltip-box { visibility: visible; opacity: 1; }
</style>
"""


def inject_css() -> None:
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


def tip(label: str, explanation: str, align: str = "left") -> str:
    cls = "tooltip-wrap tooltip-right" if align == "right" else "tooltip-wrap"
    return (
        f'<span class="{cls}">{label}<span class="tooltip-icon">ⓘ</span>'
        f'<span class="tooltip-box">{explanation}</span></span>'
    )


def render_market_snapshot(btc_dominance: Optional[float], market_context: Dict[str, Any]) -> None:
    """Tarjetas de BTC Dominance + Cautela global. Pensada para usarse dentro
    de ``with st.sidebar:`` — misma pieza visual en inspector.py y en
    pages/1_Resumen.py."""
    dom_str = f"{btc_dominance:.1f}%" if btc_dominance is not None else "N/D"
    if btc_dominance is None:
        dom_color, dom_label = "#6c757d", "No disponible"
    elif btc_dominance >= 58:
        dom_color, dom_label = "#dc3545", "Alta — penaliza longs en altcoins"
    elif btc_dominance <= 44:
        dom_color, dom_label = "#28a745", "Baja — rotación a altcoins"
    else:
        dom_color, dom_label = "#6c757d", "Neutral"

    dom_tip = tip(
        "BTC Dominance",
        "Qué % de la capitalización total del mercado cripto es Bitcoin. "
        "Alta (≥58%) penaliza longs en altcoins y favorece shorts; "
        "baja (≤44%) hace lo opuesto. No aplica al propio BTC.",
    )
    st.markdown(f"""
    <div class="card" style="border-left: 4px solid {dom_color}; padding: 0.8rem 1rem; margin-bottom:0.6rem;">
      <div style="font-size:0.75rem;color:#888;">{dom_tip}</div>
      <div style="font-size:1.8rem;font-weight:700;color:{dom_color};">{dom_str}</div>
      <div style="font-size:0.78rem;color:{dom_color};">{dom_label}</div>
    </div>
    """, unsafe_allow_html=True)

    global_ctx = market_context.get("GLOBAL", {}) if isinstance(market_context, dict) else {}
    caution = str(global_ctx.get("caution_level", "NORMAL")).upper() if isinstance(global_ctx, dict) else "NORMAL"
    caution_colors = {"LOW": "#28a745", "NORMAL": "#28a745", "MEDIUM": "#fd7e14", "HIGH": "#dc3545", "EXTREME": "#dc3545"}
    caution_color = caution_colors.get(caution, "#6c757d")
    caution_tip = tip(
        "Cautela global",
        "Nivel manual configurado en market_context.json → GLOBAL.caution_level. "
        "A mayor cautela, mayor penalización al score de todas las señales "
        "(NORMAL=0, MEDIUM=-0.25, HIGH=-0.6, EXTREME=-1.0).",
    )
    st.markdown(f"""
    <div class="card" style="border-left: 4px solid {caution_color}; padding: 0.8rem 1rem; margin-bottom:0.6rem;">
      <div style="font-size:0.75rem;color:#888;">{caution_tip}</div>
      <div style="font-size:1.4rem;font-weight:700;color:{caution_color};">{caution}</div>
    </div>
    """, unsafe_allow_html=True)


def pair_label(symbol: str) -> str:
    """Todos los activos rastreados cotizan contra USDT en Bybit Spot (ver SYMBOL_TO_BASE)."""
    base, quote = SYMBOL_TO_BASE.get(symbol, (symbol, "USDT"))
    return f"{base}/{quote}"


def fmt_price(value: float, sig_figs: int = 4) -> str:
    """~sig_figs cifras significativas sin caer nunca en notación científica.

    ``f"{value:,.4g}"`` cambia a "6.352e+04" para precios de 5+ dígitos (BTC,
    ETH) porque el formato 'g' de Python usa notación exponencial en cuanto el
    exponente decimal iguala o supera la precisión pedida.
    """
    v = float(value)
    if v == 0:
        return "$0"
    order = math.floor(math.log10(abs(v)))
    decimals = max(0, sig_figs - 1 - order)
    return f"${v:,.{decimals}f}"


@st.cache_data(ttl=300, show_spinner=False)
def get_btc_dominance() -> Optional[float]:
    return fetch_btc_dominance()


@st.cache_data(ttl=120, show_spinner=False)
def get_context() -> Dict[str, Any]:
    return load_market_context(MARKET_CONTEXT_FILE)


@st.cache_data(ttl=90, show_spinner=False)
def get_klines(symbol: str):
    daily = fetch_klines(symbol, "1d", 300)
    fourh = fetch_klines(symbol, TRADING_TIMEFRAME, 300)
    entry = fetch_klines(symbol, ENTRY_TIMEFRAME, 100)
    price = fetch_latest_price(symbol)
    return daily, fourh, entry, price


def evaluate_pair(
    symbol: str, market_context: Dict[str, Any], btc_dominance: Optional[float]
) -> Optional[Dict[str, Any]]:
    """Corre el pipeline de evaluación (macro 1D + setup 4H + timing 15m + candidate)
    para ambos lados de `symbol`, igual que hace inspector.py para un solo activo.

    Devuelve None si Bybit/OKX no tienen velas suficientes en alguna de las 3
    capas (p. ej. TON, sin cobertura de par spot — ver CLAUDE.md).
    """
    daily_df, fourh_df, entry_df, current_price = get_klines(symbol)
    if daily_df is None or fourh_df is None or entry_df is None:
        return None

    cg_id = SYMBOL_TO_CGID[symbol]
    normalized_context = normalize_context(market_context, symbol)
    if btc_dominance is not None:
        normalized_context["btc_dominance"] = btc_dominance
    allowed_sides = parse_allowed_sides(normalized_context)

    results: Dict[str, Optional[Dict[str, Any]]] = {}
    for side in (SIDE_LONG, SIDE_SHORT):
        macro_eval = evaluate_macro_confirmation(daily_df, symbol, normalized_context, side=side)
        setup_eval = evaluate_setup_confirmation(fourh_df, symbol, cg_id, side=side)
        timing_eval = evaluate_timing_confirmation(entry_df, symbol, side=side)

        if not macro_eval or not setup_eval or not timing_eval:
            results[side] = None
            continue

        candidate = build_candidate(symbol, cg_id, macro_eval, setup_eval, timing_eval)
        candidate = apply_execution_quality_gate(candidate, current_price)
        results[side] = {
            "macro": macro_eval,
            "setup": setup_eval,
            "timing": timing_eval,
            "candidate": candidate,
        }

    return {
        "daily_df": daily_df,
        "fourh_df": fourh_df,
        "entry_df": entry_df,
        "current_price": current_price,
        "normalized_context": normalized_context,
        "allowed_sides": allowed_sides,
        "results": results,
    }
