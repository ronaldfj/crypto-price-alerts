import os
import sys
import time
import json
from pathlib import Path
import numpy as np
import pandas as pd
import requests

# ── Configuración ──────────────────────────────────────────────────────────────
VS_CURRENCY = "usd"
TOP_N = 25  
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY")

ALERT_COOLDOWN_HOURS = 12
STATE_FILE = "alert_state.json"
MIN_SCORE = 5.0  
MIN_RR = 2.0     

EXCLUDE_LIST = ['usdt', 'usdc', 'usds', 'dai', 'usde', 'pyusd', 'fdusd', 'tusd', 'wbtc', 'weth']

# ── Manejo de Estado (Para no repetir alertas) ─────────────────────────────────
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
    if not last: return False
    return (time.time() - last) < ALERT_COOLDOWN_HOURS * 3600

# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram_message(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Error: Faltan credenciales de Telegram")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=15).raise_for_status()
    except Exception as e:
        print(f"Error enviando a Telegram: {e}")

# ── Lógica Técnica ─────────────────────────────────────────────────────────────
def add_indicators(df):
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    df["rsi14"] = 100 - (100 / (1 + (gain / (loss + 1e-9))))
    
    # ADX y ATR simplificados para estabilidad
    df["tr"] = df["close"].diff().abs()
    df["atr"] = df["tr"].rolling(14).mean()
    plus_dm = df["close"].diff().clip(lower=0)
    minus_dm = (-df["close"].diff()).clip(lower=0)
    df["adx"] = ( (plus_dm - minus_dm).abs() / (plus_dm + minus_dm + 1e-9) ) * 100
    df["adx"] = df["adx"].rolling(14).mean()
    return df

def get_filtered_top_assets():
    """Obtiene el top pero ignora stablecoins y activos con poco volumen."""
    url = "https://api.coingecko.com/api/v3/coins/markets"
    MIN_VOLUME_THRESHOLD = 10_000_000  # $10 millones mínimo
    
    params = {
        "vs_currency": VS_CURRENCY,
        "order": "market_cap_desc",
        "per_page": 50, # Pedimos más para tener de dónde filtrar
        "x_cg_demo_api_key": COINGECKO_API_KEY
    }
    res = requests.get(url, params=params, timeout=15)
    data = res.json()
    
    valid_assets = []
    for item in data:
        symbol = item['symbol'].lower()
        volume = item.get('total_volume', 0)
        
        # Filtro: No estable, No envuelta (Wrapped), y Volumen suficiente
        if symbol not in EXCLUDE_LIST and volume >= MIN_VOLUME_THRESHOLD:
            valid_assets.append((item['id'], item['symbol'].upper()))
            
    return valid_assets[:20] # Retornamos los 20 mejores que cumplen todo

def evaluate_asset(coin_id, symbol):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {"vs_currency": VS_CURRENCY, "days": "90", "x_cg_demo_api_key": COINGECKO_API_KEY}
    data = requests.get(url, params=params, timeout=15).json()
    
    df = pd.DataFrame(data["prices"], columns=["ts", "close"])
    df = add_indicators(df)
    last, prev = df.iloc[-1], df.iloc[-2]
    
    score, reasons = 0, []
    if last["close"] > last["ema200"]: score += 1.5; reasons.append("Tendencia Institucional (EMA200)")
    if last["adx"] > 22: score += 1.5; reasons.append(f"Fuerza ADX ({last['adx']:.1f})")
    if 45 < last["rsi14"] < 65 and last["rsi14"] > prev["rsi14"]: score += 1; reasons.append("Momento RSI")
    if last["close"] > last["ema20"] and prev["close"] <= prev["ema20"]: score += 1; reasons.append("Cruce EMA20")

    stop = last["close"] - (last["atr"] * 2.5)
    tp = last["close"] + (last["atr"] * 5)
    rr = (tp - last["close"]) / (last["close"] - stop) if (last["close"] - stop) != 0 else 0

    return {"symbol": symbol, "score": score, "rr": rr, "price": last["close"], "stop": stop, "tp": tp, 
            "alert": score >= MIN_SCORE and rr >= MIN_RR, "reasons": reasons}

# ── Ejecución Principal ────────────────────────────────────────────────────────
def main():
    print("Iniciando Escaneo con Alertas de Telegram...")
    try:
        assets = get_filtered_top_assets()
        for coin_id, symbol in assets:
            if already_alerted(symbol): continue
            
            try:
                res = evaluate_asset(coin_id, symbol)
                if res["alert"]:
                    msg = (f"🚀 *ALERTA DE TRADING: {res['symbol']}*\n\n"
                           f"💰 *Precio:* {res['price']:.4f}\n"
                           f"📊 *Score:* {res['score']}\n"
                           f"⚖️ *R:R:* {res['rr']:.2f}\n\n"
                           f"🎯 *TP:* {res['tp']:.4f}\n"
                           f"🛑 *SL:* {res['stop']:.4f}\n\n"
                           f"📝 *Razones:* {', '.join(res['reasons'])}")
                    send_telegram_message(msg)
                    mark_alerted(symbol)
                    print(f"Alerta enviada para {symbol}")
                time.sleep(2.2)
            except Exception as e: print(f"Error en {symbol}: {e}")
    except Exception as e: print(f"Error General: {e}")

if __name__ == "__main__":
    main()
