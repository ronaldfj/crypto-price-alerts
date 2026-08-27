"""
sentinel_shared.py — Utilidades y caché compartidas entre las páginas de
Crypto Sentinel Inspector (vista detalle en Inspector.py + resumen en
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
# Ambas páginas (Inspector.py y pages/1_Resumen.py) son scripts Streamlit
# independientes: sin esto, cada una necesitaría reinyectar el mismo CSS y
# reconstruir las mismas tarjetas a mano, y terminarían divergiendo visualmente.

CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600&display=swap');
:root {
    --ss-text-muted: #77756e;
    --ss-text-faint: #a3a099;
    --ss-border: #e6e3dc;
    --ss-card: #f8f9fa;
    --ss-card-2: #ffffff;
    --ss-accent: #2f6fed;
    --ss-up: #16824f;
    --ss-up-soft: #eef8f2;
    --ss-up-border: #c3e6cb;
    --ss-down: #c53a2b;
    --ss-down-soft: #fdf1ef;
    --ss-down-border: #f5c6cb;
    --ss-warn: #b8600a;
    --ss-warn-soft: #fff8f0;
    --ss-warn-border: #ffe0b2;
    --ss-neutral: #6c757d;
    --ss-code-bg: #23241f;
    --ss-code-text: #f8f8f2;
    --ss-shadow: 0 1px 2px rgba(30,25,10,0.05), 0 1px 8px rgba(30,25,10,0.03);
}
@media (prefers-color-scheme: dark) {
    :root {
        --ss-text-muted: #9a99a3;
        --ss-text-faint: #6d6c76;
        --ss-border: #33333c;
        --ss-card: #1c1c22;
        --ss-card-2: #202027;
        --ss-accent: #5b8bff;
        --ss-up: #3ddc8f;
        --ss-up-soft: rgba(61,220,143,0.10);
        --ss-up-border: rgba(61,220,143,0.30);
        --ss-down: #ff6b5c;
        --ss-down-soft: rgba(255,107,92,0.10);
        --ss-down-border: rgba(255,107,92,0.30);
        --ss-warn: #f0b23d;
        --ss-warn-soft: rgba(240,178,61,0.10);
        --ss-warn-border: rgba(240,178,61,0.30);
        --ss-neutral: #9a99a3;
        --ss-code-bg: #17181c;
        --ss-code-text: #eceae4;
        --ss-shadow: 0 6px 20px -10px rgba(0,0,0,0.5);
    }
}

.stButton > button {
    border-radius: 999px !important;
    font-weight: 600 !important;
}

.ss-kicker {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 0.72rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
    color: var(--ss-accent); margin-bottom: 2px;
}
.ss-kicker .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--ss-accent); }
.ss-page-title { font-size: 1.6rem; font-weight: 800; letter-spacing: -0.01em; margin: 0 0 2px; }
.ss-page-sub { color: var(--ss-text-muted); font-size: 0.85rem; margin-bottom: 0.6rem; }

.card {
    background: var(--ss-card);
    border: 1px solid var(--ss-border);
    border-radius: 12px;
    padding: 1.2rem 1.4rem;
    margin-bottom: 0.8rem;
    box-shadow: var(--ss-shadow);
}
.card-alert  { border-left: 5px solid var(--ss-up); background: var(--ss-up-soft); }
.card-block  { border-left: 5px solid var(--ss-down); background: var(--ss-down-soft); }
.card-warn   { border-left: 5px solid var(--ss-warn); background: var(--ss-warn-soft); }
.card-neutral{ border-left: 5px solid var(--ss-neutral); background: var(--ss-card); }

.score-big { font-family: "JetBrains Mono", ui-monospace, monospace; font-variant-numeric: tabular-nums; font-size: 3rem; font-weight: 700; line-height: 1; margin: 0; }
.score-label { font-size: 0.85rem; color: var(--ss-text-muted); margin-top: 2px; }

.bar-wrap  { background: var(--ss-border); border-radius: 6px; height: 14px; overflow: hidden; margin: 4px 0 2px; }
.bar-fill  { height: 100%; border-radius: 6px; transition: width 0.3s; }
.bar-green  { background: linear-gradient(90deg, #16824f, #3ddc8f); }
.bar-orange { background: linear-gradient(90deg, #d9820a, #ffc107); }
.bar-red    { background: linear-gradient(90deg, #c53a2b, #ff8a7a); }
.bar-blue   { background: linear-gradient(90deg, var(--ss-accent), #6ea1ff); }

.trade-row { display: flex; justify-content: space-between; gap: 0.5rem; margin: 0.5rem 0; }
.trade-cell { flex: 1; background: var(--ss-card-2); border-radius: 10px; padding: 0.7rem; text-align: center; border: 1px solid var(--ss-border); }
.trade-cell .val { font-family: "JetBrains Mono", ui-monospace, monospace; font-variant-numeric: tabular-nums; font-size: 1.15rem; font-weight: 700; }
.trade-cell .lbl { font-size: 0.72rem; color: var(--ss-text-muted); text-transform: uppercase; letter-spacing: .03em; }
.trade-cell .stop-val { color: var(--ss-down); }
.trade-cell .tp-val   { color: var(--ss-up); }

.tf-badge {
    display: inline-flex; align-items: center; gap: 0.35rem;
    padding: 0.35rem 0.7rem; border-radius: 999px; margin-right: 0.5rem;
    font-size: 0.85rem; font-weight: 600; border: 1px solid var(--ss-border);
}
.tf-on  { background: var(--ss-up-soft); border-color: var(--ss-up-border); color: var(--ss-up); }
.tf-off { background: var(--ss-down-soft); border-color: var(--ss-down-border); color: var(--ss-down); }

.signal-item {
    padding: 0.5rem 0.8rem;
    border-radius: 8px;
    margin-bottom: 0.4rem;
    font-size: 0.9rem;
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
}
.signal-ok   { background: var(--ss-up-soft); border: 1px solid var(--ss-up-border); }
.signal-warn { background: var(--ss-warn-soft); border: 1px solid var(--ss-warn-border); }
.signal-block{ background: var(--ss-down-soft); border: 1px solid var(--ss-down-border); }

.formula {
    font-family: "JetBrains Mono", ui-monospace, monospace;
    font-size: 0.88rem;
    background: var(--ss-code-bg);
    color: var(--ss-code-text);
    padding: 0.7rem 1rem;
    border-radius: 10px;
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
    border-bottom: 1px dotted var(--ss-text-faint);
}
.tooltip-icon { font-size: 0.72em; color: var(--ss-text-muted); margin-left: 2px; }
.tooltip-wrap .tooltip-box {
    visibility: hidden;
    opacity: 0;
    position: absolute;
    top: 135%;
    left: 50%;
    transform: translateX(-50%);
    background: var(--ss-code-bg);
    color: var(--ss-code-text);
    text-align: left;
    padding: 0.55rem 0.75rem;
    border-radius: 8px;
    font-size: 0.78rem;
    font-weight: 400;
    line-height: 1.35;
    width: 240px;
    z-index: 999;
    transition: opacity 0.15s ease;
    box-shadow: 0 4px 16px rgba(0,0,0,0.25);
}
.tooltip-wrap .tooltip-box::after {
    content: "";
    position: absolute;
    bottom: 100%;
    left: 50%;
    margin-left: -5px;
    border-width: 5px;
    border-style: solid;
    border-color: transparent transparent var(--ss-code-bg) transparent;
}
.tooltip-wrap.tooltip-right .tooltip-box { left: auto; right: 0; transform: none; }
.tooltip-wrap.tooltip-right .tooltip-box::after { left: auto; right: 10px; margin-left: 0; }
.tooltip-wrap:hover .tooltip-box { visibility: visible; opacity: 1; }
</style>
"""


