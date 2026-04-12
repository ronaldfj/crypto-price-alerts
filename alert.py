import requests
import sys

SYMBOL = "BTCUSD"
UPPER_ALERT = 95000
LOWER_ALERT = 80000

def get_price() -> float:
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": "bitcoin",
        "vs_currencies": "usd"
    }

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()

    data = response.json()

    if "bitcoin" not in data or "usd" not in data["bitcoin"]:
        raise ValueError(f"Respuesta inesperada de la API: {data}")

    return float(data["bitcoin"]["usd"])

def main():
    try:
        price = get_price()
        print(f"Precio actual de {SYMBOL}: {price}")

        if price >= UPPER_ALERT:
            print(f"ALERTA: {SYMBOL} subió por encima de {UPPER_ALERT}")
        elif price <= LOWER_ALERT:
            print(f"ALERTA: {SYMBOL} bajó por debajo de {LOWER_ALERT}")
        else:
            print("Sin alerta.")

    except Exception as e:
        print(f"Error ejecutando alerta: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
