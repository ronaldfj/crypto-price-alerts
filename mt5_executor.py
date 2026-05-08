import os
import MetaTrader5 as mt5
from typing import Dict, Any

# Mapeo de símbolos de CoinGecko a símbolos de MT5 (ajusta según tu broker)
SYMBOL_MAP = {
    "BTC": "BTCUSD",
    "ETH": "ETHUSD",
    "SOL": "SOLUSD",
    "BNB": "BNBUSD",
    "XRP": "XRPUSD",
    "LTC": "LTCUSD",
    "DOT": "DOTUSD",
    "LINK": "LINKUSD",
    "TON": "TONUSD",
    "TRX": "TRXUSD",
    "XLM": "XLMUSD",
}

def execute_order(alert: Dict[str, Any]) -> Dict[str, Any]:
    symbol_orig = alert["symbol"]
    symbol = SYMBOL_MAP.get(symbol_orig, symbol_orig + "USD")
    side = alert["side"]
    entry_price = alert["entry_price"]
    stop_loss = alert["stop_loss"]
    take_profit = alert["take_profit"]  # o tp2
    risk_multiplier = alert.get("risk_multiplier", 1.0)

    if not mt5.initialize():
        return {"success": False, "error": "MT5 initialization failed"}

    # Obtener saldo de la cuenta (si falla, usar valor por defecto)
    account_info = mt5.account_info()
    if account_info is None:
        mt5.shutdown()
        return {"success": False, "error": "No se pudo obtener información de la cuenta MT5"}
    balance = account_info.balance

    # Calcular lote basado en riesgo del 1% de la cuenta
    risk_per_trade = 0.01  # 1%
    risk_amount = balance * risk_per_trade
    if side == "LONG":
        risk_units = (entry_price - stop_loss) / entry_price
    else:
        risk_units = (stop_loss - entry_price) / entry_price
    if risk_units <= 0:
        mt5.shutdown()
        return {"success": False, "error": "Risk units <= 0, stop loss mal configurado"}
    lot = risk_amount / (risk_units * entry_price)
    lot = max(0.01, min(lot, 1.0))  # ajusta máximos según tu broker

    # Ajustar por multiplicador de riesgo táctico
    lot *= risk_multiplier
    lot = round(lot, 2)  # normalmente lotes de 2 decimales

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        mt5.shutdown()
        return {"success": False, "error": f"Symbol {symbol} not found in MT5"}

    order_type = mt5.ORDER_TYPE_BUY if side == "LONG" else mt5.ORDER_TYPE_SELL
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": entry_price,
        "sl": stop_loss,
        "tp": take_profit,
        "deviation": 10,
        "magic": 123456,
        "comment": f"SentinelAlert_{alert['id']}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    mt5.shutdown()
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {"success": False, "error": f"retcode={result.retcode}, comment={result.comment}"}
    return {"success": True, "order_id": result.order}
