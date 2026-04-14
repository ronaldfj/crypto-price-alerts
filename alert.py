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
TOP_N = 25  # Pedimos 25 para filtrar estables y quedarnos con ~20 reales
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY")

ALERT_COOLDOWN_HOURS = 12
STATE_FILE = "alert_state.json"
MIN_SCORE = 5.0  
MIN_RR = 2.0     

# Lista de exclusión manual de Stablecoins y Wrapped tokens
EXCLUDE_LIST = ['usdt', 'usdc', 'usds', 'dai', 'usde', 'pyusd', 'fdusd', 'tusd', 'wbtc', 'weth']

# ── Funciones Técnicas ─────────────────────────────────────────────────────────

def add_indicators(df):
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / (loss + 1e-9)
    df["rsi14"] = 100 - (100 / (1 + rs))
    
    # ADX Simple
    plus_dm = df["close"].diff().clip(lower=0)
    minus_dm = (-df["close"].diff()).clip(lower=0)
    tr = df["close"].diff().abs() # Aproximación para evitar falta de High/Low real
    df["adx"] = ( (plus_dm - minus_dm).abs() / (plus_dm + minus_dm + 1e-9) ) * 100
    df["adx"] = df["adx"].rolling(14).mean()
    df["atr"] = tr.rolling(14).mean()
    return df

def get_filtered_top_assets():
    """Obtiene el top pero ignora stablecoins."""
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": VS_CURRENCY,
        "order": "market_cap_desc",
        "per_page": TOP_N,
        "page": 1,
        "x_cg_demo_api_key": COINGECKO_API_KEY
    }
    res = requests.get(url, params=params, timeout=15)
    res.raise_for_status()
    
    valid_assets = []
    for item in res.json():
        symbol = item['symbol'].lower()
        if symbol not in EXCLUDE_LIST:
            valid_assets.append((item['id'], item['symbol'].upper()))
            
    return valid_assets[:20] # Nos quedamos con los 20 mejores volátiles

def evaluate_asset(coin_id, symbol):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {"vs_currency": VS_CURRENCY, "days": "90", "x_cg_demo_api_key": COINGECKO_API_KEY}
    
    res = requests.get(url, params=params, timeout=15)
    data = res.json()
    df = pd.DataFrame(data["prices"], columns=["ts", "close"])
    df = add_indicators(df)
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    score = 0
    reasons = []

    if last["close"] > last["ema200"]:
        score += 1.5
        reasons.append("Tendencia Institucional (+) ")
    if last["adx"] > 22:
        score += 1.5
        reasons.append(f"Fuerza ADX: {last['adx']:.1f} (+)")
    if 45 < last["rsi14"] < 65 and last["rsi14"] > prev["rsi14"]:
        score += 1
        reasons.append("Momento RSI (+)")
    if last["close"] > last["ema20"] and prev["close"] <= prev["ema20"]:
        score += 1
        reasons.append("Cruce al alza EMA20 (+)")

    stop_loss = last["close"] - (last["atr"] * 2.5)
    take_profit = last["close"] + (last["atr"] * 5)
    rr = (take_profit - last["close"]) / (last["close"] - stop_loss) if (last["close"] - stop_loss) != 0 else 0

    return {
        "symbol": symbol,
        "score": score,
        "rr": rr,
        "price": last["close"],
        "stop": stop_loss,
        "tp": take_profit,
        "alert": score >= MIN_SCORE and rr >= MIN_RR,
        "reasons": reasons
    }

def main():
    print(f"Iniciando Escaneo Filtrado (Excluyendo Stablecoins)...")
    try:
        assets = get_filtered_top_assets()
        alerts_found = []

        for coin_id, symbol in assets:
            try:
                print(f"Analizando {symbol}...")
                result = evaluate_asset(coin_id, symbol)
                if result["alert"]:
                    alerts_found.append(result)
                time.sleep(2.2) # Margen de seguridad para API gratuita
            except Exception as e:
                print(f"Error en {symbol}: {e}")

        if alerts_found:
            for alert in alerts_found:
                # Aquí iría tu send_telegram_message original
                print(f"🚀 ALERTA: {alert['symbol']} | Score: {alert['score']} | R:R: {alert['rr']:.2f}")
        else:
            print("Escaneo finalizado. Sin señales de alta probabilidad por ahora.")

    except Exception as e:
        print(f"Error General: {e}")

if __name__ == "__main__":
    main()
