import os
import time
import json
from pathlib import Path
import pandas as pd
import yfinance as yf
import requests

# ── Configuración de Activos (FILTRADO PARA BINANCE) ──────────────────────────
# ── Lista Maestra de Binance (Formato compatible con Yahoo) ──────────────────
CRYPTO_SYMBOLS = [
    'BTC-USD', 'ETH-USD', 'BNB-USD', 'SOL-USD', 'XRP-USD', 
    'ADA-USD', 'AVAX-USD', 'DOT-USD', 'LINK-USD', 'POL-USD',   # MATIC ahora es POL
    'LTC-USD', 'NEAR-USD', 'SUI1-USD', 'FET-USD', 'RENDER-USD', # SUI1 y RENDER corregidos
    'TAO1-USD', 'INJ-USD', 'STX1-USD', 'PEPE1-USD', 'SHIB-USD'  # Formatos específicos
]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = "crypto_state.json"
MIN_SCORE = 5.0  
MIN_RR = 2.0

# ── Sistema de Alertas y Telegram ─────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except Exception as e: print(f"Error Telegram: {e}")

def load_state():
    try:
        if not Path(STATE_FILE).exists(): return {}
        return json.loads(Path(STATE_FILE).read_text())
    except: return {}

def mark_alerted(symbol):
    state = load_state()
    state[symbol] = time.time()
    Path(STATE_FILE).write_text(json.dumps(state))

# ── Análisis Técnico ──────────────────────────────────────────────────────────
def evaluate_crypto(symbol):
    # Usamos una sesión simple para evitar bloqueos
    ticker = yf.Ticker(symbol)
    
    try:
        # Reducimos a 30d para asegurar que encuentre datos frescos sin error
        df = ticker.history(period="30d", interval="1h")
    except Exception as e:
        print(f"⚠️ Salto en {symbol}: {e}")
        return None
    
    if df is None or df.empty or len(df) < 100: 
        return None

    # Indicadores
    df['ema20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['ema200'] = df['Close'].ewm(span=200, adjust=False).mean()
    
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    df['rsi'] = 100 - (100 / (1 + (gain / (loss + 1e-9))))
    
    # ADX
    df['tr'] = df['Close'].diff().abs()
    df['atr'] = df['tr'].rolling(14).mean()
    p_dm = df['Close'].diff().clip(lower=0)
    m_dm = (-df['Close'].diff()).clip(lower=0)
    df['adx'] = ((p_dm - m_dm).abs() / (p_dm + m_dm + 1e-9)) * 100
    df['adx'] = df['adx'].rolling(14).mean()

    last = df.iloc[-1]
    prev = df.iloc[-2]
    score, reasons = 0, []

    # Lógica de señales
    if last['Close'] > last['ema200']: 
        score += 2.0; reasons.append("Tendencia Alcista (>EMA200)")
    if last['adx'] > 25: 
        score += 1.5; reasons.append(f"Fuerza ADX: {last['adx']:.1f}")
    if 45 < last['rsi'] < 65 and last['rsi'] > prev['rsi']: 
        score += 1.0; reasons.append("RSI con Momento")
    if last['Close'] > last['ema20'] and prev['Close'] <= prev['ema20']:
        score += 1.0; reasons.append("Cruce EMA20")

    # Gestión de Riesgo (ATR)
    current_atr = last['atr'] if last['atr'] > 0 else (last['Close'] * 0.03)
    stop = last['Close'] - (current_atr * 1.5)
    tp = last['Close'] + (current_atr * 3.5)
    rr = (tp - last['Close']) / (last['Close'] - stop)

    return {
        "symbol": symbol.replace("-USD", ""), 
        "score": score, "rr": rr, "price": last['Close'], 
        "stop": stop, "tp": tp, 
        "alert": score >= MIN_SCORE and rr >= MIN_RR, 
        "reasons": reasons
    }

def main():
    state = load_state()
    print(f"🚀 Escaneando {len(CRYPTO_SYMBOLS)} activos en Binance...")
    
    for symbol in CRYPTO_SYMBOLS:
        # Cooldown de 4 horas para cripto (mercado más rápido)
        last_alert = state.get(symbol, 0)
        if (time.time() - last_alert) < 14400: continue
        
        try:
            res = evaluate_crypto(symbol)
            if res and res["alert"]:
                msg = (f"⚡ *ALERTA CRIPTO (Binance): {res['symbol']}*\n\n"
                       f"💰 *Precio:* ${res['price']:.4f}\n"
                       f"📊 *Score:* {res['score']}\n"
                       f"⚖️ *R:R:* {res['rr']:.2f}\n\n"
                       f"🎯 *TARGET:* ${res['tp']:.4f}\n"
                       f"🛑 *STOP:* ${res['stop']:.4f}\n\n"
                       f"📝 *Análisis:* {', '.join(res['reasons'])}")
                send_telegram(msg)
                mark_alerted(symbol)
                print(f"✅ Alerta enviada: {res['symbol']}")
            time.sleep(1) # Delay discreto
        except Exception as e:
            print(f"❌ Error en {symbol}: {e}")

if __name__ == "__main__":
    main()
