import os
import time
import json
from pathlib import Path
import pandas as pd
import yfinance as yf
import requests

# ── Configuración de Activos ──────────────────────────────────────────────────
# Lista depurada con tickers compatibles con Yahoo Finance
CRYPTO_SYMBOLS = [
    'BTC-USD', 'ETH-USD', 'BNB-USD', 'SOL-USD', 'XRP-USD', 
    'ADA-USD', 'AVAX-USD', 'DOT-USD', 'LINK-USD', 'POL-USD',
    'LTC-USD', 'NEAR-USD', 'SUI-USD', 'FET-USD', 'RENDER-USD', 
    'TAO-USD', 'INJ-USD', 'STX-USD', 'PEPE-USD', 'SHIB-USD'
]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = "alert_state.json"  # Sincronizado con el flujo de GitHub Actions [cite: 1]
MIN_SCORE = 5.0  
MIN_RR = 2.0

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except Exception as e: print(f"Error Telegram: {e}")

def load_state():
    try:
        p = Path(STATE_FILE)
        return json.loads(p.read_text()) if p.exists() else {}
    except: return {}

def save_state(state):
    try:
        Path(STATE_FILE).write_text(json.dumps(state, indent=2))
    except Exception as e: print(f"Error guardando estado: {e}")

def evaluate_crypto(symbol):
    try:
        # Descarga de 60 días para asegurar cálculo de EMA200
        df = yf.Ticker(symbol).history(period="60d", interval="1h")
        if df is None or df.empty or len(df) < 200: return None

        # Indicadores Técnicos
        df['ema20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['ema200'] = df['Close'].ewm(span=200, adjust=False).mean()
        
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / (loss + 1e-9))))
        
        df['atr'] = df['Close'].diff().abs().rolling(14).mean()

        last, prev = df.iloc[-1], df.iloc[-2]
        score, reasons = 0, []

        # Lógica de señales
        if last['Close'] > last['ema200']: 
            score += 2.0; reasons.append("Tendencia Alcista (>EMA200)")
        if 45 < last['rsi'] < 65 and last['rsi'] > prev['rsi']: 
            score += 1.5; reasons.append(f"RSI Momentum ({last['rsi']:.1f})")
        if last['Close'] > last['ema20'] and prev['Close'] <= prev['ema20']:
            score += 1.5; reasons.append("Cruce EMA20")

        # Gestión de Riesgo
        atr = last['atr'] if last['atr'] > 0 else (last['Close'] * 0.02)
        stop = last['Close'] - (atr * 2.0)
        tp = last['Close'] + (atr * 4.0)
        rr = (tp - last['Close']) / (last['Close'] - stop)

        return {
            "symbol": symbol.replace("-USD", ""), "score": score, "rr": rr, 
            "price": last['Close'], "stop": stop, "tp": tp, 
            "alert": score >= MIN_SCORE and rr >= MIN_RR, "reasons": reasons
        }
    except: return None

def main():
    state = load_state()
    now = time.time()
    print(f"🚀 Iniciando escaneo de {len(CRYPTO_SYMBOLS)} activos...")
    
    any_alert = False
    for symbol in CRYPTO_SYMBOLS:
        last_alert = state.get(symbol, 0)
        if (now - last_alert) < 14400: # Cooldown 4h
            continue
        
        res = evaluate_crypto(symbol)
        if res and res["alert"]:
            msg = (f"⚡ *ALERTA:* {res['symbol']}\n"
                   f"💰 Precio: ${res['price']:.4f}\n"
                   f"📊 Score: {res['score']}\n"
                   f"⚖️ RR: {res['rr']:.2f}\n"
                   f"🎯 TP: ${res['tp']:.4f} | 🛑 SL: ${res['stop']:.4f}\n"
                   f"📝 {', '.join(res['reasons'])}")
            send_telegram(msg)
            state[symbol] = now
            any_alert = True
            print(f"✅ Alerta enviada: {res['symbol']}")
        
        time.sleep(2)

    if any_alert:
        save_state(state)

if __name__ == "__main__":
    main()
