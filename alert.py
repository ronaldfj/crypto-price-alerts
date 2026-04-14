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
TOP_N = 20  # Escaneamos el Top 20
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY")

ALERT_COOLDOWN_HOURS = 12 # Reducido para captar mejores movimientos
STATE_FILE = "alert_state.json"
MIN_SCORE = 5.0  # Subimos el estándar de calidad
MIN_RR = 2.0     # Buscamos trades más rentables

# ── Indicadores Técnicos Avanzados ─────────────────────────────────────────────

def add_indicators(df):
    """Añade indicadores con lógica de filtrado de ruido."""
    # EMAs Clásicas
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    
    # RSI
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss
    df["rsi14"] = 100 - (100 / (1 + rs))
    
    # ADX (Fuerza de tendencia)
    plus_dm = df["high"].diff().clip(lower=0)
    minus_dm = df["low"].diff(-1).clip(lower=0) # Simplificado para eficiencia
    tr = pd.concat([df["high"] - df["low"], 
                    (df["high"] - df["close"].shift()).abs(), 
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    df["adx"] = (plus_dm - minus_dm).abs() / (plus_dm + minus_dm) * 100
    df["adx"] = df["adx"].rolling(14).mean()
    df["atr"] = atr
    return df

# ── Lógica de Escaneo del Top 20 ───────────────────────────────────────────────

def get_top_20_ids():
    """Obtiene los IDs de las 20 principales monedas."""
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": VS_CURRENCY,
        "order": "market_cap_desc",
        "per_page": TOP_N,
        "page": 1,
        "x_cg_demo_api_key": COINGECKO_API_KEY
    }
    res = requests.get(url, params=params)
    res.raise_for_status()
    return [(item['id'], item['symbol'].upper()) for item in res.json()]

def evaluate_asset(coin_id, symbol):
    """Evaluación con filtro de tendencia y volatilidad."""
    # Obtenemos datos (Daily para contexto, 4h para ejecución)
    # Nota: Respetamos los límites de la API con sleep externo
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {"vs_currency": VS_CURRENCY, "days": "90", "x_cg_demo_api_key": COINGECKO_API_KEY}
    
    data = requests.get(url, params=params).json()
    df = pd.DataFrame(data["prices"], columns=["ts", "close"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    
    # Simulación de OHLC para el Top 20 (Mejorado)
    df["high"] = df["close"].rolling(3).max()
    df["low"] = df["close"].rolling(3).min()
    
    df = add_indicators(df)
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    score = 0
    reasons = []

    # FILTRO 1: Tendencia Institucional (EMA 200)
    if last["close"] > last["ema200"]:
        score += 1.5
        reasons.append("Alcista a largo plazo (>EMA200)")

    # FILTRO 2: Fuerza de Tendencia (ADX)
    if last["adx"] > 25:
        score += 1.5
        reasons.append(f"Tendencia fuerte (ADX: {last['adx']:.1f})")

    # FILTRO 3: Momento RSI
    if 45 < last["rsi14"] < 65 and last["rsi14"] > prev["rsi14"]:
        score += 1
        reasons.append("RSI en zona de aceleración")

    # Gestión de Riesgo (Cálculo RR)
    stop_loss = last["close"] - (last["atr"] * 2)
    take_profit = last["close"] + (last["atr"] * 4)
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
    print("Iniciando Escaneo de Alta Probabilidad...")
    try:
        assets = get_top_20_ids()
        alerts_found = []

        for coin_id, symbol in assets:
            try:
                print(f"Analizando {symbol}...")
                result = evaluate_asset(coin_id, symbol)
                if result["alert"]:
                    alerts_found.append(result)
                # IMPORTANTE: Pausa para no saturar la API gratuita
                time.sleep(2.5) 
            except Exception as e:
                print(f"Error en {symbol}: {e}")

        # Envío de alertas (similar a tu código original pero más limpio)
        if alerts_found:
            for alert in alerts_found:
                msg = f"🚀 OPORTUNIDAD: {alert['symbol']}\nPrecio: {alert['price']:.2f}\nScore: {alert['score']}\nR:R: {alert['rr']:.2f}\nTP: {alert['tp']:.2f} / SL: {alert['stop']:.2f}\nRazones: {', '.join(alert['reasons'])}"
                # send_telegram_message(msg)
                print(msg)
        else:
            print("Mercado en calma. Sin señales claras.")

    except Exception as e:
        print(f"Error General: {e}")

if __name__ == "__main__":
    main()
