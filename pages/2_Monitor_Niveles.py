"""
pages/2_Monitor_Niveles.py — Embebe btc_level_monitor.html dentro del app
Streamlit. Herramienta standalone e independiente del motor del bot: no
usa data_source.py/Bybit/OKX ni alerts_state.db, trae precio en vivo de
CoinGecko/Kraken/Coinbase directo desde el navegador y persiste el
historial en localStorage (ver CLAUDE.md).
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(
    page_title="Crypto Sentinel — Monitor de niveles",
    page_icon="🪙",
    layout="wide",
    initial_sidebar_state="expanded",
)

html_path = Path(__file__).parent.parent / "btc_level_monitor.html"
components.html(html_path.read_text(encoding="utf-8"), height=1400, scrolling=True)
