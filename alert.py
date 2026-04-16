import os
import time
import json
from pathlib import Path
import pandas as pd
import requests

# ── Configuración de Activos ──────────────────────────────────────────────────
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
MIN_SCORE = 3.5

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})

def load_state():
    try:
        return json.loads(Path(STATE_FILE).read_text()) if Path(STATE_FILE).exists() else {}
    except: return {}

def get_data(cg_id):
    # Usamos el endpoint específico para el Plan Demo (api-demo)
    url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc?vs_currency=usd&days=30"
    
    # Ajuste de headers para Plan Demo
    headers = {
        "accept": "application/json",
        "x-cg-demo-api-key": CG_API_KEY
    }
    
    try:
        # Si no hay API KEY, intentamos sin header (límite público)
        response = requests.get(url, headers=headers if CG_API_KEY else {"accept": "application/json"}, timeout=15)
        
        if response.status_code == 401:
            print(f"❌ Error 401 en {cg_id}: API Key no válida o mal configurada.")
            return None
        if response.status_code != 200:
            print(f"❌ Error API {cg_id}: {response.status_code}")
            return None
            
        df = pd.DataFrame(response.json(), columns=['ts', 'Open', 'High', 'Low', 'Close'])
        return df
    except Exception as e:
        print(f"❌ Error de conexión {cg_id}: {e}")
        return None

def evaluate(df, symbol):
    try:
        if len(df) < 20: # CoinGecko devuelve menos velas en OHLC que Yahoo
            return None

        df['ema20'] = df['Close'].ewm(span=20, adjust=False).mean()
        # Nota: La EMA200 es difícil con OHLC de 30 días, usamos EMA50 para diagnóstico
        df['ema50'] = df['Close'].ewm(span=50, adjust=False).mean()
        
        last = df.iloc[-1]
        score, reasons = 0, []

        if last['Close'] > last['ema50']: 
            score += 2.0; reasons.append("Tendencia Alcista (EMA50)")
        if last['Close'] > last['ema20']:
            score += 1.5; reasons.append("Precio > EMA20")

        print(f"🔍 {symbol} analizado: Score={score}")

        return {
            "alert": score >= MIN_SCORE,
            "price": last['Close'],
            "score": score,
            "reasons": reasons
        }
    except Exception as e:
        print(f"❌ Error evaluando {symbol}: {e}")
        return None

def main():
    state = load_state()
    now = time.time()
    any_alert = False

    print(f"🚀 Iniciando escaneo con corrección de Auth...")

    for cg_id, symbol in CRYPTO_IDS.items():
        # Saltamos cooldown para esta prueba
        df = get_data(cg_id)
        if df is not None:
            res = evaluate(df, symbol)
            if res and res["alert"]:
                msg = (f"🚀 *ALERTA {symbol}*\n\n"
                       f"💰 *Precio:* ${res['price']:.4f}\n"
                       f"📊 *Score:* {res['score']}\n"
                       f"📝 *Análisis:* {', '.join(res['reasons'])}")
                send_telegram(msg)
                state[symbol] = now
                any_alert = True
        
        time.sleep(2) # Respetar Rate Limit

    if any_alert:
        Path(STATE_FILE).write_text(json.dumps(state))
    print("🏁 Fin del escaneo.")

if __name__ == "__main__":
    main()
