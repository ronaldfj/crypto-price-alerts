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
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))

import plotly.graph_objects as go
import streamlit as st

st.set_page_config(
    page_title="Crypto Sentinel Inspector",
    page_icon="🪙",
    layout="wide",
    initial_sidebar_state="expanded",
)

from sentinel_shared import (
    SYMBOLS,
    pair_label,
    fmt_price as _fmt_price,
    get_btc_dominance as _get_btc_dominance,
    get_context as _get_context,
    get_klines as _get_klines,
    evaluate_pair,
    inject_css,
    tip as _tip,
    render_market_snapshot,
)

inject_css()

# ── Imports del bot ───────────────────────────────────────────────────────────

from alert import (
    MIN_SCORE,
    MIN_RR,
    MIN_ADX,
    TRADING_TIMEFRAME,
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


def _candlestick_chart(
    df,
    *,
    ema_spans: Tuple[int, ...] = (20, 50),
    entry: Optional[float] = None,
    stop: Optional[float] = None,
    tp1: Optional[float] = None,
    tp2: Optional[float] = None,
    height: int = 380,
    tail_bars: Optional[int] = None,
) -> go.Figure:
    """Velas + EMAs de contexto y, si se pasan, niveles de trade (solo capa 4H).

    EMAs se calculan sobre el histórico completo (para que no arranquen "frías")
    y recién después se recorta a `tail_bars` para mostrar solo el tramo reciente.
    """
    ema_full = {span: df["Close"].ewm(span=span, adjust=False).mean() for span in ema_spans}

    plot_df = df
    if tail_bars is not None and len(df) > tail_bars:
        plot_df = df.tail(tail_bars)
        ema_full = {span: ema.tail(tail_bars) for span, ema in ema_full.items()}

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=plot_df["ts"], open=plot_df["Open"], high=plot_df["High"], low=plot_df["Low"], close=plot_df["Close"],
        name="Precio", increasing_line_color="#28a745", decreasing_line_color="#dc3545",
        showlegend=False,
    ))
    ema_colors = {20: "#4dabf7", 50: "#fd7e14", 200: "#a371f7"}
    for span, ema in ema_full.items():
        fig.add_trace(go.Scatter(
            x=plot_df["ts"], y=ema, name=f"EMA{span}", mode="lines",
            line=dict(color=ema_colors.get(span, "#888"), width=1.2),
        ))

    if entry is not None and stop is not None:
        fig.add_hrect(
            y0=min(entry, stop), y1=max(entry, stop),
            fillcolor="rgba(220,53,69,0.12)", line_width=0, layer="below",
        )
    if entry is not None and tp2 is not None:
        fig.add_hrect(
            y0=min(entry, tp2), y1=max(entry, tp2),
            fillcolor="rgba(40,167,69,0.12)", line_width=0, layer="below",
        )

    levels = (
        (entry, "Entry", "#0066cc"),
        (stop, "Stop", "#dc3545"),
        (tp1, "TP1", "#28a745"),
        (tp2, "TP2", "#1e7e34"),
    )
    for value, _label, color in levels:
        if value is None:
            continue
        fig.add_hline(y=value, line_dash="dot", line_color=color, line_width=1.3)

    # Caja agrupada en vez de una etiqueta por línea: cuando entry/stop/tp1/tp2
    # quedan muy cerca en precio (rango angosto vs. todo el histórico mostrado),
    # las anotaciones individuales se superponen y se vuelven ilegibles.
    box_rows = [
        f'<span style="color:{color}">{label} <b>{_fmt_price(value)}</b></span>'
        for value, label, color in levels if value is not None
    ]
    if box_rows:
        fig.add_annotation(
            xref="paper", yref="paper", x=0.01, y=0.98,
            xanchor="left", yanchor="top", align="left", showarrow=False,
            text="<br>".join(box_rows),
            bgcolor="rgba(255,255,255,0.9)", bordercolor="#dee2e6", borderwidth=1,
            borderpad=6, font=dict(size=11.5),
        )

    fig.update_layout(
        height=height,
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis_rangeslider_visible=False,
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
        hovermode="x unified",
    )
    return fig


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

    render_market_snapshot(btc_dominance, market_context)

    st.divider()
    st.caption(
        f"Score mín: **{MIN_SCORE}** · RR mín: **{MIN_RR}** · ADX mín: **{MIN_ADX}**\n\n"
        f"Alertas TACTICAL: **{'activadas' if ENABLE_TACTICAL_ALERTS else 'desactivadas'}**\n\n"
        "Datos: Bybit (primario) / OKX (fallback), vela cerrada."
    )

# ── Header + Input ────────────────────────────────────────────────────────────

st.markdown("## Crypto Sentinel Inspector")
st.caption("Motor idéntico al bot (1D macro + 4H setup + 15m timing) · sin Telegram, sin escribir en la DB")

