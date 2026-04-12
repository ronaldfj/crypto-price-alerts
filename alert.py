import requests

SYMBOL = "BTCUSDT"
UPPER_ALERT = 95000
LOWER_ALERT = 80000

def get_price(symbol: str) -> float:
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    data = response.json()
    return float(data["price"])

def main():
    price = get_price(SYMBOL)
    print(f"Precio actual de {SYMBOL}: {price}")

    if price >= UPPER_ALERT:
        print(f"ALERTA: {SYMBOL} subió por encima de {UPPER_ALERT}")

    elif price <= LOWER_ALERT:
        print(f"ALERTA: {SYMBOL} bajó por debajo de {LOWER_ALERT}")

    else:
        print("Sin alerta.")

if __name__ == "__main__":
    main()
