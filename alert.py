import os
import time
import json
from pathlib import Path
import pandas as pd
import requests

# ── Configuración de Activos (IDs NATIVOS DE COINGECKO) ──────────────────────
# Estos IDs son inmutables y no fallan como los tickers de Yahoo
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
STATE_FILE = "alert_state.json" # Sincronizado con .yml
MIN_SCORE = 1

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})

def load_state():
    try:
        return json.loads(Path(STATE_FILE).read_text()) if Path(STATE_FILE).exists() else {}
    except: return {}

def get_data(cg_id):
    # Uso de la API de CoinGecko para obtener velas (OHLC)
    url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc?vs_currency=usd&days=30"
    headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200: return None
        df = pd.DataFrame(response.json(), columns=['ts', 'Open', 'High', 'Low', 'Close'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        return df
    except: return None

def evaluate(df, symbol):
    # Indicadores Técnicos
    df['ema20'] = df['Close'].ewm(span=20).mean()
    df['ema200'] = df['Close'].ewm(span=200).mean()
    
    last, prev = df.iloc[-1], df.iloc[-2]
    score, reasons = 0, []

    if last['Close'] > last['ema200']: 
        score += 2.0; reasons.append("Tendencia Alcista")
    if last['Close'] > last['ema20'] and prev['Close'] <= prev['ema20']:
        score += 2.0; reasons.append("Cruce EMA20")

    return {"alert": score >= MIN_SCORE, "price": last['Close'], "reasons": reasons}

def main():
    state = load_state()
    now = time.time()
    any_alert = False

    for cg_id, symbol in CRYPTO_IDS.items():
        if (now - state.get(symbol, 0)) < 14400: continue # Cooldown 4h

        df = get_data(cg_id)
        if df is not None:
            res = evaluate(df, symbol)
            if res["alert"]:
                msg = f"🚀 *ALERTA {symbol}*\n💰 Precio: ${res['price']}\n📝 {', '.join(res['reasons'])}"
                send_telegram(msg)
                state[symbol] = now
                any_alert = True
        time.sleep(1.5) # Respetar Rate Limit de CoinGecko

    if any_alert:
        Path(STATE_FILE).write_text(json.dumps(state))

if __name__ == "__main__":
    main()
