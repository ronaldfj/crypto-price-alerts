import os
import sys
import time
import requests
import pandas as pd
import numpy as np

COIN_ID = "bitcoin"
VS_CURRENCY = "usd"
SYMBOL = "BTCUSD"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MIN_SCORE = 6.0
MIN_RR = 2.0

# -----------------------------
# CoinGecko
# -----------------------------
def get_market_chart_range(coin_id: str, vs_currency: str, days_back: int = 120) -> pd.DataFrame:
    now = int(time.time())
    start = now - days_back * 24 * 60 * 60

    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart/range"
    params = {
        "vs_currency": vs_currency,
        "from": start,
        "to": now
    }

    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    data = response.json()

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

    # Proxy de actividad; no es volumen OHLCV de exchange
    out["volume"] = df["volume_proxy"].resample(rule).mean()

    out = out.dropna().reset_index()

    # eliminamos la vela en formación
    if len(out) > 1:
        out = out.iloc[:-1].copy().reset_index(drop=True)

    return out


# -----------------------------
# Indicators
# -----------------------------
def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    return result.fillna(50)


def atr(df: pd.DataFrame, length: int = 10) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()

    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    out = df.copy()
    out["atr"] = atr(out, period)

    hl2 = (out["high"] + out["low"]) / 2
    out["upperband"] = hl2 + multiplier * out["atr"]
    out["lowerband"] = hl2 - multiplier * out["atr"]

    out["final_upperband"] = np.nan
    out["final_lowerband"] = np.nan
    out["supertrend"] = np.nan
    out["st_direction"] = True

    for i in range(len(out)):
        if i == 0:
            out.loc[i, "final_upperband"] = out.loc[i, "upperband"]
            out.loc[i, "final_lowerband"] = out.loc[i, "lowerband"]
            out.loc[i, "supertrend"] = out.loc[i, "lowerband"]
            out.loc[i, "st_direction"] = True
            continue

        prev_fub = out.loc[i - 1, "final_upperband"]
        prev_flb = out.loc[i - 1, "final_lowerband"]
        prev_close = out.loc[i - 1, "close"]

        if out.loc[i, "upperband"] < prev_fub or prev_close > prev_fub:
            out.loc[i, "final_upperband"] = out.loc[i, "upperband"]
        else:
            out.loc[i, "final_upperband"] = prev_fub

        if out.loc[i, "lowerband"] > prev_flb or prev_close < prev_flb:
            out.loc[i, "final_lowerband"] = out.loc[i, "lowerband"]
        else:
            out.loc[i, "final_lowerband"] = prev_flb

        prev_st = out.loc[i - 1, "supertrend"]

        if prev_st == prev_fub:
            if out.loc[i, "close"] <= out.loc[i, "final_upperband"]:
                out.loc[i, "supertrend"] = out.loc[i, "final_upperband"]
                out.loc[i, "st_direction"] = False
            else:
                out.loc[i, "supertrend"] = out.loc[i, "final_lowerband"]
                out.loc[i, "st_direction"] = True
        else:
            if out.loc[i, "close"] >= out.loc[i, "final_lowerband"]:
                out.loc[i, "supertrend"] = out.loc[i, "final_lowerband"]
                out.loc[i, "st_direction"] = True
            else:
                out.loc[i, "supertrend"] = out.loc[i, "final_upperband"]
                out.loc[i, "st_direction"] = False

    return out


# -----------------------------
# Strategy logic
# -----------------------------
def is_hammer(candle: pd.Series) -> bool:
    body = abs(candle["close"] - candle["open"])
    total_range = candle["high"] - candle["low"]
    lower_shadow = min(candle["open"], candle["close"]) - candle["low"]
    upper_shadow = candle["high"] - max(candle["open"], candle["close"])

    if total_range == 0:
        return False

    return (
        lower_shadow >= body * 2
        and upper_shadow <= max(body, 1e-9)
        and body / total_range <= 0.4
    )


def in_key_zone_bullish(df: pd.DataFrame, threshold: float = 0.01, lookback: int = 20) -> bool:
    recent_low = df["low"].iloc[-lookback:].min()
    close = df["close"].iloc[-1]
    distance = abs(close - recent_low) / close
    return distance <= threshold


def relative_activity(df: pd.DataFrame, lookback: int = 20) -> float:
    if len(df) < lookback + 1:
        return 0.0

    last_val = df["volume"].iloc[-1]
    avg_val = df["volume"].iloc[-(lookback + 1):-1].mean()

    if avg_val == 0 or pd.isna(avg_val):
        return 0.0

    return float(last_val / avg_val)


