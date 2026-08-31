"""Monitor automático de cruces de niveles BTC (soporte/resistencia marcados a mano).

Corre en GitHub Actions junto con alert.py (mismo cron, misma cadencia). Reutiliza
la infraestructura del proyecto (fetch_latest_price, meta table, send_telegram) en
vez de duplicar lógica propia. Los niveles deben mantenerse sincronizados a mano
con LEVELS en btc_level_monitor.html si cambia el gráfico de referencia.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from alert import DB_FILE, get_db_connection, get_meta, init_db, send_telegram, set_meta
from data_source import fetch_latest_price

LEVELS = [
    {"label": "Resistencia (High reciente)", "price": 82814.03, "type": "resistencia"},
    {"label": "Resistencia secundaria", "price": 82033.66, "type": "resistencia"},
    {"label": "Macro Support 79K", "price": 79032.13, "type": "soporte"},
    {"label": "EMA/BB superior", "price": 75534.58, "type": "media"},
    {"label": "Macro Support 74K", "price": 74000.00, "type": "soporte"},
    {"label": "EMA/DC media", "price": 68954.65, "type": "media"},
    {"label": "1st ATH 2021 (65K)", "price": 63195.29, "type": "fibo"},
    {"label": "EMA corta", "price": 62776.58, "type": "media"},
    {"label": "Fibonacci 0.618", "price": 61813.04, "type": "fibo"},
    {"label": "Low (mínimo reciente)", "price": 57748.80, "type": "min"},
]

META_KEY = "btc_level_monitor:last_price"


def fmt(price: float) -> str:
    return f"${price:,.2f}"


def crossing_event(prev_price: float, curr_price: float):
    """Replica crossingEvent() de btc_level_monitor.html. None si no hubo cruce."""
    lo, hi = min(prev_price, curr_price), max(prev_price, curr_price)
    crossed = sorted(
        (l for l in LEVELS if lo < l["price"] < hi),
        key=lambda l: l["price"],
        reverse=True,
    )
    if not crossed:
        return None

    up = curr_price > prev_price
    sorted_levels = sorted(LEVELS, key=lambda l: l["price"], reverse=True)
    above = next((l for l in reversed(sorted_levels) if l["price"] >= curr_price), None)
    below = next((l for l in sorted_levels if l["price"] <= curr_price), None)

    if below and above:
        rango = f"entre {below['label']} ({fmt(below['price'])}) y {above['label']} ({fmt(above['price'])})"
    elif not below:
        rango = f"por debajo del mínimo marcado ({sorted_levels[-1]['label']})"
    else:
        rango = f"por encima de la resistencia máxima ({sorted_levels[0]['label']})"

    nombres = " y ".join(f"{l['label']} ({fmt(l['price'])})" for l in crossed)
    direction = "al alza" if up else "a la baja"
    arrow = "\U0001F7E2" if up else "\U0001F534"
    return arrow, f"Cruzó {nombres} {direction} — ahora {rango}"


def main() -> None:
    conn = get_db_connection(DB_FILE)
    init_db(conn)

    price = fetch_latest_price("BTC")
    if price is None:
        print("No se pudo obtener el precio de BTC (Bybit/OKX sin datos).")
        conn.close()
        return

    raw = get_meta(conn, META_KEY, "")
    prev = json.loads(raw) if raw else None

    if prev is not None:
        result = crossing_event(prev["price"], price)
        if result:
            arrow, event = result
            send_telegram(f"{arrow} <b>BTC/USD</b> — {event}\nPrecio: {fmt(price)}")
            print(f"Alerta enviada: {event}")
        else:
            print(f"Sin cruces. Precio actual: {fmt(price)} (anterior: {fmt(prev['price'])})")
    else:
        print(f"Primer registro. Precio: {fmt(price)}")

    set_meta(conn, META_KEY, json.dumps({"price": price, "ts": datetime.now(timezone.utc).isoformat()}))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
