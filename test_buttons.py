import os
import json
import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TOKEN or not CHAT_ID:
    raise ValueError("Faltan variables")

url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
payload = {
    "chat_id": CHAT_ID,
    "text": "🔔 Prueba de botón (sin librería)",
    "reply_markup": {
        "inline_keyboard": [[
            {"text": "✅ ENTRAR", "callback_data": "enter:{\"id\":999}"},
            {"text": "❌ RECHAZAR", "callback_data": "reject:{\"id\":999}"}
        ]]
    },
    "parse_mode": "HTML"
}
r = requests.post(url, json=payload)
print(r.status_code, r.text)
