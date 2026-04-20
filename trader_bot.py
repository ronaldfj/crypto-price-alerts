"""
trader_bot.py — Bot persistente de ejecución en Binance Spot

Responsabilidades:
  1. Escucha callbacks de botones inline de Telegram (polling).
  2. Cuando el usuario aprueba una alerta, crea en Binance Spot:
       - Orden LIMIT de entrada
       - Stop-Loss (STOP_LOSS_LIMIT)
       - Take Profit TP1 (TAKE_PROFIT_LIMIT)
       - Take Profit TP2 (TAKE_PROFIT_LIMIT)
  3. Confirma el resultado por Telegram.
  4. Expira alertas pendientes después de MAX_PENDING_MINUTES.

Variables de entorno requeridas:
  TELEGRAM_BOT_TOKEN      — token del bot
  TELEGRAM_CHAT_ID        — tu chat_id (solo responde a este ID)
  BINANCE_API_KEY         — API key de Binance Spot
  BINANCE_API_SECRET      — API secret de Binance Spot

Variables opcionales:
  ORDER_SIZE_USDT         — USDT por operación (default: 20)
  MAX_PENDING_MINUTES     — minutos antes de expirar alerta (default: 30)
  POLLING_INTERVAL        — segundos entre polls a Telegram (default: 3)
  DRY_RUN                 — "true" para simular sin enviar órdenes reales
"""

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
from typing import Any, Dict, Optional, Tuple

import requests

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("trader-bot")

# ── Configuración ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID     = str(os.getenv("TELEGRAM_CHAT_ID", ""))
BINANCE_API_KEY      = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET   = os.getenv("BINANCE_API_SECRET", "")

ORDER_SIZE_USDT      = float(os.getenv("ORDER_SIZE_USDT", "20"))
MAX_PENDING_MINUTES  = int(os.getenv("MAX_PENDING_MINUTES", "30"))
POLLING_INTERVAL     = float(os.getenv("POLLING_INTERVAL", "3"))
DRY_RUN              = os.getenv("DRY_RUN", "false").lower() == "true"

BINANCE_BASE         = "https://api.binance.com"
TELEGRAM_BASE        = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
REQUEST_TIMEOUT      = 15

# ── Estado en memoria ─────────────────────────────────────────────────────────
# pending_alerts[callback_id] = {symbol, entry, stop, tp1, tp2, ts, message_id}
pending_alerts: Dict[str, Dict[str, Any]] = {}


# ── Helpers Telegram ──────────────────────────────────────────────────────────
def tg_post(method: str, payload: Dict[str, Any]) -> Optional[Dict]:
    """Llama a la API de Telegram y retorna el body o None si falla."""
    try:
        r = requests.post(
            f"{TELEGRAM_BASE}/{method}",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        body = r.json()
        if not body.get("ok"):
            log.warning(f"Telegram/{method} error: {body}")
            return None
        return body
    except Exception as e:
        log.error(f"Telegram/{method} excepción: {e}")
        return None


def send_message(text: str, reply_markup: Optional[Dict] = None) -> Optional[int]:
    """Envía mensaje y retorna el message_id."""
    payload: Dict[str, Any] = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    body = tg_post("sendMessage", payload)
    if body:
        return body["result"]["message_id"]
    return None


def edit_message(message_id: int, text: str) -> None:
    """Edita un mensaje existente (usado para actualizar estado del botón)."""
    tg_post("editMessageText", {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    })


def answer_callback(callback_query_id: str, text: str = "") -> None:
    """Responde al callback para quitar el spinner del botón."""
    tg_post("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text,
    })


def get_updates(offset: int) -> list:
    """Polling: obtiene actualizaciones desde Telegram."""
    try:
        r = requests.get(
            f"{TELEGRAM_BASE}/getUpdates",
            params={"offset": offset, "timeout": 10, "allowed_updates": ["callback_query", "message"]},
            timeout=20,
        )
        body = r.json()
        if body.get("ok"):
            return body.get("result", [])
    except Exception as e:
        log.error(f"getUpdates excepción: {e}")
    return []


