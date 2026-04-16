import os
import time
import json
from pathlib import Path
import pandas as pd
import requests

# ── Configuración de Activos (IDs de CoinGecko) ──────────────────────────────
CRYPTO_IDS = {
    'bitcoin': 'BTC', 'ethereum': 'ETH', 'binancecoin': 'BNB', 
    'solana': 'SOL', 'ripple': 'XRP', 'cardano': 'ADA', 
    'avalanche-2': 'AVAX', 'polkadot': 'DOT', 'chainlink': 'LINK', 
    'polygon-ecosystem-token': 'POL', 'litecoin': 'LTC', 'near': 'NEAR', 
    'sui': 'SUI', 'fetch-ai': 'FET', 'render-token': 'RENDER', 
    'bittensor': 'TAO', 'injective-protocol': 'INJ', 'blockstack': 'STX', 
    'pepe': 'PEPE', 'shiba-inu': 'SHIB'
}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CG_API_KEY = os.getenv("COINGECKO_API_KEY")
STATE_FILE = "alert_state.json"
MIN_SCORE = 2

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})

def load_state():
    try:
        return json.loads(Path(STATE_FILE).read_text()) if Path(STATE_FILE).exists() else {}
    except: return {}

def get_data(cg_id):
    url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc?vs_currency=usd&days=30"
    headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200: return None
        df = pd.DataFrame(response.json(), columns=['ts', 'Open', 'High', 'Low', 'Close'])
        return df
    except: return None

def evaluate(df, symbol):
    # Indicadores Técnicos
    df['ema20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['ema200'] = df['Close'].ewm(span=200, adjust=False).mean()
    
    # RSI
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    df['rsi'] = 100 - (100 / (1 + (gain / (loss + 1e-9))))
    
    # ATR (Volatilidad para Stop/Target)
    df['atr'] = df['Close'].diff().abs().rolling(14).mean()

    last, prev = df.iloc[-1], df.iloc[-2]
    score, reasons = 0, []

    # Sistema de Puntuación
    if last['Close'] > last['ema200']: 
        score += 2.0; reasons.append("Tendencia Institucional (EMA200)")
    if 45 < last['rsi'] < 65 and last['rsi'] > prev['rsi']: 
        score += 1.5; reasons.append("Impulso RSI")
    if last['Close'] > last['ema20'] and prev['Close'] <= prev['ema20']:
        score += 1.5; reasons.append("Cruce EMA20")

    # Gestión de Riesgo (Cálculo de Target y Stop)
    atr = last['atr'] if last['atr'] > 0 else (last['Close'] * 0.02)
    stop = last['Close'] - (atr * 2.0)
    tp = last['Close'] + (atr * 4.4) # R:R de 2.2
    rr = (tp - last['Close']) / (last['Close'] - stop)

    return {
        "alert": score >= MIN_SCORE,
        "price": last['Close'],
        "score": score,
        "rr": rr,
        "tp": tp,
        "stop": stop,
        "reasons": reasons
    }

def main():
    state = load_state()
    now = time.time()
    any_alert = False

    for cg_id, symbol in CRYPTO_IDS.items():
        if (now - state.get(symbol, 0)) < 14400: continue 

        df = get_data(cg_id)
        if df is not None and len(df) > 50:
            res = evaluate(df, symbol)
            if res["alert"]:
                msg = (f"🚀 *ALERTA COMPRA: {symbol}*\n\n"
                       f"💰 *Precio:* ${res['price']:.4f}\n"
                       f"📊 *Score:* {res['score']}\n"
                       f"⚖️ *R:R:* {res['rr']:.2f}\n"
                       f"🎯 *TARGET (TP):* ${res['tp']:.4f}\n"
                       f"🛑 *STOP (SL):* ${res['stop']:.4f}\n\n"
                       f"📝 *Análisis:* {', '.join(res['reasons'])}")
                send_telegram(msg)
                state[symbol] = now
                any_alert = True
        time.sleep(1.5) 

    if any_alert:
        Path(STATE_FILE).write_text(json.dumps(state))

if __name__ == "__main__":
    main()
