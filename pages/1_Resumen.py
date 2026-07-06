"""
pages/1_Resumen.py — Tablero comparativo de todos los pares rastreados.
Corre el mismo motor (1D macro + 4H setup + 15m timing) sobre cada símbolo
y arma una tabla para escanear visualmente cuál tiene el mejor setup,
LONG o SHORT. Clic en una fila → abre ese par en el Inspector (detalle
completo, igual que si se hubiera elegido a mano). Sin Telegram, sin
escribir en alerts_state.db.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import streamlit as st

from alert import MIN_SCORE, MIN_RR, MIN_ADX, SIDE_LONG, SIDE_SHORT, asset_group
from sentinel_shared import (
    SYMBOLS,
    pair_label,
    fmt_price,
    get_btc_dominance,
    get_context,
    evaluate_pair,
    inject_css,
    render_market_snapshot,
)

st.set_page_config(
    page_title="Crypto Sentinel — Resumen",
    page_icon="🪙",
    layout="wide",
    initial_sidebar_state="expanded",
)

inject_css()

with st.sidebar:
    st.markdown("### Mercado")
    if st.button("Refrescar", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    with st.spinner(""):
        btc_dominance = get_btc_dominance()
        market_context = get_context()

    render_market_snapshot(btc_dominance, market_context)

    st.divider()
    st.caption(
        f"Score mín: **{MIN_SCORE}** · RR mín: **{MIN_RR}** · ADX mín: **{MIN_ADX}**\n\n"
        "Mismos thresholds y motor que el bot en vivo."
    )

st.markdown("## Crypto Sentinel — Resumen de pares")
st.caption(
    "Un vistazo a los activos rastreados: score, R:R, ADX y régimen por timeframe, "
    "para LONG y SHORT. Clic en una fila para ver el detalle completo en el Inspector."
)


def _side_summary(pair_data: Dict[str, Any], side: str) -> Dict[str, Any]:
    data = pair_data["results"].get(side)
    if data is None:
        return {"score": None, "rr": None, "estado": "N/D — histórico insuficiente"}

    candidate = data["candidate"]
    macro_eval = data["macro"]
    side_ok = side in pair_data["allowed_sides"]

    if candidate["alert"]:
        estado = f"🔥 ALERTA ({candidate['alert_profile']})"
    elif not side_ok:
        estado = "🚫 Lado no permitido"
    elif macro_eval.get("hard_block"):
        estado = "🚫 Bloqueo manual"
    else:
        estado = "⚠️ Sin señal"

    return {"score": candidate["score"], "rr": candidate["rr_ratio"], "estado": estado}


rows: list[Dict[str, Any]] = []
progress = st.progress(0.0, text="Evaluando pares...")
for i, symbol in enumerate(SYMBOLS):
    progress.progress((i) / len(SYMBOLS), text=f"Evaluando {pair_label(symbol)}… ({i + 1}/{len(SYMBOLS)})")
    pair_data = evaluate_pair(symbol, market_context, btc_dominance)

    if pair_data is None:
        rows.append({
            "Symbol": symbol,
            "Par": pair_label(symbol),
            "Grupo": asset_group(symbol),
            "Precio": "N/D",
            "1D": "—",
            "4H": "—",
            "ADX": None,
            "Score LONG": None, "RR LONG": None, "Estado LONG": "N/D — sin datos",
            "Score SHORT": None, "RR SHORT": None, "Estado SHORT": "N/D — sin datos",
        })
        continue

    long_summary = _side_summary(pair_data, SIDE_LONG)
    short_summary = _side_summary(pair_data, SIDE_SHORT)

    any_side_data = pair_data["results"].get(SIDE_LONG) or pair_data["results"].get(SIDE_SHORT)
    macro_regime = any_side_data["macro"]["regime"] if any_side_data else "—"
    setup_regime = any_side_data["candidate"]["regime"] if any_side_data else "—"
    adx_4h = any_side_data["candidate"]["adx"] if any_side_data else None

    current_price = pair_data["current_price"]
    rows.append({
        "Symbol": symbol,
        "Par": pair_label(symbol),
        "Grupo": asset_group(symbol),
        "Precio": fmt_price(current_price) if current_price is not None else "N/D",
        "1D": macro_regime,
        "4H": setup_regime,
        "ADX": round(adx_4h, 1) if adx_4h is not None else None,
        "Score LONG": long_summary["score"], "RR LONG": long_summary["rr"], "Estado LONG": long_summary["estado"],
        "Score SHORT": short_summary["score"], "RR SHORT": short_summary["rr"], "Estado SHORT": short_summary["estado"],
    })

progress.empty()

df = pd.DataFrame(rows)
df["_best_score"] = pd.to_numeric(df[["Score LONG", "Score SHORT"]].max(axis=1), errors="coerce")
df = df.sort_values("_best_score", ascending=False, na_position="last").drop(columns=["_best_score"]).reset_index(drop=True)

only_alerts = st.checkbox("🔥 Solo mostrar pares con alerta activa")
display_df = df
if only_alerts:
    display_df = df[df["Estado LONG"].str.contains("ALERTA") | df["Estado SHORT"].str.contains("ALERTA")].reset_index(drop=True)

if display_df.empty:
    st.info("Ningún par tiene alerta activa en esta corrida.")
    st.stop()

event = st.dataframe(
    display_df.drop(columns=["Symbol"]),
    use_container_width=True,
    hide_index=True,
    height=(len(display_df) + 1) * 36 + 3,
    column_config={
        "Score LONG": st.column_config.NumberColumn(format="%.2f"),
        "Score SHORT": st.column_config.NumberColumn(format="%.2f"),
        "RR LONG": st.column_config.NumberColumn(format="%.2f×"),
        "RR SHORT": st.column_config.NumberColumn(format="%.2f×"),
        "ADX": st.column_config.NumberColumn(format="%.1f"),
    },
    on_select="rerun",
    selection_mode="single-row",
    key="resumen_table",
)

st.caption("💡 Clic en una fila para abrir ese par en el Inspector con el detalle completo (1D/4H/15m, scoring, señales, gráfico).")

selected_rows = event.selection["rows"] if event.selection else []
if selected_rows:
    selected_symbol: str = display_df.iloc[selected_rows[0]]["Symbol"]
    st.session_state["selected_symbol"] = selected_symbol
    st.switch_page("inspector.py")
