"""
data_source.py — Fuente única de OHLCV para Crypto Sentinel Bot.

Cambia CoinGecko (precios indicativos agregados) por Binance Spot klines
(libro de órdenes real). Validación explícita de vela cerrada.

Endpoints públicos sin auth: https://api.binance.com/api/v3/klines
Documentación: https://developers.binance.com/docs/binance-spot-api-docs/rest-api

Convenciones de retorno:
- DataFrame con columnas: ts (UTC tz-aware), Open, High, Low, Close, Volume, QuoteVolume, Trades
- Última fila SIEMPRE es la última vela cerrada (las en curso se descartan).
- Ordenado ascendente por ts.
"""

from __future__ import annotations

import os
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests


# ── Configuración ─────────────────────────────────────────────────────────────
BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://api.binance.com")
BINANCE_TIMEOUT = int(os.getenv("BINANCE_TIMEOUT", "15"))
BINANCE_RETRIES = int(os.getenv("BINANCE_RETRIES", "3"))
BINANCE_BACKOFF = float(os.getenv("BINANCE_BACKOFF", "1.5"))
BINANCE_MAX_KLINES_PER_CALL = 1000  # Hard cap del endpoint.

# Mapeo símbolo lógico → par Binance Spot. Debe quedar alineado con BINANCE_PAIRS
# en alert.py. Se redefine aquí para evitar dependencia circular.
SYMBOL_TO_BINANCE_PAIR: Dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "DOT": "DOTUSDT",
    "TON": "TONUSDT",
    "LTC": "LTCUSDT",
    "XRP": "XRPUSDT",
    "TRX": "TRXUSDT",
    "XLM": "XLMUSDT",
    "SOL": "SOLUSDT",
    "LINK": "LINKUSDT",
    "BNB": "BNBUSDT",
}

# Mapeo de timeframe lógico → intervalo Binance.
INTERVAL_MAP: Dict[str, str] = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "15min": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "8h": "8h",
    "12h": "12h",
    "1d": "1d",
    "1D": "1d",
    "3d": "3d",
    "1w": "1w",
    "1W": "1w",
}

INTERVAL_SECONDS: Dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "3d": 259200,
    "1w": 604800,
}

KLINE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
]


# ── Helpers públicos ──────────────────────────────────────────────────────────
def normalize_interval(timeframe: str) -> Optional[str]:
    """Mapea un timeframe lógico a un intervalo Binance válido."""
    if timeframe in INTERVAL_MAP:
        return INTERVAL_MAP[timeframe]
    lower = timeframe.lower()
    return INTERVAL_MAP.get(lower)


def interval_seconds(binance_interval: str) -> int:
    return INTERVAL_SECONDS.get(binance_interval, 0)


def symbol_to_pair(symbol: str) -> Optional[str]:
    return SYMBOL_TO_BINANCE_PAIR.get(symbol.upper())


# ── Fetch ─────────────────────────────────────────────────────────────────────
def _request_klines(
    pair: str,
    interval: str,
    limit: int,
    end_time_ms: Optional[int] = None,
) -> Optional[List[List[float]]]:
    """Llamada cruda a /api/v3/klines con retry y backoff exponencial."""
    url = f"{BINANCE_BASE_URL}/api/v3/klines"
    params: Dict[str, object] = {
        "symbol": pair,
        "interval": interval,
        "limit": min(max(limit, 1), BINANCE_MAX_KLINES_PER_CALL),
    }
    if end_time_ms is not None:
        params["endTime"] = end_time_ms

    backoff = BINANCE_BACKOFF
    last_error = ""
    for attempt in range(BINANCE_RETRIES):
        try:
            response = requests.get(url, params=params, timeout=BINANCE_TIMEOUT)
            if response.status_code == 200:
                return response.json()
            if response.status_code in (418, 429):
                # Rate limit: respetar Retry-After si existe.
                retry_after = float(response.headers.get("Retry-After", backoff))
                time.sleep(retry_after)
                backoff *= 2
                last_error = f"rate limit {response.status_code}"
                continue
            if response.status_code >= 500:
                time.sleep(backoff)
                backoff *= 2
                last_error = f"server {response.status_code}"
                continue
            last_error = f"http {response.status_code}: {response.text[:180]}"
            break
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = f"network: {exc}"
            time.sleep(backoff)
            backoff *= 2
        except Exception as exc:
            last_error = f"unexpected: {exc}"
            break

    print(f"⚠️ Binance klines {pair} {interval}: {last_error}")
    return None


