import os
import sqlite3
import time
import logging
from flask import Flask, request, Response
from telegram import Update, Bot
from telegram.ext import Dispatcher, CallbackQueryHandler
from mt5_executor import execute_order

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
DB_FILE = os.getenv("ALERT_DB_FILE", "alerts_state.db")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID")

app = Flask(__name__)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dispatcher = Dispatcher(bot, None, use_context=True)

logging.basicConfig(level=logging.INFO)

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def handle_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != TELEGRAM_CHAT_ID:
        query.answer("No autorizado", show_alert=True)
        return

    data = query.data
    if data.startswith("enter:"):
        alert_id = int(data.split(":")[1])
        conn = get_db_connection()
        alert = conn.execute(
            "SELECT id, symbol, side, entry_price, stop_loss, take_profit, tp1, tp2, risk_multiplier "
            "FROM alerts WHERE id = ? AND status = 'ACTIVE'",
            (alert_id,)
        ).fetchone()
        if not alert:
            query.answer("⚠️ Alerta ya no está activa o no existe", show_alert=True)
            conn.close()
            return

        # Registrar solicitud
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO execution_requests (alert_id, user_id, action, status, requested_at) VALUES (?, ?, ?, ?, ?)",
            (alert_id, user_id, "ENTER", "PENDING", int(time.time()))
        )
        conn.commit()
        conn.close()

        # Ejecutar orden en MT5
        result = execute_order(dict(alert))
        conn = get_db_connection()
        if result["success"]:
            conn.execute(
                "UPDATE execution_requests SET status = 'EXECUTED', processed_at = ?, order_id = ? WHERE alert_id = ? AND status = 'PENDING'",
                (int(time.time()), result["order_id"], alert_id)
            )
            conn.commit()
            query.answer(f"✅ Orden ejecutada: {result['order_id']}")
            query.edit_message_text(
                text=query.message.text + f"\n\n✅ **ORDEN ENVIADA A MT5**\nOrden: {result['order_id']} | Symbol: {alert['symbol']} | Side: {alert['side']}",
                parse_mode="HTML"
            )
        else:
            conn.execute(
                "UPDATE execution_requests SET status = 'FAILED', processed_at = ?, error_message = ? WHERE alert_id = ? AND status = 'PENDING'",
                (int(time.time()), result["error"], alert_id)
            )
            conn.commit()
            query.answer(f"❌ Error: {result['error']}", show_alert=True)
        conn.close()

    elif data.startswith("reject:"):
        alert_id = int(data.split(":")[1])
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO execution_requests (alert_id, user_id, action, status, requested_at) VALUES (?, ?, ?, ?, ?)",
            (alert_id, user_id, "REJECT", "REJECTED", int(time.time()))
        )
        conn.commit()
        conn.close()
        query.answer("Operación descartada")
        query.edit_message_text(
            text=query.message.text + "\n\n❌ **RECHAZADA**",
            parse_mode="HTML"
        )

dispatcher.add_handler(CallbackQueryHandler(handle_callback))

@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return Response("ok", status=200)

if __name__ == "__main__":
    # Configurar webhook (debe ejecutarse una vez al inicio)
    bot.set_webhook(f"https://tu-dominio.com/webhook/{TELEGRAM_BOT_TOKEN}")
    app.run(host="0.0.0.0", port=8443, ssl_context=("cert.pem", "key.pem"))