# ── Helpers Binance ───────────────────────────────────────────────────────────
def binance_sign(params: Dict[str, Any]) -> str:
    """Genera firma HMAC-SHA256 para Binance."""
    query = urllib.parse.urlencode(params)
    return hmac.new(
        BINANCE_API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def binance_request(method: str, path: str, params: Dict[str, Any]) -> Tuple[bool, Any]:
    """
    Ejecuta una request firmada a Binance.
    Retorna (éxito, body).
    """
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = binance_sign(params)

    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    url = f"{BINANCE_BASE}{path}"

    try:
        if method == "POST":
            r = requests.post(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        else:
            r = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)

        body = r.json()
        if r.status_code == 200:
            return True, body
        else:
            log.error(f"Binance {path} error {r.status_code}: {body}")
            return False, body
    except Exception as e:
        log.error(f"Binance {path} excepción: {e}")
        return False, str(e)


def get_symbol_info(symbol: str) -> Optional[Dict]:
    """Obtiene precisión de precio y cantidad para el símbolo."""
    ok, body = binance_request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
    if not ok:
        return None
    symbols = body.get("symbols", [])
    for s in symbols:
        if s["symbol"] == symbol:
            return s
    return None


def get_step_and_tick(symbol_info: Dict) -> Tuple[float, float]:
    """
    Extrae stepSize (precisión de cantidad) y tickSize (precisión de precio)
    desde los filtros del símbolo.
    """
    step_size = 0.001
    tick_size = 0.01
    for f in symbol_info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            step_size = float(f["stepSize"])
        if f["filterType"] == "PRICE_FILTER":
            tick_size = float(f["tickSize"])
    return step_size, tick_size


def round_step(value: float, step: float) -> float:
    """Redondea value al múltiplo de step más cercano hacia abajo."""
    precision = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
    factor = 1 / step
    return round(int(value * factor) / factor, precision)


def get_current_price(symbol: str) -> Optional[float]:
    """Obtiene el precio actual de mercado."""
    try:
        r = requests.get(
            f"{BINANCE_BASE}/api/v3/ticker/price",
            params={"symbol": symbol},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 200:
            return float(r.json()["price"])
    except Exception as e:
        log.error(f"Precio actual {symbol}: {e}")
    return None


# ── Ejecución de órdenes ──────────────────────────────────────────────────────
def execute_trade(alert: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Crea las 4 órdenes en Binance Spot:
      1. LIMIT de entrada
      2. STOP_LOSS_LIMIT
      3. TAKE_PROFIT_LIMIT TP1
      4. TAKE_PROFIT_LIMIT TP2 (mitad del tamaño)

    Retorna (éxito, mensaje de resultado).
    """
    symbol_binance = alert["symbol"] + "USDT"  # BTC → BTCUSDT
    entry  = float(alert["entry"])
    stop   = float(alert["stop"])
    tp1    = float(alert["tp1"])
    tp2    = float(alert["tp2"])

    if DRY_RUN:
        log.info(f"[DRY RUN] Simulando órdenes para {symbol_binance}")
        msg = (
            f"🧪 <b>DRY RUN — {alert['symbol']}</b>\n\n"
            f"Entrada LIMIT: ${entry:.4f}\n"
            f"Stop Loss: ${stop:.4f}\n"
            f"TP1: ${tp1:.4f}\n"
            f"TP2: ${tp2:.4f}\n"
            f"Tamaño: ${ORDER_SIZE_USDT:.0f} USDT\n\n"
            f"<i>Modo prueba — ninguna orden fue enviada a Binance.</i>"
        )
        return True, msg

    # Obtener info del símbolo para precisión
    symbol_info = get_symbol_info(symbol_binance)
    if not symbol_info:
        return False, f"No se encontró información del símbolo {symbol_binance} en Binance."

    step_size, tick_size = get_step_and_tick(symbol_info)

    # Calcular cantidad total basada en USDT disponible
    quantity_total = round_step(ORDER_SIZE_USDT / entry, step_size)
    quantity_half  = round_step(quantity_total / 2, step_size)

    # Redondear precios al tick del símbolo
    entry_price = round_step(entry, tick_size)
    stop_price  = round_step(stop * 0.999, tick_size)  # trigger ligeramente antes
    stop_limit  = round_step(stop * 0.998, tick_size)  # límite debajo del trigger
    tp1_price   = round_step(tp1, tick_size)
    tp2_price   = round_step(tp2, tick_size)

    results = []
    all_ok = True

    # ── Orden 1: Entrada LIMIT ────────────────────────────────────────────────
    ok, body = binance_request("POST", "/api/v3/order", {
        "symbol":      symbol_binance,
        "side":        "BUY",
        "type":        "LIMIT",
        "timeInForce": "GTC",
        "quantity":    quantity_total,
        "price":       entry_price,
    })
    if ok:
        results.append(f"✅ Entrada LIMIT ${entry_price:.4f} ({quantity_total} {alert['symbol']})")
        log.info(f"Entrada creada: {body.get('orderId')}")
    else:
        all_ok = False
        results.append(f"❌ Entrada fallida: {body}")

    # ── Orden 2: Stop Loss ────────────────────────────────────────────────────
    ok, body = binance_request("POST", "/api/v3/order", {
        "symbol":      symbol_binance,
        "side":        "SELL",
        "type":        "STOP_LOSS_LIMIT",
        "timeInForce": "GTC",
        "quantity":    quantity_total,
        "stopPrice":   stop_price,
        "price":       stop_limit,
    })
    if ok:
        results.append(f"✅ Stop Loss ${stop_limit:.4f}")
        log.info(f"Stop Loss creado: {body.get('orderId')}")
    else:
        all_ok = False
        results.append(f"❌ Stop Loss fallido: {body}")

    # ── Orden 3: Take Profit TP1 (mitad de la posición) ───────────────────────
    ok, body = binance_request("POST", "/api/v3/order", {
        "symbol":      symbol_binance,
        "side":        "SELL",
        "type":        "TAKE_PROFIT_LIMIT",
        "timeInForce": "GTC",
        "quantity":    quantity_half,
        "stopPrice":   round_step(tp1_price * 0.9995, tick_size),
        "price":       tp1_price,
    })
    if ok:
        results.append(f"✅ TP1 ${tp1_price:.4f} ({quantity_half} {alert['symbol']})")
        log.info(f"TP1 creado: {body.get('orderId')}")
    else:
        all_ok = False
        results.append(f"❌ TP1 fallido: {body}")

    # ── Orden 4: Take Profit TP2 (otra mitad) ─────────────────────────────────
    ok, body = binance_request("POST", "/api/v3/order", {
        "symbol":      symbol_binance,
        "side":        "SELL",
        "type":        "TAKE_PROFIT_LIMIT",
        "timeInForce": "GTC",
        "quantity":    quantity_half,
        "stopPrice":   round_step(tp2_price * 0.9995, tick_size),
        "price":       tp2_price,
    })
    if ok:
        results.append(f"✅ TP2 ${tp2_price:.4f} ({quantity_half} {alert['symbol']})")
        log.info(f"TP2 creado: {body.get('orderId')}")
    else:
        all_ok = False
        results.append(f"❌ TP2 fallido: {body}")

    status_icon = "✅" if all_ok else "⚠️"
    msg = (
        f"{status_icon} <b>Órdenes {alert['symbol']}USDT</b>\n\n"
        + "\n".join(results)
        + f"\n\n💵 <b>Capital comprometido:</b> ${ORDER_SIZE_USDT:.0f} USDT"
    )
    return all_ok, msg


# ── Construcción del mensaje de alerta con botones ────────────────────────────
def build_alert_with_buttons(
    symbol: str,
    entry: float,
    stop: float,
    tp1: float,
    tp1_rr: float,
    tp2: float,
    tp2_rr: float,
    score: float,
    rr: float,
    alert_text: str,
) -> Tuple[str, Dict, str]:
    """
    Construye el mensaje de alerta con botones inline Aprobar/Rechazar.
    Retorna (texto, reply_markup, callback_id).
    """
    # ID único para esta alerta — usado para recuperar los datos al aprobar
    callback_id = f"{symbol}_{int(time.time())}"

    # Guardamos los datos de la alerta en memoria
    pending_alerts[callback_id] = {
        "symbol": symbol,
        "entry":  entry,
        "stop":   stop,
        "tp1":    tp1,
        "tp2":    tp2,
        "ts":     time.time(),
    }

    # Botones inline
    markup = {
        "inline_keyboard": [[
            {
                "text": f"✅ Ejecutar ${ORDER_SIZE_USDT:.0f} USDT",
                "callback_data": f"approve:{callback_id}",
            },
            {
                "text": "❌ Rechazar",
                "callback_data": f"reject:{callback_id}",
            },
        ]]
    }

    return alert_text, markup, callback_id


# ── Procesamiento de callbacks ────────────────────────────────────────────────
def handle_callback(update: Dict[str, Any]) -> None:
    """Procesa un callback_query de Telegram (botón presionado)."""
    cq = update.get("callback_query", {})
    cq_id      = cq.get("id", "")
    data       = cq.get("data", "")
    message_id = cq.get("message", {}).get("message_id")

    # Seguridad: solo responde al chat_id configurado
    chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
    if chat_id != TELEGRAM_CHAT_ID:
        log.warning(f"Callback de chat_id desconocido: {chat_id}")
        answer_callback(cq_id, "⛔ No autorizado")
        return

    if not data or ":" not in data:
        answer_callback(cq_id)
        return

    action, payload = data.split(":", 1)

    # ── Rechazar ──────────────────────────────────────────────────────────────
    if action == "reject":
        parts = payload.split("|")
        symbol = parts[0] if parts else "?"
        answer_callback(cq_id, "❌ Rechazado.")
        if message_id:
            edit_message(message_id, f"❌ <b>Rechazado</b> — {symbol} no ejecutado.")
        log.info(f"Alerta rechazada: {symbol}")
        return

    # ── Aprobar ───────────────────────────────────────────────────────────────
    if action == "approve":
        # Formato: symbol|entry|stop|tp1|tp2|ts
        parts = payload.split("|")
        if len(parts) < 6:
            answer_callback(cq_id, "⚠️ Datos de alerta incompletos.")
            return

        try:
            symbol = parts[0]
            entry  = float(parts[1])
            stop   = float(parts[2])
            tp1    = float(parts[3])
            tp2    = float(parts[4])
            ts     = int(parts[5])
        except (ValueError, IndexError) as e:
            log.error(f"Error parseando callback: {e} | data={data}")
            answer_callback(cq_id, "⚠️ Error procesando la alerta.")
            return

        # Verificar expiración basada en el timestamp embebido
        elapsed_minutes = (time.time() - ts) / 60
        if elapsed_minutes > MAX_PENDING_MINUTES:
            answer_callback(cq_id, f"⏰ Alerta expirada ({elapsed_minutes:.0f}min).")
            if message_id:
                edit_message(message_id, f"⏰ <b>Alerta expirada</b> — han pasado {elapsed_minutes:.0f} minutos.")
            return

        answer_callback(cq_id, "⏳ Ejecutando órdenes...")
        log.info(f"Aprobado: {symbol} | entrada=${entry:.4f} | stop=${stop:.4f}")

        alert = {"symbol": symbol, "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2}

        # Verificar drift de precio
        current_price = get_current_price(symbol + "USDT")
        if current_price:
            drift_pct = abs(current_price - entry) / entry * 100
            if drift_pct > 1.5:
                msg = (
                    f"⚠️ <b>Ejecución cancelada — {symbol}</b>\n\n"
                    f"El precio se alejó <b>{drift_pct:.2f}%</b> desde la alerta.\n"
                    f"Precio alerta: <b>${entry:.4f}</b>\n"
                    f"Precio actual: <b>${current_price:.4f}</b>\n\n"
                    f"<i>Ejecuta manualmente si aún consideras válida la entrada.</i>"
                )
                if message_id:
                    edit_message(message_id, f"⚠️ <b>Cancelado por drift</b> — {symbol} ({drift_pct:.2f}%)")
                send_message(msg)
                return

        ok, result_msg = execute_trade(alert)

        if message_id:
            status = "✅ Ejecutado" if ok else "⚠️ Ejecución parcial"
            edit_message(message_id, f"{status} — <b>{symbol}</b> | ${entry:.4f}")

        send_message(result_msg)
        log.info(f"Trade {symbol}: {'OK' if ok else 'PARCIAL'}")


def handle_message(update: Dict[str, Any]) -> None:
    """Maneja comandos de texto simples."""
    msg  = update.get("message", {})
    text = msg.get("text", "").strip()
    chat = str(msg.get("chat", {}).get("id", ""))

    if chat != TELEGRAM_CHAT_ID:
        return

    if text == "/status":
        if pending_alerts:
            lines = [f"⏳ <b>Alertas pendientes ({len(pending_alerts)}):</b>"]
            for cid, a in pending_alerts.items():
                mins = (time.time() - a["ts"]) / 60
                lines.append(f"• {a['symbol']} — {mins:.0f}min transcurridos")
            send_message("\n".join(lines))
        else:
            send_message("✅ No hay alertas pendientes.")

    elif text == "/ping":
        mode = "DRY RUN" if DRY_RUN else "PRODUCCIÓN"
        send_message(f"🟢 Bot activo | Modo: <b>{mode}</b> | Capital por trade: <b>${ORDER_SIZE_USDT:.0f} USDT</b>")

    elif text == "/help":
        send_message(
            "📖 <b>Comandos disponibles:</b>\n\n"
            "/ping — verifica que el bot está activo\n"
            "/status — alertas pendientes de aprobación\n"
            "/help — esta ayuda\n\n"
            "<i>Las alertas llegan automáticamente desde el scanner.</i>"
        )


# ── Loop de polling ───────────────────────────────────────────────────────────
def cleanup_expired_alerts() -> None:
    """Elimina alertas pendientes que superaron el tiempo máximo."""
    now = time.time()
    expired = [
        cid for cid, a in pending_alerts.items()
        if (now - a["ts"]) / 60 > MAX_PENDING_MINUTES
    ]
    for cid in expired:
        symbol = pending_alerts[cid]["symbol"]
        del pending_alerts[cid]
        log.info(f"Alerta expirada limpiada: {symbol} ({cid})")


def run_polling() -> None:
    """Loop principal de polling a Telegram."""
    log.info(f"🤖 Trader Bot iniciado | DRY_RUN={DRY_RUN} | Capital=${ORDER_SIZE_USDT:.0f} USDT")
    log.info(f"⏳ Expiración de alertas: {MAX_PENDING_MINUTES} minutos")

    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN no configurado. Abortando.")
        return
    if not BINANCE_API_KEY and not DRY_RUN:
        log.error("BINANCE_API_KEY no configurado y DRY_RUN=false. Abortando.")
        return

    # Anunciar arranque
    mode_tag = " [DRY RUN]" if DRY_RUN else ""
    send_message(
        f"🟢 <b>Trader Bot activo{mode_tag}</b>\n"
        f"Capital por trade: <b>${ORDER_SIZE_USDT:.0f} USDT</b>\n"
        f"Expiración de alertas: <b>{MAX_PENDING_MINUTES} min</b>\n\n"
        f"Usa /help para ver comandos disponibles."
    )

    offset = 0
    cleanup_counter = 0

    while True:
        try:
            updates = get_updates(offset)

            for update in updates:
                update_id = update.get("update_id", 0)
                offset = update_id + 1

                if "callback_query" in update:
                    handle_callback(update)
                elif "message" in update:
                    handle_message(update)

            # Limpiar alertas expiradas cada 20 ciclos (~60 segundos)
            cleanup_counter += 1
            if cleanup_counter >= 20:
                cleanup_expired_alerts()
                cleanup_counter = 0

        except KeyboardInterrupt:
            log.info("Bot detenido por el usuario.")
            break
        except Exception as e:
            log.error(f"Error en loop principal: {e}")

        time.sleep(POLLING_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_polling()
