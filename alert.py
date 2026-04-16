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
MIN_SCORE = 3.5  # Filtro de calidad

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except Exception as e:
        print(f"Error enviando a Telegram: {e}")

def load_state():
    try:
        return json.loads(Path(STATE_FILE).read_text()) if Path(STATE_FILE).exists() else {}
    except: return {}

def get_data(cg_id):
    url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc?vs_currency=usd&days=30"
    headers = {"accept": "application/json", "x-cg-demo-api-key": CG_API_KEY}
    try:
        response = requests.get(url, headers=headers if CG_API_KEY else {"accept": "application/json"}, timeout=15)
        if response.status_code != 200: return None
        return pd.DataFrame(response.json(), columns=['ts', 'Open', 'High', 'Low', 'Close'])
    except: return None

def evaluate(df, symbol):
    try:
        if len(df) < 20: return None

        # --- Indicadores Técnicos ---
        df['ema20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['ema50'] = df['Close'].ewm(span=50, adjust=False).mean()
        
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / (loss + 1e-9))))
        df['atr'] = df['Close'].diff().abs().rolling(14).mean()

        last = df.iloc[-1]
        score, reasons = 0, []

        # --- Lógica de Puntuación ---
        if last['Close'] > last['ema50']: 
            score += 2.0; reasons.append("Tendencia Alcista (EMA50)")
        if last['Close'] > last['ema20']:
            score += 1.5; reasons.append("Precio > EMA20")
        if 40 < last['rsi'] < 70:
            score += 1.0; reasons.append(f"RSI Estable ({last['rsi']:.1f})")

        # --- Gestión de Riesgo (TP/SL) ---
        atr_val = last['atr'] if last['atr'] > 0 else (last['Close'] * 0.02)
        stop_price = last['Close'] - (atr_val * 2.0)
        tp_price = last['Close'] + (atr_val * 4.4)
        rr_ratio = (tp_price - last['Close']) / (last['Close'] - stop_price)

        print(f"🔍 {symbol} analizado: Score={score}")

        # La clave 'stop' y 'tp' ahora coinciden con lo que pide el mensaje
        return {
            "alert": score >= MIN_SCORE,
            "price": last['Close'],
            "score": score,
            "tp": tp_price, 
            "stop": stop_price, 
            "rr": rr_ratio,
            "reasons": reasons
        }
    except Exception as e:
        print(f"❌ Error evaluando {symbol}: {e}")
        return None

def main():
    state = load_state()
    now = time.time()
    any_alert = False

    print(f"🚀 Iniciando escaneo de {len(CRYPTO_IDS)} activos...")

    for cg_id, symbol in CRYPTO_IDS.items():
        if (now - state.get(symbol, 0)) < 14400:
            print(f"⏳ {symbol} en cooldown.")
            continue

        df = get_data(cg_id)
        if df is not None:
            res = evaluate(df, symbol)
            if res and res["alert"]:
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
    print("🏁 Fin del escaneo.")

if __name__ == "__main__":
    main()
