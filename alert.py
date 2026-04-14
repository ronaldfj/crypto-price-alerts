import os
import sys
import time
import json
from pathlib import Path

import numpy as np
import pandas as pd
import requests

VS_CURRENCY = "usd"

ASSETS = [
    {"coin_id": "bitcoin", "symbol": "BTCUSD"},
    {"coin_id": "ethereum", "symbol": "ETHUSD"},
    {"coin_id": "solana", "symbol": "SOLUSD"},
    {"coin_id": "ripple", "symbol": "XRPUSD"},
    {"coin_id": "binancecoin", "symbol": "BNBUSD"},
]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY")

ALERT_COOLDOWN_HOURS = 24
STATE_FILE = "alert_state.json"

DAYS_FOR_DAILY = 300
DAYS_FOR_4H = 100

MIN_SCORE = 4.5
MIN_RR = 1.8


# ── Estado ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        if not Path(STATE_FILE).exists():
            return {}
        content = Path(STATE_FILE).read_text().strip()
        if not content:
            return {}
        return json.loads(content)
    except Exception:
        return {}


def already_alerted(symbol: str) -> bool:
    state = load_state()
    last = state.get(symbol)
    if not last:
        return False
    return (time.time() - last) < ALERT_COOLDOWN_HOURS * 3600


def mark_alerted(symbol: str):
    state = load_state()
    state[symbol] = time.time()
    Path(STATE_FILE).write_text(json.dumps(state))


# ── CoinGecko ──────────────────────────────────────────────────────────────────

def cg_get(url: str, params: dict) -> dict:
    if not COINGECKO_API_KEY:
        raise ValueError("Falta COINGECKO_API_KEY en GitHub Secrets")

    params = params.copy()
    params["x_cg_demo_api_key"] = COINGECKO_API_KEY

    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def get_market_chart_days(coin_id: str, vs_currency: str, days: int, interval: str | None = None) -> pd.DataFrame:
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {
        "vs_currency": vs_currency,
        "days": days,
    }
    if interval:
        params["interval"] = interval

    data = cg_get(url, params)

    prices = pd.DataFrame(data["prices"], columns=["timestamp", "price"])
    volumes = pd.DataFrame(data["total_volumes"], columns=["timestamp", "volume_proxy"])

    df = prices.merge(volumes, on="timestamp", how="inner")
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    return df


def build_ohlcv_from_series(df_raw: pd.DataFrame, rule: str) -> pd.DataFrame:
    df = df_raw.copy().set_index("timestamp")

    out = pd.DataFrame()
    out["open"] = df["price"].resample(rule).first()
    out["high"] = df["price"].resample(rule).max()
    out["low"] = df["price"].resample(rule).min()
    out["close"] = df["price"].resample(rule).last()
    out["volume"] = df["volume_proxy"].resample(rule).mean()

    out = out.dropna().reset_index()

    if len(out) > 1:
        out = out.iloc[:-1].copy().reset_index(drop=True)

    return out


# ── Indicadores ────────────────────────────────────────────────────────────────

def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


# ── Utilidades de señal ────────────────────────────────────────────────────────

def relative_activity(df: pd.DataFrame, lookback: int = 20) -> float:
    if len(df) < lookback + 1:
        return 0.0
    last_val = df["volume"].iloc[-1]
    avg_val = df["volume"].iloc[-(lookback + 1):-1].mean()
    if avg_val == 0 or pd.isna(avg_val):
        return 0.0
    return float(last_val / avg_val)


def near_moving_average(price: float, ma_value: float, pct: float = 0.015) -> bool:
    if ma_value == 0 or pd.isna(ma_value):
        return False
    return abs(price - ma_value) / ma_value <= pct


def calc_rr(df_4h: pd.DataFrame) -> tuple[float, float, float, float]:
    if len(df_4h) < 30:
        return 0.0, 0.0, 0.0, 0.0

    entry = df_4h["close"].iloc[-1]
    atr_4h = df_4h["atr14"].iloc[-1]

    recent_low = df_4h["low"].iloc[-3:].min()
    ema50 = df_4h["ema50"].iloc[-1]

    atr_stop = ema50 - 0.5 * atr_4h
    stop = min(recent_low, atr_stop)

    risk = entry - stop
    if risk <= 0:
        return entry, stop, 0.0, 0.0

    target = entry + 1.8 * risk
    rr = (target - entry) / risk
    return entry, stop, target, float(rr)


# ── Telegram ───────────────────────────────────────────────────────────────────

