"""
Módulo para la ejecución de órdenes en Deriv.com usando su WebSocket API.
"""

import os
import json
import asyncio
import logging
from typing import Dict, Any, Optional

import websockets

# Configuración desde variables de entorno
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")  # 1089 es un ID de prueba (demo)
DERIV_API_TOKEN = os.getenv("DERIV_API_TOKEN", "")
DERIV_ACCOUNT_TYPE = os.getenv("DERIV_ACCOUNT_TYPE", "demo")  # 'demo' o 'real'

# Símbolo por defecto (Forex) - puedes sobrescribirlo en la orden
DERIV_DEFAULT_SYMBOL = os.getenv("DERIV_DEFAULT_SYMBOL", "frxEURUSD")

# Configurar logging básico
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Mapeo de símbolos de CoinGecko a símbolos de Deriv (ajusta según tu broker de Deriv)
SYMBOL_MAP = {
    "BTC": "frxBTCUSD",
    "ETH": "frxETHUSD",
    "EURUSD": "frxEURUSD",
    "USDJPY": "frxUSDJPY",
    # Añade más pares según necesites
}

async def deriv_request(ws, request: Dict[str, Any], timeout: int = 10) -> Dict[str, Any]:
    """Envía una solicitud a la API de Deriv y espera la respuesta."""
    await ws.send(json.dumps(request))
    try:
        response = await asyncio.wait_for(ws.recv(), timeout=timeout)
        return json.loads(response)
    except asyncio.TimeoutError:
        logger.error(f"Timeout en la solicitud: {request}")
        return {"error": {"message": "Timeout en la solicitud"}}

async def execute_order_async(alert: Dict[str, Any]) -> Dict[str, Any]:
    """
    Conecta a Deriv usando WebSocket y ejecuta una orden.
    """
    if not DERIV_API_TOKEN:
        logger.error("DERIV_API_TOKEN no está configurado.")
        return {"success": False, "error": "DERIV_API_TOKEN no configurado"}

    logger.info(f"Ejecutando orden para: {alert}")

    # Construir símbolo Deriv o usar el por defecto
    symbol = SYMBOL_MAP.get(alert["symbol"], DERIV_DEFAULT_SYMBOL)
    side = alert["side"]
    stop_loss = alert["stop_loss"]
    take_profit = alert["take_profit"]
    risk_multiplier = alert.get("risk_multiplier", 1.0)

    # Calcular el tamaño de la orden (lote) basado en el riesgo
    # --- Cálculo de lote (simple por ahora. Mejorar después) ---
    balance = 10000  # Idealmente se obtendría de la cuenta real
    risk_per_trade = 0.01  # 1%
    risk_amount = balance * risk_per_trade
    if side == "LONG":
        risk_units = (alert["entry_price"] - stop_loss) / alert["entry_price"]
    else:
        risk_units = (stop_loss - alert["entry_price"]) / alert["entry_price"]

    if risk_units <= 0:
        return {"success": False, "error": "Risk units <= 0, stop loss mal configurado"}
    lot = (risk_amount / (risk_units * alert["entry_price"])) * risk_multiplier
    lot = round(lot, 2)  # Normalmente lotes con 2 decimales

    # --- Conexión WebSocket a Deriv ---
    ws_url = "wss://ws.deriv.com/websockets/v3"

    try:
        async with websockets.connect(ws_url) as ws:
            logger.info("Conectado a Deriv WebSocket.")

            # 1. Autenticar con el token
            auth_response = await deriv_request(ws, {
                "authorize": DERIV_API_TOKEN,
                "req_id": 1
            })
            if "error" in auth_response:
                error_msg = auth_response["error"].get("message", "Error desconocido")
                logger.error(f"Error de autenticación: {error_msg}")
                return {"success": False, "error": f"Auth error: {error_msg}"}
            logger.info("Autenticación exitosa.")

            account_type = auth_response.get("authorize", {}).get("account_type", DERIV_ACCOUNT_TYPE)
            logger.info(f"Conectado a cuenta {account_type}")

            # 2. Obtener balance (Opcional, para validación)
            balance_response = await deriv_request(ws, {"balance": 1, "req_id": 2})
            if "error" not in balance_response:
                balance = balance_response["balance"]["balance"]
                logger.info(f"Balance actual: {balance}")

            # 3. Realizar compra/venta (buy/sell)
            # Primero, obtener un "proposal" (cotización)
            contract_type = "CALL" if side == "LONG" else "PUT"
            buy_request = {
                "buy": 1,  # Este ID se obtiene del proposal, aquí simplificado
                "price": lot,  # Monto en USD o la moneda base
                "parameters": {
                    "amount": lot,
                    "basis": "stake",
                    "contract_type": contract_type,
                    "currency": "USD",
                    "duration": 60,  # Duración en segundos
                    "duration_unit": "second",
                    "symbol": symbol,
                }
            }

            # Simulación de proposal (Detalle omitido por brevedad)
            # En un caso real, usarías 'proposal' para obtener un ID válido
            buy_response = await deriv_request(ws, buy_request)
            if "error" in buy_response:
                error_msg = buy_response["error"].get("message", "Error desconocido")
                logger.error(f"Error en buy: {error_msg}")
                return {"success": False, "error": f"Buy error: {error_msg}"}

            logger.info(f"Orden ejecutada: {buy_response}")
            return {"success": True, "order_id": buy_response.get("buy", {}).get("contract_id", "N/A")}

    except Exception as e:
        logger.exception(f"Excepción en WebSocket: {e}")
        return {"success": False, "error": str(e)}

def execute_order(alert: Dict[str, Any]) -> Dict[str, Any]:
    """
    Wrapper síncrono para ejecutar orden, ideal para ser llamado desde Flask.
    """
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(execute_order_async(alert))
    except Exception as e:
        logger.exception(f"Fallo en execute_order: {e}")
        return {"success": False, "error": str(e)}
    finally:
        loop.close()

if __name__ == "__main__":
    # Prueba simple
    test_alert = {
        "symbol": "EURUSD",
        "side": "LONG",
        "entry_price": 1.1000,
        "stop_loss": 1.0950,
        "take_profit": 1.1100,
        "risk_multiplier": 1.0,
        "id": 999
    }
    result = execute_order(test_alert)
    print(result)
