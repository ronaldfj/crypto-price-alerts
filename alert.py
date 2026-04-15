import os
import sys
import time
import json
from pathlib import Path
import numpy as np
import pandas as pd
import requests

# ── Configuración de Expertos ──────────────────────────────────────────────────
VS_CURRENCY = "usd"
SCAN_LIMIT = 50          # Escaneamos más para filtrar calidad
MIN_VOLUME = 10_000_000  # $10M mínimo para asegurar liquidez
MIN_SCORE = 5.0          # Filtro estricto de alta probabilidad
MIN_RR = 2.0             # Solo trades donde la ganancia dobla el riesgo

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY")

ALERT_COOLDOWN_HOURS = 12
STATE_FILE = "alert_state.json"
EXCLUDE_LIST = ['usdt', 'usdc', 'usds', 'dai', 'usde', 'pyusd', 'fdusd', 'tusd', 'wbtc', 'weth']

# ── Sistema de Alertas y Estado ────────────────────────────────────────────────
def load_state() -> dict:
    try:
        if not Path(STATE_FILE).exists(): return {}
        return json.loads(Path(STATE_FILE).read_text())
    except: return {}

def mark_alerted(symbol: str):
    state = load_state()
    state[symbol] = time.time()
    Path(STATE_FILE).write_text(json.dumps(state))

def already_alerted(symbol: str) -> bool:
    state = load_state()
    last = state.get(symbol)
    return (time.time() - last) < ALERT_COOLDOWN_HOURS * 3600 if last else False

def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except Exception as e: print(f"Error Telegram: {e}")

# ── Motor de Análisis Técnico ─────────────────────────────────────────────────
def add_indicators(df):
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    df["rsi14"] = 100 - (100 / (1 + (gain / (loss + 1e-9))))
    
    df["tr"] = df["close"].diff().abs()
    df["atr"] = df["tr"].rolling(14).mean()
    p_dm = df["close"].diff().clip(lower=0)
    m_dm = (-df["close"].diff()).clip(lower=0)
    df["adx"] = ((p_dm - m_dm).abs() / (p_dm + m_dm + 1e-9)) * 100
    df["adx"] = df["adx"].rolling(14).mean()
    return df

def get_market_data():
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {"vs_currency": VS_CURRENCY, "order": "market_cap_desc", "per_page": SCAN_LIMIT, "x_cg_demo_api_key": COINGECKO_API_KEY}
    res = requests.get(url, params=params, timeout=15).json()
    # MODIFICADO: Ahora extraemos también el nombre completo ('name')
    return [(c['id'], c['symbol'].upper(), c['name']) for c in res if c['symbol'].lower() not in EXCLUDE_LIST and c.get('total_volume', 0) >= MIN_VOLUME][:20]

def evaluate(coin_id, symbol, name):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {"vs_currency": VS_CURRENCY, "days": "90", "x_cg_demo_api_key": COINGECKO_API_KEY}
    data = requests.get(url, params=params, timeout=15).json()
    df = add_indicators(pd.DataFrame(data["prices"], columns=["ts", "close"]))
    
    last, prev = df.iloc[-1], df.iloc[-2]
    score, reasons = 0, []

    if last["close"] > last["ema200"]: score += 1.5; reasons.append("Tendencia Institucional (EMA200)")
    if last["adx"] > 22: score += 1.5; reasons.append(f"Fuerza de Tendencia (ADX: {last['adx']:.1f})")
    if 45 < last["rsi14"] < 65 and last["rsi14"] > prev["rsi14"]: score += 1; reasons.append("Impulso RSI")
    if last["close"] > last["ema20"] and prev["close"] <= prev["ema20"]: score += 1; reasons.append("Cruce EMA20")

    stop = last["close"] - (last["atr"] * 2.5)
    tp = last["close"] + (last["atr"] * 5.5) 
    rr = (tp - last["close"]) / (last["close"] - stop)

    return {
        "symbol": symbol, 
        "name": name, 
        "score": score, 
        "rr": rr, 
        "price": last["close"], 
        "stop": stop, 
        "tp": tp, 
        "alert": score >= MIN_SCORE and rr >= MIN_RR, 
        "reasons": reasons
    }

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print("Iniciando Sistema de Trading Automático...")
    try:
        # MODIFICADO: Desempaquetamos también el nombre
        for coin_id, symbol, name in get_market_data():
            if already_alerted(symbol): continue
            try:
                res = evaluate(coin_id, symbol, name)
                if res["alert"]:
                    # Mensaje mejorado con Nombre Completo + Símbolo
                    msg = (f"🚀 *ALERTA COMPRA: {res['name']} ({res['symbol']})*\n\n"
                           f"💰 *Precio:* {res['price']:.4f}\n"
                           f"📊 *Score:* {res['score']}\n"
                           f"⚖️ *R:R:* {res['rr']:.2f}\n\n"
                           f"🎯 *TARGET (TP):* {res['tp']:.4f}\n"
                           f"🛑 *STOP (SL):* {res['stop']:.4f}\n\n"
                           f"📝 *Análisis:* {', '.join(res['reasons'])}")
                    send_telegram(msg)
                    mark_alerted(symbol)
                    print(f"Alerta enviada: {res['name']} ({symbol})")
                time.sleep(2.2) 
            except Exception as e: print(f"Error {symbol}: {e}")
    except Exception as e: print(f"Error General: {e}")

if __name__ == "__main__":
    main()