def send_telegram_message(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()


# ── Evaluación V3 ──────────────────────────────────────────────────────────────

def evaluate_asset(coin_id: str, symbol: str) -> dict:
    df_raw_daily = get_market_chart_days(coin_id, VS_CURRENCY, DAYS_FOR_DAILY)
    time.sleep(1.5)
    df_raw_4h = get_market_chart_days(coin_id, VS_CURRENCY, DAYS_FOR_4H, interval="hourly")

    df_d = build_ohlcv_from_series(df_raw_daily, "1D")
    df_w = build_ohlcv_from_series(df_raw_daily, "W-MON")
    df_4h = build_ohlcv_from_series(df_raw_4h, "4h")

    if len(df_d) < 80 or len(df_w) < 20 or len(df_4h) < 80:
        raise ValueError(
            f"No hay suficientes datos para {symbol}. 4H={len(df_4h)}, D={len(df_d)}, W={len(df_w)}"
        )

    for df in [df_d, df_w, df_4h]:
        df["ema20"] = ema(df["close"], 20)
        df["ema50"] = ema(df["close"], 50)
        df["rsi14"] = rsi(df["close"], 14)

    df_4h["atr14"] = atr(df_4h, 14)

    score = 0.0
    reasons = []

    # 1) Régimen diario (máx 3)
    if df_d["close"].iloc[-1] > df_d["ema50"].iloc[-1]:
        score += 1
        reasons.append("D: Precio > EMA50 (+1)")
    if df_d["ema20"].iloc[-1] > df_d["ema50"].iloc[-1]:
        score += 1
        reasons.append("D: EMA20 > EMA50 (+1)")
    if df_d["rsi14"].iloc[-1] > 52:
        score += 1
        reasons.append("D: RSI14 > 52 (+1)")

    # 2) Contexto semanal suave (máx 1)
    if df_w["close"].iloc[-1] > df_w["ema20"].iloc[-1]:
        score += 1
        reasons.append("W: Precio > EMA20 (+1)")

    # 3) Setup 4H pullback (máx 2)
    close_4h = df_4h["close"].iloc[-1]
    ema20_4h = df_4h["ema20"].iloc[-1]
    ema50_4h = df_4h["ema50"].iloc[-1]
    rsi_4h = df_4h["rsi14"].iloc[-1]
    prev_rsi_4h = df_4h["rsi14"].iloc[-2]

    if near_moving_average(close_4h, ema20_4h, 0.015) or near_moving_average(close_4h, ema50_4h, 0.02):
        score += 1
        reasons.append("4H: Pullback a EMA20/EMA50 (+1)")

    if (40 <= rsi_4h <= 62) or (prev_rsi_4h < 45 <= rsi_4h):
        score += 1
        reasons.append("4H: RSI de recuperación (+1)")

    # 4) Gatillo 4H (máx 2)
    prev_close = df_4h["close"].iloc[-2]
    prev_high = df_4h["high"].iloc[-2]
    activity_ratio = relative_activity(df_4h, 20)

    if close_4h > ema20_4h and close_4h > prev_close:
        score += 1
        reasons.append("4H: Cierre fuerte sobre EMA20 (+1)")

    if close_4h > prev_high or activity_ratio >= 1.10:
        score += 1
        reasons.append(f"4H: Break/actividad {activity_ratio:.2f}x (+1)")

    entry, stop, target, rr = calc_rr(df_4h)

    return {
        "coin_id": coin_id,
        "symbol": symbol,
        "score": score,
        "rr": rr,
        "entry": entry,
        "stop": stop,
        "target": target,
        "activity_ratio": activity_ratio,
        "rsi_4h": rsi_4h,
        "reasons": reasons,
        "alert": score >= MIN_SCORE and rr >= MIN_RR,
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    try:
        print("COINGECKO_API_KEY cargada:", bool(COINGECKO_API_KEY))

        ping_url = "https://api.coingecko.com/api/v3/ping"
        ping_params = {"x_cg_demo_api_key": COINGECKO_API_KEY}
        ping_response = requests.get(ping_url, params=ping_params, timeout=10)
        print("Ping CoinGecko status:", ping_response.status_code)
        print("Ping CoinGecko body:", ping_response.text)

        alerts_found = []

        for asset in ASSETS:
            try:
                result = evaluate_asset(asset["coin_id"], asset["symbol"])

                print(f"=== {result['symbol']} ===")
                print(f"Score total: {result['score']:.1f}")
                print(f"R:R estimado: {result['rr']:.2f}")
                print(f"Entry: {result['entry']:.2f}")
                print(f"Stop: {result['stop']:.2f}")
                print(f"Target: {result['target']:.2f}")
                print(f"RSI 4H: {result['rsi_4h']:.2f}")
                print(f"Actividad relativa: {result['activity_ratio']:.2f}x")
                print("Razones:")
                if result["reasons"]:
                    for r in result["reasons"]:
                        print("-", r)
                else:
                    print("- Sin condiciones cumplidas")
                print("")

                if result["alert"]:
                    alerts_found.append(result)

            except Exception as asset_error:
                print(f"=== {asset['symbol']} ===")
                print(f"Error evaluando activo: {asset_error}")
                print("")

            time.sleep(2)

        if alerts_found:
            for result in alerts_found:
                if already_alerted(result["symbol"]):
                    print(f"Alerta suprimida para {result['symbol']} (cooldown {ALERT_COOLDOWN_HOURS}h)")
                    continue

                message = (
                    f"🚦 ALERTA V3 {result['symbol']}\n"
                    f"Score: {result['score']:.1f}\n"
                    f"R:R: {result['rr']:.2f}\n"
                    f"Entry: {result['entry']:.2f}\n"
                    f"Stop: {result['stop']:.2f}\n"
                    f"Target: {result['target']:.2f}\n"
                    f"RSI 4H: {result['rsi_4h']:.2f}\n"
                    f"Actividad: {result['activity_ratio']:.2f}x\n"
                    f"Checklist:\n- " + "\n- ".join(result["reasons"])
                )
                send_telegram_message(message)
                mark_alerted(result["symbol"])
                print(f"Alerta enviada para {result['symbol']}")
        else:
            print("Sin alertas en ningún activo.")

    except Exception as e:
        print(f"Error general V3: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