def inject_css() -> None:
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


def render_page_header(title: str, subtitle: str, kicker: str = "Crypto Sentinel") -> None:
    """Encabezado compartido (kicker + título + subtítulo) para que Inspector.py
    y pages/1_Resumen.py abran con la misma identidad visual en vez de un
    st.markdown("## ...") suelto en cada script."""
    st.markdown(f"""
    <div class="ss-kicker"><span class="dot"></span>{kicker}</div>
    <div class="ss-page-title">{title}</div>
    <div class="ss-page-sub">{subtitle}</div>
    """, unsafe_allow_html=True)


def tip(label: str, explanation: str, align: str = "left") -> str:
    cls = "tooltip-wrap tooltip-right" if align == "right" else "tooltip-wrap"
    return (
        f'<span class="{cls}">{label}<span class="tooltip-icon">ⓘ</span>'
        f'<span class="tooltip-box">{explanation}</span></span>'
    )


def render_market_snapshot(btc_dominance: Optional[float], market_context: Dict[str, Any]) -> None:
    """Tarjetas de BTC Dominance + Cautela global. Pensada para usarse dentro
    de ``with st.sidebar:`` — misma pieza visual en Inspector.py y en
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
      <div style="font-size:0.75rem;color:var(--ss-text-muted);">{dom_tip}</div>
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
      <div style="font-size:0.75rem;color:var(--ss-text-muted);">{caution_tip}</div>
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
    para ambos lados de `symbol`, igual que hace Inspector.py para un solo activo.

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
