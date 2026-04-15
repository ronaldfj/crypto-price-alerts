import os
import time
import json
from pathlib import Path
import pandas as pd
import yfinance as yf
import requests

# ── Configuración Maestra ──────────────────────────────────────────────────
# Lista optimizada: Eliminados sufijos '1' que causan error 404 en Yahoo
CRYPTO_SYMBOLS = [
    'BTC-USD', 'ETH-USD', 'BNB-USD', 'SOL-USD', 'XRP-USD', 
    'ADA-USD', 'AVAX-USD', 'DOT-USD', 'LINK-USD', 'POL-USD', 
    'LTC-USD', 'NEAR-USD', 'SUI-USD', 'FET-USD', 'RENDER-USD', 
    'TAO-USD', 'INJ-USD', 'STX-USD', 'PEPE-USD', 'SHIB-USD'
]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = "alert_state.json"  # <── SINCRONIZADO CON EL YAML
MIN_SCORE = 4.5 
MIN_RR = 1.8

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
        # Descarga robusta: period 30d es ideal para indicadores de 1h
        df = yf.Ticker(symbol).history(period="30d", interval="1h")
        if df is None or df.empty or len(df) < 50: return None

        # Indicadores
        df['ema20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['ema200'] = df['Close'].ewm(span=200, adjust=False).mean()
        
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / (loss + 1e-9))))
        
        df['tr'] = df['Close'].diff().abs()
        df['atr'] = df['tr'].rolling(14).mean()

        last, prev = df.iloc[-1], df.iloc[-2]
        score, reasons = 0, []

        if last['Close'] > last['ema200']: 
            score += 2.0; reasons.append("Tendencia Alcista (>EMA200)")
        if 40 < last['rsi'] < 65 and last['rsi'] > prev['rsi']: 
            score += 1.5; reasons.append(f"RSI Momentum ({last['rsi']:.1f})")
        if last['Close'] > last['ema20'] and prev['Close'] <= prev['ema20']:
            score += 1.0; reasons.append("Cruce EMA20")

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
        # Cooldown de 4 horas (14400 seg)
        if (now - last_alert) < 14400:
            print(f"⏳ {symbol} en cooldown.")
            continue
        
        res = evaluate_crypto(symbol)
        if res and res["alert"]:
            msg = (f"✅ *ALERTA:* {res['symbol']}\n"
                   f"💰 Precio: ${res['price']:.4f}\n"
                   f"📊 Score: {res['score']}\n"
                   f"⚖️ RR: {res['rr']:.2f}\n"
                   f"🎯 TP: ${res['tp']:.4f} | 🛑 SL: ${res['stop']:.4f}\n"
                   f"📝 {', '.join(res['reasons'])}")
            
            # Envío a Telegram
            if TELEGRAM_BOT_TOKEN:
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})
            
            state[symbol] = now
            any_alert = True
            print(f"✅ Alerta enviada: {res['symbol']}")
        
        time.sleep(2) # Evitar bloqueos de Yahoo

    if any_alert:
        save_state(state)

if __name__ == "__main__":
    main()
