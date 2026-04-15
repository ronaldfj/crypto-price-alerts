import os
import time
import json
from pathlib import Path
import pandas as pd
import yfinance as yf
import requests

# ── Configuración Maestra (CORREGIDA PARA YAHOO) ──────────────────────────
CRYPTO_SYMBOLS = [
    'BTC-USD', 'ETH-USD', 'BNB-USD', 'SOL-USD', 'XRP-USD', 
    'ADA-USD', 'AVAX-USD', 'DOT-USD', 'LINK-USD', 'MATIC-USD', # MATIC es más estable en Yahoo
    'LTC-USD', 'NEAR-USD', 'SUI-USD', 'FET-USD', 'RENDER-USD', 
    'TAO-USD', 'INJ-USD', 'STX-USD', 'PEPE-USD', 'SHIB-USD'   # Sin el '1' para evitar 404
]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = "alert_state.json"  # <--- UNIFICADO CON EL YAML
MIN_SCORE = 4.5  # Ajustado para captar señales como las de la ejecución anterior
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

# ... (Funciones de indicadores y evaluación se mantienen igual) ...

def main():
    state = load_state()
    now = time.time()
    print(f"🚀 Iniciando escaneo de {len(CRYPTO_SYMBOLS)} activos...")
    
    for symbol in CRYPTO_SYMBOLS:
        # Cooldown real: 4 horas (14400 seg)
        last_alert = state.get(symbol, 0)
        if (now - last_alert) < 14400:
            print(f"⏳ {symbol} en cooldown.")
            continue
        
        try:
            res = evaluate_crypto(symbol) # Asumiendo que esta función está definida
            if res and res.get("alert"):
                # (Lógica de envío de Telegram)
                # ...
                state[symbol] = now
                save_state(state) # Guardar inmediatamente después de alertar
                print(f"✅ Alerta enviada: {res['symbol']}")
            
            time.sleep(2) # Evitar bloqueos por ráfaga
        except Exception as e:
            print(f"❌ Error crítico en {symbol}: {e}")

if __name__ == "__main__":
    main()