def bullish_rsi_divergence(df: pd.DataFrame, pivot_window: int = 3, lookback: int = 60) -> bool:
    data = df.copy().tail(lookback).reset_index(drop=True)
    data["rsi"] = rsi(data["close"], 14)

    pivot_lows = []

    for i in range(pivot_window, len(data) - pivot_window):
        current_low = data.loc[i, "low"]
        left = data.loc[i - pivot_window:i - 1, "low"]
        right = data.loc[i + 1:i + pivot_window, "low"]

        if current_low < left.min() and current_low < right.min():
            pivot_lows.append(i)

    if len(pivot_lows) < 2:
        return False

    i1, i2 = pivot_lows[-2], pivot_lows[-1]

    price_lower_low = data.loc[i2, "low"] < data.loc[i1, "low"]
    rsi_higher_low = data.loc[i2, "rsi"] > data.loc[i1, "rsi"]

    return bool(price_lower_low and rsi_higher_low)


def calc_rr(df_4h: pd.DataFrame) -> float:
    entry = df_4h["close"].iloc[-1]
    stop = df_4h["low"].iloc[-1]
    target = df_4h["high"].iloc[-20:].max()

    risk = entry - stop
    reward = target - entry

    if risk <= 0:
        return 0.0

    return float(reward / risk)


def send_telegram_message(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()


def main():
    try:
        df_raw = get_market_chart_range(COIN_ID, VS_CURRENCY, days_back=180)

        # 4H, Diario y Semanal  
        df_4h = build_ohlcv_from_series(df_raw, "4h")
        df_d = build_ohlcv_from_series(df_raw, "1d")
        df_w = build_ohlcv_from_series(df_raw, "W-MON")
        # Indicadores
        df_4h = supertrend(df_4h)
        df_d = supertrend(df_d)
        df_w = supertrend(df_w)

        for df in [df_4h, df_d, df_w]:
            df["ema21"] = ema(df["close"], 21)
            df["rsi14"] = rsi(df["close"], 14)

        score = 0.0
        reasons = []

        # -----------------------------
        # Filtro 1 — Semanal
        # -----------------------------
        if bool(df_w["st_direction"].iloc[-1]):
            score += 1
            reasons.append("W: Supertrend verde (+1)")

        if df_w["close"].iloc[-1] > df_w["ema21"].iloc[-1]:
            score += 1
            reasons.append("W: Precio > EMA21 (+1)")

        if df_w["rsi14"].iloc[-1] > 50:
            score += 1
            reasons.append("W: RSI14 > 50 (+1)")

        # -----------------------------
        # Filtro 2 — Diario
        # -----------------------------
        if bool(df_d["st_direction"].iloc[-1]):
            score += 1
            reasons.append("D: Supertrend verde (+1)")

        if bullish_rsi_divergence(df_d):
            score += 1
            reasons.append("D: Divergencia alcista RSI (+1)")

        if df_d["rsi14"].iloc[-1] > 50:
            score += 1
            reasons.append("D: RSI14 > 50 (+1)")

        # -----------------------------
        # Filtro 3 — 4H
        # -----------------------------
        if is_hammer(df_4h.iloc[-1]) and in_key_zone_bullish(df_4h):
            score += 1
            reasons.append("4H: Martillo en zona clave (+1)")

        activity_ratio = relative_activity(df_4h, 20)
        if activity_ratio >= 1.5:
            score += 0.5
            reasons.append(f"4H: Actividad relativa {activity_ratio:.2f}x (+0.5)")

        rsi_4h = df_4h["rsi14"].iloc[-1]
        if rsi_4h < 30 or rsi_4h > 70:
            score += 1
            reasons.append(f"4H: RSI extremo {rsi_4h:.2f} (+1)")

        rr = calc_rr(df_4h)

        print(f"Score total: {score:.1f}/8.5")
        print(f"R:R estimado: {rr:.2f}")
        print("Razones:")
        for r in reasons:
            print("-", r)

        if score >= MIN_SCORE and rr >= MIN_RR:
            message = (
                f"🚦 ALERTA V2 {SYMBOL}\n"
                f"Score: {score:.1f}/8.5\n"
                f"R:R: {rr:.2f}\n"
                f"Detalles:\n- " + "\n- ".join(reasons)
            )
            send_telegram_message(message)
            print("Alerta enviada a Telegram.")
        else:
            print("Sin alerta. No cumple score o R:R.")

    except Exception as e:
        print(f"Error V2 CoinGecko: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