def _klines_to_df(raw: List[List[float]]) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=KLINE_COLUMNS)
    for col in ("open", "high", "low", "close", "volume", "quote_volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["trades"] = pd.to_numeric(df["trades"], errors="coerce").fillna(0).astype("int64")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df


def fetch_klines(
    symbol: str,
    timeframe: str,
    candles_needed: int,
    drop_unclosed: bool = True,
    now_ts: Optional[int] = None,
) -> Optional[pd.DataFrame]:
    """
    Descarga `candles_needed` velas cerradas de Binance Spot para el símbolo.

    Devuelve DataFrame con: ts, Open, High, Low, Close, Volume, QuoteVolume, Trades.
    `ts` es el OPEN time de la vela (estándar de OHLCV).
    Última fila garantizada cerrada cuando drop_unclosed=True.
    """
    pair = symbol_to_pair(symbol)
    if pair is None:
        print(f"⚠️ Símbolo {symbol} no tiene par Binance mapeado.")
        return None

    interval = normalize_interval(timeframe)
    if interval is None:
        print(f"⚠️ Timeframe {timeframe} no soportado por Binance.")
        return None

    # Pedimos un margen extra para descartar la vela en curso si aplica.
    limit = min(candles_needed + 2, BINANCE_MAX_KLINES_PER_CALL)
    raw = _request_klines(pair, interval, limit)
    if raw is None or len(raw) == 0:
        return None

    df = _klines_to_df(raw)
    if df.empty:
        return None

    # Validación explícita de vela cerrada: close_time del candle debe ser <= now.
    # Forzamos dtype a ns para que // 10**9 dé segundos consistentemente
    # (pandas modernos pueden devolver datetime64[ms] preservando la unit).
    if drop_unclosed:
        now = int(time.time()) if now_ts is None else int(now_ts)
        close_epoch = df["close_time"].astype("datetime64[ns, UTC]").astype("int64") // 10**9
        df = df[close_epoch < now].copy()

    if df.empty:
        print(f"⚠️ {symbol} {interval}: ninguna vela cerrada disponible.")
        return None

    if len(df) < candles_needed:
        print(
            f"⚠️ {symbol} {interval}: solo {len(df)} velas cerradas (se pidieron {candles_needed})."
        )

    out = df.rename(
        columns={
            "open_time": "ts",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
            "quote_volume": "QuoteVolume",
            "trades": "Trades",
        }
    )[["ts", "Open", "High", "Low", "Close", "Volume", "QuoteVolume", "Trades"]]

    return out.sort_values("ts").reset_index(drop=True)


def fetch_klines_range(
    symbol: str,
    timeframe: str,
    start_ts: int,
    end_ts: int,
) -> Optional[pd.DataFrame]:
    """
    Descarga velas en un rango temporal arbitrario [start_ts, end_ts] (epoch s).
    Itera con paginación de 1000 en 1000 si el rango lo requiere.

    Para validación de outcomes de alertas y backtesting punto-en-tiempo.
    """
    pair = symbol_to_pair(symbol)
    if pair is None:
        return None
    interval = normalize_interval(timeframe)
    if interval is None:
        return None

    seconds = interval_seconds(interval)
    if seconds == 0:
        return None

    candles_needed = max(1, int((end_ts - start_ts) / seconds) + 2)
    chunks: List[pd.DataFrame] = []
    cursor_end_ms = int(end_ts * 1000)
    remaining = candles_needed

    # Iteramos hacia atrás desde end_ts en bloques de hasta 1000.
    while remaining > 0:
        block = min(remaining, BINANCE_MAX_KLINES_PER_CALL)
        raw = _request_klines(pair, interval, block, end_time_ms=cursor_end_ms)
        if not raw:
            break
        df = _klines_to_df(raw)
        chunks.append(df)
        oldest_open = df["open_time"].min()
        if pd.isna(oldest_open):
            break
        oldest_open_ms = int(oldest_open.value // 10**6)
        if oldest_open_ms <= start_ts * 1000:
            break
        cursor_end_ms = oldest_open_ms - 1
        remaining -= block

    if not chunks:
        return None

    df_all = pd.concat(chunks, ignore_index=True).drop_duplicates(subset=["open_time"])
    df_all = df_all.sort_values("open_time").reset_index(drop=True)

    # Filtro estricto al rango pedido y vela cerrada.
    # Forzamos dtype a ns para evitar inconsistencias entre versiones de pandas.
    open_epoch = df_all["open_time"].astype("datetime64[ns, UTC]").astype("int64") // 10**9
    close_epoch = df_all["close_time"].astype("datetime64[ns, UTC]").astype("int64") // 10**9
    mask = (open_epoch >= start_ts) & (close_epoch <= end_ts)
    df_all = df_all[mask].copy()

    if df_all.empty:
        return None

    out = df_all.rename(
        columns={
            "open_time": "ts",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
            "quote_volume": "QuoteVolume",
            "trades": "Trades",
        }
    )[["ts", "Open", "High", "Low", "Close", "Volume", "QuoteVolume", "Trades"]]

    return out.reset_index(drop=True)


def fetch_latest_price(symbol: str) -> Optional[float]:
    """Precio spot reciente vía /api/v3/ticker/price (fallback robusto)."""
    pair = symbol_to_pair(symbol)
    if pair is None:
        return None
    url = f"{BINANCE_BASE_URL}/api/v3/ticker/price"
    try:
        response = requests.get(url, params={"symbol": pair}, timeout=BINANCE_TIMEOUT)
        if response.status_code != 200:
            return None
        price = float(response.json().get("price", 0.0))
        return price if price > 0 else None
    except Exception:
        return None


# ── Mantenimiento ─────────────────────────────────────────────────────────────
def health_check() -> Tuple[bool, str]:
    """Sanity check para CI: confirma que el endpoint responde y devuelve OHLCV."""
    df = fetch_klines("BTC", "4h", 5)
    if df is None or df.empty:
        return False, "no klines"
    if df["Volume"].sum() <= 0:
        return False, "zero volume"
    return True, f"ok | last_close={df.iloc[-1]['Close']:.2f}"


if __name__ == "__main__":
    ok, msg = health_check()
    print(f"Health: {ok} | {msg}")