# Si venimos de un clic en la tabla de pages/1_Resumen.py, preseleccionar ese
# activo y saltar directo a la evaluación sin exigir un segundo clic en "Evaluar".
preselected_symbol = st.session_state.get("selected_symbol")
default_index = SYMBOLS.index(preselected_symbol) if preselected_symbol in SYMBOLS else 0

col_sym, col_btn = st.columns([5, 1])

with col_sym:
    symbol: str = st.selectbox(
        "",
        options=SYMBOLS,
        index=default_index,
        format_func=lambda s: f"{pair_label(s)}  —  {asset_group(s)}",
        label_visibility="collapsed",
    )

with col_btn:
    st.write("")
    evaluar = st.button("Evaluar →", type="primary", use_container_width=True)

evaluar = evaluar or (preselected_symbol == symbol)

st.divider()

if not evaluar:
    st.info("Seleccioná un activo y presioná **Evaluar →** para ver el análisis en las 3 capas (1D / 4H / 15m), para LONG y SHORT.")
    st.stop()

# ── Descarga + evaluación ─────────────────────────────────────────────────────

with st.spinner(f"Descargando velas 1D/4H/15m y evaluando {symbol}..."):
    pair_data = evaluate_pair(symbol, market_context, btc_dominance)

if pair_data is None:
    st.error(f"Datos insuficientes para **{symbol}** — Bybit/OKX no devolvieron velas en alguna de las 3 capas.")
    st.stop()

daily_df = pair_data["daily_df"]
fourh_df = pair_data["fourh_df"]
entry_df = pair_data["entry_df"]
current_price = pair_data["current_price"]
normalized_context = pair_data["normalized_context"]
allowed_sides = pair_data["allowed_sides"]
results = pair_data["results"]

price_str = _fmt_price(current_price) if current_price is not None else "N/D"
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
            <div class="val">{_fmt_price(entry)}</div>
          </div>
          <div class="trade-cell">
            <div class="lbl">{stop_tip}</div>
            <div class="val stop-val">{_fmt_price(stop)}</div>
          </div>
          <div class="trade-cell">
            <div class="lbl">{tp1_tip}</div>
            <div class="val tp-val">{_fmt_price(tp1)}</div>
          </div>
          <div class="trade-cell">
            <div class="lbl">{tp2_tip}</div>
            <div class="val tp-val">{_fmt_price(tp2)}</div>
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
            f"Breakeven trigger: **{_fmt_price(candidate.get('breakeven_trigger', 0))}** &nbsp;·&nbsp; "
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
        tab_chart, tab_score, tab_ind, tab_signals, tab_cooldown = st.tabs(
            ["📈 Gráfico", "📊 Scoring", "📐 Indicadores", "🔍 Señales", "🕒 Cooldown"]
        )

        # ── TAB 0: Gráfico ────────────────────────────────────
        with tab_chart:
            tf_1d, tf_4h, tf_15m = st.tabs(["1D", "4H", "15m"])
            with tf_1d:
                st.caption("EMA20 / EMA50 / EMA200 — régimen macro usado por la confirmación 1D. Últimos 2 meses.")
                st.plotly_chart(
                    _candlestick_chart(daily_df, ema_spans=(20, 50, 200), tail_bars=60),
                    use_container_width=True, config={"displayModeBar": False},
                    key=f"chart_1d_{side}",
                )
            with tf_4h:
                st.caption("EMA20 / EMA50 / EMA200 + niveles del trade setup (entry/stop/TP1/TP2). Última semana.")
                st.plotly_chart(
                    _candlestick_chart(
                        fourh_df, ema_spans=(20, 50, 200),
                        entry=entry, stop=stop, tp1=tp1, tp2=tp2, tail_bars=42,
                    ),
                    use_container_width=True, config={"displayModeBar": False},
                    key=f"chart_4h_{side}",
                )
            with tf_15m:
                st.caption("EMA20 / EMA50 — capa de timing de entrada. Últimas 24h.")
                st.plotly_chart(
                    _candlestick_chart(entry_df, ema_spans=(20, 50), tail_bars=96),
                    use_container_width=True, config={"displayModeBar": False},
                    key=f"chart_15m_{side}",
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
            s3.metric("ATR 4H", _fmt_price(candidate['atr']), help="Volatilidad 4H en unidades de precio; define distancia de stop/TP.")

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
            | Swing low (4H) | {_fmt_price(candidate['swing_low'])} |
            | Swing high (4H) | {_fmt_price(candidate['swing_high'])} |
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
                      <span>{sent_dt} · score {row['score']:.2f} · RR {row['rr_ratio']:.2f} · entry {_fmt_price(row['entry_price'])} · status {row['status']}</span>
                    </div>
                    """, unsafe_allow_html=True)


tab_long, tab_short = st.tabs(["🟢 LONG", "🔴 SHORT"])
with tab_long:
    render_side(SIDE_LONG)
with tab_short:
    render_side(SIDE_SHORT)
