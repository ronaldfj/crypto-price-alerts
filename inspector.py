"""
inspector.py — Crypto Sentinel Inspector
Evalúa cualquier activo rastreado con el motor idéntico a alert.py
(1D macro + 4H setup + 15m timing). Sin modificaciones al bot,
sin envío a Telegram, sin escribir en alerts_state.db (solo lectura
informativa de cooldown).

Uso:
    streamlit run inspector.py --server.port 8502
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))

import streamlit as st

st.set_page_config(
    page_title="Crypto Sentinel Inspector",
    page_icon="🪙",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS custom ────────────────────────────────────────────────────────────────

st.markdown("""
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
""", unsafe_allow_html=True)

# ── Imports del bot ───────────────────────────────────────────────────────────

from alert import (
    CRYPTO_IDS,
    MIN_SCORE,
    MIN_RR,
    MIN_ADX,
    TRADING_TIMEFRAME,
    ENTRY_TIMEFRAME,
    MARKET_CONTEXT_FILE,
    DB_FILE,
    COOLDOWN_HOURS,
    RSI_BAND_SHORT_MIN,
    RSI_BAND_SHORT_MAX,
    MAX_VWAP_DISTANCE_SHORT_PCT,
    REQUIRE_RSI_BAND_SHORT,
    REQUIRE_FIB_OUTSIDE_SHORT,
    REQUIRE_VWAP_PROXIMITY_SHORT,
    ENABLE_TACTICAL_ALERTS,
    ALLOW_MIXED_REGIME,
    ENABLE_RSI_CONFIRMATION,
    RISK_PER_TRADE_USD,
    SIDE_LONG,
    SIDE_SHORT,
    ACTIVE,
    evaluate_macro_confirmation,
    evaluate_setup_confirmation,
    evaluate_timing_confirmation,
    build_candidate,
    apply_execution_quality_gate,
    load_market_context,
    normalize_context,
    fetch_btc_dominance,
    parse_allowed_sides,
    validate_adx_minimum,
    validate_rsi_confirmation,
    validate_regime_filter,
    validate_rsi_band_short,
    validate_fib_outside_short,
    validate_vwap_proximity_short,
    _compute_qty,
    asset_group,
    side_label,
)
from data_source import fetch_klines, fetch_latest_price, SYMBOL_TO_BASE

SYMBOLS = sorted(CRYPTO_IDS.values())
SYMBOL_TO_CGID = {sym: cg for cg, sym in CRYPTO_IDS.items()}


def pair_label(symbol: str) -> str:
    """Todos los activos rastreados cotizan contra USDT en Bybit Spot (ver SYMBOL_TO_BASE)."""
    base, quote = SYMBOL_TO_BASE.get(symbol, (symbol, "USDT"))
    return f"{base}/{quote}"

# ── Helpers de presentación ───────────────────────────────────────────────────

def _pct(value: float, vmin: float, vmax: float) -> float:
    if vmax <= vmin:
        return 0.0
    return max(0.0, min(1.0, (value - vmin) / (vmax - vmin)))


def _bar_color(pct: float) -> str:
    if pct >= 0.6:
        return "bar-green"
    if pct >= 0.35:
        return "bar-orange"
    return "bar-red"


def _score_bar(label: str, value: float, vmin: float, vmax: float, color: str = "", tip: str = "") -> None:
    pct = _pct(value, vmin, vmax)
    css_color = color or _bar_color(pct)
    label_html = _tip(label, tip) if tip else label
    st.markdown(f"""
    <div style="margin-bottom:0.6rem;">
      <div style="display:flex; justify-content:space-between; font-size:0.82rem; color:#555; margin-bottom:2px;">
        <span>{label_html}</span><span><b>{value:.2f}</b></span>
      </div>
      <div class="bar-wrap">
        <div class="bar-fill {css_color}" style="width:{pct*100:.1f}%"></div>
      </div>
    </div>
    """, unsafe_allow_html=True)


def _tip(label: str, explanation: str, align: str = "left") -> str:
    cls = "tooltip-wrap tooltip-right" if align == "right" else "tooltip-wrap"
    return (
        f'<span class="{cls}">{label}<span class="tooltip-icon">ⓘ</span>'
        f'<span class="tooltip-box">{explanation}</span></span>'
    )


def _signal_row(text: str, kind: str) -> None:
    icons = {"ok": "✅", "warn": "⚠️", "block": "🚫"}
    css = {"ok": "signal-ok", "warn": "signal-warn", "block": "signal-block"}
    st.markdown(f"""
    <div class="signal-item {css[kind]}">
      <span>{icons[kind]}</span>
      <span>{text}</span>
    </div>
    """, unsafe_allow_html=True)


def _tf_badge(label: str, ok: bool) -> str:
    css = "tf-on" if ok else "tf-off"
    icon = "✅" if ok else "❌"
    return f'<span class="tf-badge {css}">{icon} {label}</span>'


# ── Cache ─────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def _get_btc_dominance() -> Optional[float]:
    return fetch_btc_dominance()


@st.cache_data(ttl=120, show_spinner=False)
def _get_context() -> Dict[str, Any]:
    return load_market_context(MARKET_CONTEXT_FILE)


@st.cache_data(ttl=90, show_spinner=False)
def _get_klines(symbol: str):
    daily = fetch_klines(symbol, "1d", 300)
    fourh = fetch_klines(symbol, TRADING_TIMEFRAME, 300)
    entry = fetch_klines(symbol, ENTRY_TIMEFRAME, 100)
    price = fetch_latest_price(symbol)
    return daily, fourh, entry, price


def _get_recent_alerts(symbol: str, side: str, timeframe: str) -> List[sqlite3.Row]:
    """Lectura de solo consulta (mode=ro) — nunca escribe en la DB del bot."""
    db_path = Path(DB_FILE)
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cutoff = int(time.time()) - COOLDOWN_HOURS * 3600
        rows = conn.execute(
            """
            SELECT sent_at, score, rr_ratio, status, entry_price
            FROM alerts
            WHERE symbol = ? AND side = ? AND timeframe = ? AND sent_at >= ?
            ORDER BY sent_at DESC
            """,
            (symbol, side, timeframe, cutoff),
        ).fetchall()
        conn.close()
        return rows
    except Exception:
        return []


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### Mercado")
    if st.button("Refrescar", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    with st.spinner(""):
        btc_dominance = _get_btc_dominance()
        market_context = _get_context()

    dom_str = f"{btc_dominance:.1f}%" if btc_dominance is not None else "N/D"
    if btc_dominance is None:
        dom_color, dom_label = "#6c757d", "No disponible"
    elif btc_dominance >= 58:
        dom_color, dom_label = "#dc3545", "Alta — penaliza longs en altcoins"
    elif btc_dominance <= 44:
        dom_color, dom_label = "#28a745", "Baja — rotación a altcoins"
    else:
        dom_color, dom_label = "#6c757d", "Neutral"

    dom_tip = _tip(
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
    caution_tip = _tip(
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

    st.divider()
    st.caption(
        f"Score mín: **{MIN_SCORE}** · RR mín: **{MIN_RR}** · ADX mín: **{MIN_ADX}**\n\n"
        f"Alertas TACTICAL: **{'activadas' if ENABLE_TACTICAL_ALERTS else 'desactivadas'}**\n\n"
        "Datos: Bybit (primario) / OKX (fallback), vela cerrada."
    )

# ── Header + Input ────────────────────────────────────────────────────────────

st.markdown("## Crypto Sentinel Inspector")
st.caption("Motor idéntico al bot (1D macro + 4H setup + 15m timing) · sin Telegram, sin escribir en la DB")

col_sym, col_btn = st.columns([5, 1])

with col_sym:
    symbol: str = st.selectbox(
        "",
        options=SYMBOLS,
        format_func=lambda s: f"{pair_label(s)}  —  {asset_group(s)}",
        label_visibility="collapsed",
    )

with col_btn:
    st.write("")
    evaluar = st.button("Evaluar →", type="primary", use_container_width=True)

st.divider()

if not evaluar:
    st.info("Seleccioná un activo y presioná **Evaluar →** para ver el análisis en las 3 capas (1D / 4H / 15m), para LONG y SHORT.")
    st.stop()

# ── Descarga + evaluación ─────────────────────────────────────────────────────

cg_id = SYMBOL_TO_CGID[symbol]

with st.spinner(f"Descargando velas 1D/4H/15m y evaluando {symbol}..."):
    daily_df, fourh_df, entry_df, current_price = _get_klines(symbol)

if daily_df is None or fourh_df is None or entry_df is None:
    st.error(f"Datos insuficientes para **{symbol}** — Bybit/OKX no devolvieron velas en alguna de las 3 capas.")
    st.stop()

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

price_str = f"${current_price:,.4g}" if current_price is not None else "N/D"
st.markdown(f"### {pair_label(symbol)} — {asset_group(symbol)}  ·  precio actual: **{price_str}**")

note = str(normalized_context.get("note", "")).strip()
if note:
    st.caption(f"📝 Nota de contexto: {note}")


# ── Render por lado ────────────────────────────────────────────────────────────

def render_side(side: str) -> None:
    data = results.get(side)
    if data is None:
        st.warning(f"Histórico insuficiente para evaluar {side} en alguna de las 3 capas (mínimo 210 velas 1D/4H, 60 en 15m).")
        return

    macro_eval = data["macro"]
    setup_eval = data["setup"]
    timing_eval = data["timing"]
    candidate = data["candidate"]

    side_ok = side in allowed_sides

    # ── Status badge ──────────────────────────────────────────────────────
    if candidate["alert"]:
        profile = candidate["alert_profile"]
        status_css, status_icon, status_text = "card-alert", "🔥", f"ALERTA — perfil {profile}"
    elif not side_ok:
        status_css, status_icon, status_text = "card-block", "🚫", "Lado no permitido (allowed_sides en market_context.json)"
    elif macro_eval.get("hard_block"):
        status_css, status_icon, status_text = "card-block", "🚫", "Bloqueo manual del lado (hard_block_long/short)"
    else:
        status_css, status_icon, status_text = "card-warn", "⚠️", (
            f"SIN SEÑAL — score {candidate['score']:.2f} (mín {MIN_SCORE}) · "
            f"RR {candidate['rr_ratio']:.2f}× (mín {candidate['required_min_rr']:.2f}) · "
            f"ADX {candidate['adx']:.1f} (mín {MIN_ADX})"
        )

    st.markdown(f"""
    <div class="card {status_css}" style="padding:1rem 1.4rem; margin-bottom:1rem;">
      <span style="font-size:1.1rem; font-weight:700;">{status_icon} {side_label(side)} ({side})</span><br>
      <span style="font-size:0.9rem;">{status_text}</span>
    </div>
    """, unsafe_allow_html=True)

    # ── Badges de confirmación por timeframe ─────────────────────────────
    badges = (
        _tf_badge(f"1D {macro_eval['regime']}", candidate["macro_ok"])
        + _tf_badge(f"4H {candidate['regime']}", candidate["setup_ok"])
        + _tf_badge(f"15m ({timing_eval['points']:.1f}pts)", candidate["timing_ok"])
    )
    st.markdown(badges, unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)

    left, right = st.columns([2, 3])

    # ═══════════════════════════════════════════════════════
    # COLUMNA IZQUIERDA
    # ═══════════════════════════════════════════════════════
    with left:
        score_color = "#28a745" if candidate["score"] >= MIN_SCORE else "#dc3545"
        rr_color = "#28a745" if candidate["rr_ratio"] >= candidate["required_min_rr"] else "#dc3545"

        score_tip = _tip(
            "Score Total",
            "Suma de la base 4H (estructura/RSI/ADX/Fib/VWAP) más los ajustes de "
            f"contexto 1D y 15m. Por debajo de {MIN_SCORE} no se generaría alerta.",
        )
        rr_tip = _tip(
            "Risk/Reward",
            "Ganancia potencial (a TP2) dividida por el riesgo hasta el stop loss. "
            "El mínimo requerido puede ser más bajo que MIN_RR si el contexto táctico "
            "cappea el target (ver 'RR mínimo requerido').",
            align="right",
        )
        confirm_tip = _tip(
            "Confirmaciones",
            "Cuántas de las 3 capas (1D macro, 4H setup, 15m timing) están en OK ahora mismo. "
            "Se necesitan las 3 para el perfil FULL.",
        )
        adx_tip = _tip(
            "ADX (4H)",
            "Fuerza de la tendencia en 4H, sin importar dirección. Por debajo del mínimo, "
            "el setup se descarta aunque el score sea alto.",
            align="right",
        )

        st.markdown(f"""
        <div class="card card-neutral" style="padding:1.2rem;">
          <div style="display:flex; justify-content:space-between; align-items:flex-start;">
            <div>
              <div class="score-label">{score_tip}</div>
              <div class="score-big" style="color:{score_color};">{candidate['score']:.2f}</div>
              <div class="score-label" style="color:{score_color};">mínimo {MIN_SCORE}</div>
            </div>
            <div style="text-align:right;">
              <div class="score-label">{rr_tip}</div>
              <div style="font-size:2rem; font-weight:700; color:{rr_color};">{candidate['rr_ratio']:.2f}×</div>
              <div class="score-label" style="color:{rr_color};">mínimo {candidate['required_min_rr']:.2f}</div>
            </div>
          </div>
          <div style="border-top:1px solid #dee2e6; margin:0.8rem 0;"></div>
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <div>
              <div class="score-label">{confirm_tip}</div>
              <div style="font-size:1.4rem; font-weight:700;">{candidate['confirmations_passed']}/3</div>
            </div>
            <div style="text-align:right;">
              <div class="score-label">{adx_tip}</div>
              <div style="font-size:1.4rem; font-weight:700; color:{'#28a745' if candidate['adx'] >= MIN_ADX else '#dc3545'};">{candidate['adx']:.1f}</div>
            </div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        # Trade Setup
        entry = float(candidate["entry_price"])
        stop = float(candidate["stop_loss"])
        tp1 = float(candidate.get("tp1", candidate["take_profit"]))
        tp2 = float(candidate.get("tp2", candidate["take_profit"]))
        risk_mult = float(candidate.get("risk_multiplier", 1.0))
        qty_str = _compute_qty(entry, stop, risk_mult)
        risk_usd = RISK_PER_TRADE_USD * max(risk_mult, 0.01)

        entry_tip = _tip("Entry", "Precio de entrada. Si la ejecución está en curso (CAUTION/EXECUTABLE), ya refleja el precio actual, no la vela 4H original.")
        stop_tip = _tip("Stop Loss", "Precio de salida de emergencia. Corta la pérdida ahí y no más abajo/arriba.")
        tp1_tip = _tip("TP1", "Primer objetivo parcial, más conservador.")
        tp2_tip = _tip("TP2", "Objetivo final, usado para el R:R mostrado arriba.")
        qty_tip = _tip("Cantidad", f"(RISK_PER_TRADE_USD={RISK_PER_TRADE_USD} × risk_multiplier) / distancia al stop.")
        risk_tip = _tip("Riesgo USD", "Cuánto se pierde si el precio llega al stop, dado el risk_multiplier vigente.")

        st.markdown("##### Trade Setup")
        st.markdown(f"""
        <div class="trade-row">
          <div class="trade-cell">
            <div class="lbl">{entry_tip}</div>
            <div class="val">${entry:,.4g}</div>
          </div>
          <div class="trade-cell">
            <div class="lbl">{stop_tip}</div>
            <div class="val stop-val">${stop:,.4g}</div>
          </div>
          <div class="trade-cell">
            <div class="lbl">{tp1_tip}</div>
            <div class="val tp-val">${tp1:,.4g}</div>
          </div>
          <div class="trade-cell">
            <div class="lbl">{tp2_tip}</div>
            <div class="val tp-val">${tp2:,.4g}</div>
          </div>
        </div>
        <div class="trade-row">
          <div class="trade-cell">
            <div class="lbl">{qty_tip}</div>
            <div class="val">{qty_str}</div>
          </div>
          <div class="trade-cell">
            <div class="lbl">{risk_tip}</div>
            <div class="val">${risk_usd:.2f}</div>
          </div>
          <div class="trade-cell">
            <div class="lbl">risk_multiplier</div>
            <div class="val">{risk_mult:.2f}</div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        st.caption(
            f"Breakeven trigger: **${candidate.get('breakeven_trigger', 0):,.4g}** &nbsp;·&nbsp; "
            f"Grupo: **{candidate['asset_group']}**"
        )

        # Execution gate
        exec_state = candidate.get("execution_state", "NOT_CHECKED")
        exec_colors = {
            "EXECUTABLE": "#28a745", "CAUTION": "#fd7e14",
            "LATE": "#dc3545", "INVALID_NOW": "#dc3545", "NOT_CHECKED": "#6c757d",
        }
        exec_color = exec_colors.get(exec_state, "#6c757d")
        metrics = candidate.get("execution", {})
        exec_tip = _tip(
            "Execution Gate",
            "Evita convertir en alerta un setup técnicamente válido pero que ya corrió "
            "demasiado hacia TP1, o cuyo R:R real al precio actual se deterioró.",
        )
        st.markdown(f"""
        <div class="card" style="border-left:4px solid {exec_color}; padding:0.8rem 1rem;">
          <div style="font-size:0.8rem;color:#888;">{exec_tip}</div>
          <div style="font-weight:700; color:{exec_color};">{exec_state} — {candidate.get('execution_decision','')}</div>
          <div style="font-size:0.8rem; color:#555; margin-top:4px;">
            Avance a TP1: {metrics.get('progress_to_tp1_pct', 0):.1f}% &nbsp;·&nbsp;
            R:R actual (TP2): {metrics.get('current_rr_tp2', 0):.2f}× &nbsp;·&nbsp;
            Drift desde señal: {metrics.get('price_drift_pct', 0):+.2f}%
          </div>
        </div>
        """, unsafe_allow_html=True)

    # ═══════════════════════════════════════════════════════
    # COLUMNA DERECHA — tabs
    # ═══════════════════════════════════════════════════════
    with right:
        tab_score, tab_ind, tab_signals, tab_cooldown = st.tabs(
            ["📊 Scoring", "📐 Indicadores", "🔍 Señales", "🕒 Cooldown"]
        )

        # ── TAB 1: Scoring ──────────────────────────────────
        with tab_score:
            base_4h = float(setup_eval["score"])
            adj_1d = float(macro_eval["score_adjustment"])
            adj_15m = float(timing_eval["score_adjustment"])

            st.markdown(f"""
            <div class="formula">
              <span class="set">{base_4h:.2f}</span> (base 4H)
              &nbsp;+&nbsp;
              <span class="mac">{adj_1d:+.2f}</span> (ajuste 1D)
              &nbsp;+&nbsp;
              <span class="tim">{adj_15m:+.2f}</span> (ajuste 15m)
              &nbsp;=&nbsp;
              <span class="tot">{candidate['score']:.2f}</span>
            </div>
            """, unsafe_allow_html=True)

            st.markdown(_tip(
                "**4H — base del setup**",
                "Combina régimen EMA20/50/200, posición del precio, cruce reciente, RSI, "
                "ADX+DI, zona Fibonacci, distancia a VWAP y momentum de volumen (solo LONG).",
            ), unsafe_allow_html=True)
            _score_bar("Score base 4H", base_4h, -2.0, 8.0, tip="Puntos acumulados por la capa 4H antes de ajustes de contexto.")

            st.markdown(_tip(
                "**1D — ajuste macro**",
                "Penalización/bono por cautela global, BTC dominance y ajustes manuales "
                "(long/short_score_adjustment) en market_context.json.",
            ), unsafe_allow_html=True)
            _score_bar("Ajuste 1D", adj_1d, -1.5, 1.0, "bar-blue", tip="Positivo favorece la señal, negativo la penaliza.")

            st.markdown(_tip(
                "**15m — ajuste de timing**",
                "Pequeños ajustes por momentum de entrada, divergencia de volumen y "
                "distancia a VWAP/soporte-resistencia local en 15m.",
            ), unsafe_allow_html=True)
            _score_bar("Ajuste 15m", adj_15m, -0.6, 0.4, "bar-blue", tip="Positivo = entrada táctica limpia; negativo = timing débil.")

            st.divider()
            _score_bar(
                f"Score Total (mín {MIN_SCORE})", candidate["score"], 0, 10.0,
                "bar-green" if candidate["score"] >= MIN_SCORE else "bar-red",
                tip=f"Si no llega a {MIN_SCORE}, no se generaría alerta aunque el resto esté OK.",
            )
            _score_bar(
                f"Risk/Reward (mín {candidate['required_min_rr']:.2f})", candidate["rr_ratio"], 0, 3.0,
                "bar-green" if candidate["rr_ratio"] >= candidate["required_min_rr"] else "bar-red",
                tip="RR mínimo requerido puede ser menor que MIN_RR si el contexto táctico cappea el target.",
            )
            _score_bar(
                f"ADX (mín {MIN_ADX})", candidate["adx"], 0, 45.0,
                "bar-green" if candidate["adx"] >= MIN_ADX else "bar-red",
                tip="Fuerza de tendencia 4H. Gate independiente del score.",
            )

        # ── TAB 2: Indicadores ──────────────────────────────
        with tab_ind:
            st.markdown("**1D — Macro**")
            m1, m2, m3 = st.columns(3)
            m1.metric("Régimen 1D", macro_eval["regime"], help="BULL_STACK / BEAR_STACK / MIXED según EMA20 vs EMA50 vs EMA200 diario.")
            m2.metric("RSI 1D", f"{macro_eval['rsi']:.1f}", help="RSI(14) diario.")
            m3.metric("ADX 1D", f"{macro_eval['adx']:.1f}", help="Fuerza de tendencia diaria (informativo, el gate de ADX usa la capa 4H).")

            m4, m5, m6 = st.columns(3)
            m4.metric("Bias manual", macro_eval.get("macro_bias", "—"), help="macro_bias configurado en market_context.json para este activo.")
            m5.metric("Cautela", macro_eval.get("caution_level", "NORMAL"), help="caution_level efectivo (GLOBAL + override del activo).")
            m6.metric("Nivel cercano", "Sí" if macro_eval.get("barrier_near") else "No", help=macro_eval.get("barrier_label") or "Sin resistencia/soporte manual marcado cerca.")

            st.divider()
            st.markdown("**4H — Setup**")
            s1, s2, s3 = st.columns(3)
            s1.metric("Régimen 4H", candidate["regime"], help="Régimen EMA en la capa 4H, la que determina el setup base.")
            s2.metric("RSI 4H", f"{candidate['rsi']:.1f}", help="RSI(14) en 4H.")
            s3.metric("ATR 4H", f"${candidate['atr']:,.4g}", help="Volatilidad 4H en unidades de precio; define distancia de stop/TP.")

            s4, s5, s6 = st.columns(3)
            s4.metric("VWAP dist.", f"{candidate['vwap_distance_pct']:+.2f}%", help="Distancia del precio al VWAP de las últimas 20 velas 4H.")
            s5.metric("Zona Fibonacci", candidate["fib_zone"], help="Retroceso (LONG) o pullback desde mínimo (SHORT) sobre el rango de 55 velas 4H.")
            s6.metric("Volumen", "Fuerte" if candidate.get("volume_strong") else ("Divergente" if candidate.get("volume_divergence") else "Normal"), help="Momentum de volumen 4H (solo contribuye al score para LONG; para SHORT es informativo).")

            st.divider()
            st.markdown("**15m — Timing**")
            t1, t2, t3 = st.columns(3)
            t1.metric("Puntos 15m", f"{timing_eval['points']:.2f}", help="Puntaje interno de timing; se requiere ≥2.4 y sin hard_fail para timing_ok.")
            t2.metric("RSI 15m", f"{timing_eval['rsi']:.1f}", help="RSI(14) en 15m.")
            t3.metric("VWAP dist. 15m", f"{timing_eval['vwap_distance_pct']:+.2f}%", help="Distancia al VWAP de las últimas 16 velas 15m.")

            st.divider()
            st.markdown("**Niveles de referencia**")
            l1, l2 = st.columns(2)
            l1.markdown(f"""
            | Nivel | Valor |
            |---|---|
            | Swing low (4H) | ${candidate['swing_low']:,.4g} |
            | Swing high (4H) | ${candidate['swing_high']:,.4g} |
            | Dist. a swing high | {candidate.get('distance_to_swing_high_pct', 0):.2f}% |
            | Dist. a swing low | {candidate.get('distance_to_swing_low_pct', 0):.2f}% |
            """)
            l2.markdown(f"""
            | Métrica | Valor |
            |---|---|
            | tp1_rr / tp2_rr | {candidate['tp1_rr']:.2f} / {candidate['tp2_rr']:.2f} |
            | move_to_be_rr | {candidate.get('move_to_be_rr', 0):.2f} |
            | Perfil de alerta | {candidate['alert_profile']} |
            | Setup key | `{candidate['setup_key'][:40]}…` |
            """)

        # ── TAB 3: Señales ───────────────────────────────────
        with tab_signals:
            blockers: List[str] = []
            if not candidate["macro_ok"] and not candidate.get("tactical_alert"):
                blockers.append("1D macro no confirma este lado")
            if not candidate["setup_ok"]:
                blockers.append("4H setup no confirma (estructura/RR/score insuficiente)")
            if not candidate["timing_ok"]:
                blockers.append("15m timing no confirma (hard-fail o puntos < 2.4)")
            if candidate["score"] < MIN_SCORE:
                blockers.append(f"score {candidate['score']:.2f} < mínimo {MIN_SCORE}")
            if candidate["adx"] < MIN_ADX:
                blockers.append(f"ADX {candidate['adx']:.1f} < mínimo {MIN_ADX}")
            if candidate["rr_ratio"] < candidate["required_min_rr"]:
                blockers.append(f"RR {candidate['rr_ratio']:.2f} < mínimo {candidate['required_min_rr']:.2f}")
            if not side_ok:
                blockers.append("Lado no está en allowed_sides")
            if macro_eval.get("hard_block"):
                blockers.append("hard_block manual activo para este lado")

            st.markdown(f"##### 🚫 Bloqueos estructurales ({len(blockers)})")
            st.caption("Condiciones que impiden la alerta ahora mismo, aunque el resto se vea bien.")
            if blockers:
                for b in blockers:
                    _signal_row(b, "block")
            else:
                st.caption("Ninguno — 3/3 confirmaciones + thresholds cumplidos.")

            st.divider()
            st.markdown("##### ✅ Gates de calidad validados por backtest")
            st.caption("Recalculados en vivo con las mismas funciones que usa el bot.")

            adx_ok, adx_msg = validate_adx_minimum(candidate["adx"])
            _signal_row(adx_msg or f"ADX {candidate['adx']:.1f} ≥ mínimo {MIN_ADX}", "ok" if adx_ok else "block")

            if ENABLE_RSI_CONFIRMATION:
                rsi_ok, rsi_msg = validate_rsi_confirmation(candidate["rsi"], candidate["adx"], candidate["regime"])
                _signal_row(rsi_msg or f"RSI {candidate['rsi']:.1f} dentro de rango normal (30-70) o con tendencia suficiente", "ok" if rsi_ok else "block")

            if not ALLOW_MIXED_REGIME:
                regime_ok, regime_msg = validate_regime_filter(candidate["regime"])
                _signal_row(regime_msg or f"Régimen {candidate['regime']} no es ambiguo", "ok" if regime_ok else "block")

            if side == SIDE_SHORT:
                if REQUIRE_RSI_BAND_SHORT:
                    rsi_band_ok, rsi_band_msg = validate_rsi_band_short(candidate["rsi"], side)
                    _signal_row(rsi_band_msg or f"RSI {candidate['rsi']:.1f} en banda válida [{RSI_BAND_SHORT_MIN:.0f}-{RSI_BAND_SHORT_MAX:.0f})", "ok" if rsi_band_ok else "block")
                if REQUIRE_FIB_OUTSIDE_SHORT:
                    fib_ok, fib_msg = validate_fib_outside_short(candidate["fib_zone"], side)
                    _signal_row(fib_msg or "Entrada fuera de zona Fibonacci 0.382-0.786", "ok" if fib_ok else "block")
                if REQUIRE_VWAP_PROXIMITY_SHORT:
                    vwap_ok, vwap_msg = validate_vwap_proximity_short(candidate.get("vwap_distance_pct", 0.0), side)
                    _signal_row(vwap_msg or f"Distancia a VWAP dentro de ±{MAX_VWAP_DISTANCE_SHORT_PCT:.1f}%", "ok" if vwap_ok else "block")
            else:
                st.caption("Los gates RSI-band / Fibonacci-outside / VWAP-proximity son exclusivos de SHORT (validados por walk-forward).")

            st.divider()
            st.markdown(f"##### 📋 Razones combinadas ({len(candidate['reasons'])})")
            st.caption("Registro completo de la evaluación: 4H sin prefijo, 1D con prefijo '1D:', 15m con prefijo '15m:'.")
            for r in candidate["reasons"]:
                kind = "ok"
                lowered = r.lower()
                if any(kw in lowered for kw in ["bloqueo", "gate:", "extremo", "ambigu", "fuera de banda", "fibonacci (", "sobreextendid", "insuficiente", "demasiado", "no ejecutable", "pegado"]):
                    kind = "block"
                elif any(kw in lowered for kw in ["cautela", "advertencia", "requiere"]):
                    kind = "warn"
                _signal_row(r, kind)

        # ── TAB 4: Cooldown ──────────────────────────────────
        with tab_cooldown:
            st.caption(f"Alertas ACTIVAS enviadas para {symbol} {side} en las últimas {COOLDOWN_HOURS}h (solo lectura de alerts_state.db).")
            rows = _get_recent_alerts(symbol, side, TRADING_TIMEFRAME)
            if not rows:
                st.success("Sin alertas activas recientes — una señal válida ahora no chocaría con cooldown.")
            else:
                for row in rows:
                    sent_dt = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(row["sent_at"]))
                    st.markdown(f"""
                    <div class="signal-item {"signal-ok" if row["status"] == ACTIVE else "signal-warn"}">
                      <span>{"🟢" if row["status"] == ACTIVE else "⚪"}</span>
                      <span>{sent_dt} · score {row['score']:.2f} · RR {row['rr_ratio']:.2f} · entry ${row['entry_price']:,.4g} · status {row['status']}</span>
                    </div>
                    """, unsafe_allow_html=True)


tab_long, tab_short = st.tabs(["🟢 LONG", "🔴 SHORT"])
with tab_long:
    render_side(SIDE_LONG)
with tab_short:
    render_side(SIDE_SHORT)
