import os
import json
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID")

bot = Bot(token=TELEGRAM_BOT_TOKEN)

alert_id = 999
side = "LONG"
entry_price = 50000.0
stop_loss = 49500.0
take_profit = 51000.0

payload = json.dumps({
    "id": alert_id,
    "side": side,
    "entry": entry_price,
    "sl": stop_loss,
    "tp": take_profit
})
if len(payload) > 64:
    payload = json.dumps({"id": alert_id})

keyboard = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("✅ ENTRAR", callback_data=f"enter:{payload}"),
        InlineKeyboardButton("❌ RECHAZAR", callback_data=f"reject:{payload}"),
    ]
])

text = (
    "🔔 <b>ALERTA DE PRUEBA</b>\n"
    f"Lado: {side}\n"
    f"Entry: {entry_price}\n"
    f"SL: {stop_loss}\n"
    f"TP: {take_profit}\n"
    "Pulsa botón para probar integración con Deriv."
)

bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, reply_markup=keyboard, parse_mode="HTML")
print("Mensaje de prueba enviado.")
