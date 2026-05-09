"""
data_source.py — Fuente única de OHLCV para Crypto Sentinel Bot.

PROBLEMA RESUELTO: Binance bloquea peticiones desde IPs de cloud providers
(GitHub Actions runners corren en Azure US → 451 Unavailable For Legal Reasons).
Migración a Bybit Spot como primario + OKX como fallback. Ambos exchanges
tienen alta liquidez en los pares spot que el bot rastrea y no aplican
geo-bloqueos a sus endpoints públicos de mercado desde cloud.

Convenciones de retorno:
- DataFrame con columnas: ts (UTC tz-aware, OPEN time), Open, High, Low, Close,
  Volume, QuoteVolume, Trades, __source__.
- Última fila SIEMPRE es la última vela cerrada (las en curso se descartan).
- Ordenado ascendente por ts.

APIs utilizadas (públicas, sin auth):
- Bybit v5: https://api.bybit.com/v5/market/kline
- OKX v5:   https://www.okx.com/api/v5/market/candles
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
import requests


# ── Configuración ─────────────────────────────────────────────────────────────
BYBIT_BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com")
OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com")

HTTP_TIMEOUT = int(os.getenv("DATA_HTTP_TIMEOUT", "8"))
HTTP_RETRIES = int(os.getenv("DATA_HTTP_RETRIES", "2"))
HTTP_BACKOFF = float(os.getenv("DATA_HTTP_BACKOFF", "1.0"))

BYBIT_MAX_LIMIT = 1000
OKX_MAX_LIMIT = 300

DATA_SOURCE_ORDER = [
    s.strip().lower()
    for s in os.getenv("DATA_SOURCE_ORDER", "bybit,okx").split(",")
    if s.strip()
]


# ── Mapeos ────────────────────────────────────────────────────────────────────
SYMBOL_TO_BASE: Dict[str, Tuple[str, str]] = {
    "BTC": ("BTC", "USDT"),
    "ETH": ("ETH", "USDT"),
    "DOT": ("DOT", "USDT"),
    "TON": ("TON", "USDT"),
    "LTC": ("LTC", "USDT"),
    "XRP": ("XRP", "USDT"),
    "TRX": ("TRX", "USDT"),
    "XLM": ("XLM", "USDT"),
    "SOL": ("SOL", "USDT"),
    "LINK": ("LINK", "USDT"),
    "BNB": ("BNB", "USDT"),
}

# Compat: alert.py usa este nombre para construir deeplinks Binance.
SYMBOL_TO_BINANCE_PAIR: Dict[str, str] = {
    sym: f"{base}{quote}" for sym, (base, quote) in SYMBOL_TO_BASE.items()
}

# Bybit usa minutos como string para intra; "D"/"W" para diario+.
BYBIT_INTERVAL_MAP: Dict[str, str] = {
    "1m": "1", "3m": "3", "5m": "5",
    "15m": "15", "15min": "15",
    "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
    "1d": "D", "1D": "D",
    "1w": "W", "1W": "W",
}

# OKX: "1m","3m","5m","15m","30m","1H","2H","4H","6H","12H","1D","1W"
OKX_INTERVAL_MAP: Dict[str, str] = {
    "1m": "1m", "3m": "3m", "5m": "5m",
    "15m": "15m", "15min": "15m",
    "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
    "1d": "1D", "1D": "1D",
    "1w": "1W", "1W": "1W",
}

INTERVAL_SECONDS: Dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300,
    "15m": 900, "15min": 900,
    "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400, "1D": 86400,
    "1w": 604800, "1W": 604800,
}


# ── Helpers públicos ──────────────────────────────────────────────────────────
def symbol_to_pair(symbol: str) -> Optional[str]:
    """Compat: par concatenado tipo Binance/Bybit."""
    return SYMBOL_TO_BINANCE_PAIR.get(symbol.upper())


def normalize_interval(timeframe: str) -> Optional[str]:
    if timeframe in INTERVAL_SECONDS:
        return timeframe
    lower = timeframe.lower()
    return lower if lower in INTERVAL_SECONDS else None


def interval_seconds(timeframe: str) -> int:
    return INTERVAL_SECONDS.get(timeframe, 0)


# ── Bybit ─────────────────────────────────────────────────────────────────────
def _bybit_pair(symbol: str) -> Optional[str]:
    base = SYMBOL_TO_BASE.get(symbol.upper())
    return f"{base[0]}{base[1]}" if base else None


def _bybit_request(
    pair: str,
    interval: str,
    limit: int,
    end_ms: Optional[int] = None,
    start_ms: Optional[int] = None,
) -> Optional[List[List[Any]]]:
    url = f"{BYBIT_BASE_URL}/v5/market/kline"
    params: Dict[str, Any] = {
        "category": "spot",
        "symbol": pair,
        "interval": interval,
        "limit": min(max(limit, 1), BYBIT_MAX_LIMIT),
    }
    if end_ms is not None:
        params["end"] = end_ms
    if start_ms is not None:
        params["start"] = start_ms

    backoff = HTTP_BACKOFF
    for _ in range(HTTP_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                payload = r.json()
                if payload.get("retCode") == 0:
                    return payload.get("result", {}).get("list", []) or []
                print(f"⚠️ Bybit {pair} {interval}: retCode={payload.get('retCode')} msg={payload.get('retMsg')}")
                return None
            if r.status_code in (429, 418, 403):
                ra = float(r.headers.get("Retry-After", backoff))
                time.sleep(ra)
                backoff *= 2
                continue
            if r.status_code >= 500:
                time.sleep(backoff)
                backoff *= 2
                continue
            print(f"⚠️ Bybit {pair} {interval}: http {r.status_code}: {r.text[:160]}")
            return None
        except (requests.Timeout, requests.ConnectionError) as exc:
            print(f"⚠️ Bybit {pair} {interval}: network {exc}")
            time.sleep(backoff)
            backoff *= 2
        except Exception as exc:
            print(f"⚠️ Bybit {pair} {interval}: unexpected {exc}")
            return None
    return None


def _bybit_to_df(raw: List[List[Any]]) -> pd.DataFrame:
    """Bybit list: [start, open, high, low, close, volume, turnover]. DESC."""
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(
        raw,
        columns=["start", "open", "high", "low", "close", "volume", "turnover"],
    )
    for col in ("open", "high", "low", "close", "volume", "turnover"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["start"] = pd.to_datetime(df["start"].astype("int64"), unit="ms", utc=True)
    return df.sort_values("start").reset_index(drop=True)


def _bybit_fetch(
    symbol: str, norm_tf: str, candles_needed: int, drop_unclosed: bool, now_ts: int
) -> Optional[pd.DataFrame]:
    pair = _bybit_pair(symbol)
    if pair is None:
        return None
    interval = BYBIT_INTERVAL_MAP.get(norm_tf)
    if interval is None:
        return None

    raw = _bybit_request(pair, interval, candles_needed + 2)
    if raw is None or len(raw) == 0:
        return None
    df = _bybit_to_df(raw)
    if df.empty:
        return None

    seconds = interval_seconds(norm_tf)
    open_epoch = df["start"].astype("datetime64[ns, UTC]").astype("int64") // 10**9
    close_epoch = open_epoch + seconds
    if drop_unclosed:
        df = df[close_epoch <= now_ts].copy()
    if df.empty:
        return None

    out = df.rename(
        columns={
            "start": "ts", "open": "Open", "high": "High",
            "low": "Low", "close": "Close", "volume": "Volume",
            "turnover": "QuoteVolume",
        }
    )[["ts", "Open", "High", "Low", "Close", "Volume", "QuoteVolume"]]
    out["Trades"] = 0
    out["__source__"] = "bybit"
    return out.reset_index(drop=True)


# ── OKX ───────────────────────────────────────────────────────────────────────
def _okx_pair(symbol: str) -> Optional[str]:
    base = SYMBOL_TO_BASE.get(symbol.upper())
    return f"{base[0]}-{base[1]}" if base else None


def _okx_request(
    pair: str,
    interval: str,
    limit: int,
    after_ms: Optional[int] = None,
    before_ms: Optional[int] = None,
) -> Optional[List[List[Any]]]:
    url = f"{OKX_BASE_URL}/api/v5/market/candles"
    params: Dict[str, Any] = {
        "instId": pair,
        "bar": interval,
        "limit": min(max(limit, 1), OKX_MAX_LIMIT),
    }
    if after_ms is not None:
        params["after"] = after_ms
    if before_ms is not None:
        params["before"] = before_ms

    backoff = HTTP_BACKOFF
    for _ in range(HTTP_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                payload = r.json()
                if payload.get("code") == "0":
                    return payload.get("data", []) or []
                print(f"⚠️ OKX {pair} {interval}: code={payload.get('code')} msg={payload.get('msg')}")
                return None
            if r.status_code in (429, 418, 403):
                time.sleep(backoff)
                backoff *= 2
                continue
            if r.status_code >= 500:
                time.sleep(backoff)
                backoff *= 2
                continue
            print(f"⚠️ OKX {pair} {interval}: http {r.status_code}: {r.text[:160]}")
            return None
        except (requests.Timeout, requests.ConnectionError) as exc:
            print(f"⚠️ OKX {pair} {interval}: network {exc}")
            time.sleep(backoff)
            backoff *= 2
        except Exception as exc:
            print(f"⚠️ OKX {pair} {interval}: unexpected {exc}")
            return None
    return None


def _okx_to_df(raw: List[List[Any]]) -> pd.DataFrame:
    """OKX: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]. DESC."""
    if not raw:
        return pd.DataFrame()
    cols = ["ts", "open", "high", "low", "close", "volume", "volCcy", "volCcyQuote", "confirm"]
    raw_norm = [(row + [None] * len(cols))[: len(cols)] for row in raw]
    df = pd.DataFrame(raw_norm, columns=cols)
    for col in ("open", "high", "low", "close", "volume", "volCcy", "volCcyQuote"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
    df["confirm"] = df["confirm"].astype(str)
    return df.sort_values("ts").reset_index(drop=True)


def _okx_fetch(
    symbol: str, norm_tf: str, candles_needed: int, drop_unclosed: bool, now_ts: int
) -> Optional[pd.DataFrame]:
    pair = _okx_pair(symbol)
    if pair is None:
        return None
    interval = OKX_INTERVAL_MAP.get(norm_tf)
    if interval is None:
        return None

    needed = candles_needed + 2
    chunks: List[pd.DataFrame] = []
    cursor_after: Optional[int] = None
    safety = 0
    while needed > 0 and safety < 20:
        block = min(needed, OKX_MAX_LIMIT)
        raw = _okx_request(pair, interval, block, after_ms=cursor_after)
        if not raw:
            break
        chunk = _okx_to_df(raw)
        if chunk.empty:
            break
        chunks.append(chunk)
        oldest_ms = int(chunk["ts"].astype("datetime64[ns, UTC]").astype("int64").min() // 10**6)
        cursor_after = oldest_ms - 1
        needed -= len(chunk)
        safety += 1

    if not chunks:
        return None

    df = (
        pd.concat(chunks, ignore_index=True)
        .drop_duplicates(subset=["ts"])
        .sort_values("ts")
        .reset_index(drop=True)
    )

    if drop_unclosed:
        seconds = interval_seconds(norm_tf)
        open_epoch = df["ts"].astype("datetime64[ns, UTC]").astype("int64") // 10**9
        close_epoch = open_epoch + seconds
        df = df[(df["confirm"] == "1") | (close_epoch <= now_ts)].copy()

    if df.empty:
        return None

    out = df.rename(
        columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume", "volCcyQuote": "QuoteVolume",
        }
    )[["ts", "Open", "High", "Low", "Close", "Volume", "QuoteVolume"]]
    out["Trades"] = 0
    out["__source__"] = "okx"
    return out.reset_index(drop=True)


# ── API pública ───────────────────────────────────────────────────────────────
SOURCE_REGISTRY: Dict[str, Callable[..., Optional[pd.DataFrame]]] = {
    "bybit": _bybit_fetch,
    "okx": _okx_fetch,
}


def fetch_klines(
    symbol: str,
    timeframe: str,
    candles_needed: int,
    drop_unclosed: bool = True,
    now_ts: Optional[int] = None,
) -> Optional[pd.DataFrame]:
    norm = normalize_interval(timeframe)
    if norm is None:
        print(f"⚠️ Timeframe '{timeframe}' no soportado.")
        return None

    now = int(time.time()) if now_ts is None else int(now_ts)
    last_error = "no source attempted"

    for source_name in DATA_SOURCE_ORDER:
        fn = SOURCE_REGISTRY.get(source_name)
        if fn is None:
            continue
        df = fn(symbol, norm, candles_needed, drop_unclosed, now)
        if df is None or df.empty:
            last_error = f"{source_name} returned empty"
            continue
        if len(df) < candles_needed:
            print(f"ℹ️ {source_name}/{symbol} {norm}: {len(df)} velas (se pidieron {candles_needed}).")
        return df

    print(f"⚠️ {symbol} {norm}: ningún proveedor devolvió datos. Último: {last_error}")
    return None


def fetch_klines_range(
    symbol: str,
    timeframe: str,
    start_ts: int,
    end_ts: int,
) -> Optional[pd.DataFrame]:
    norm = normalize_interval(timeframe)
    if norm is None:
        return None
    seconds = interval_seconds(norm)
    if seconds == 0:
        return None
    candles_needed = max(1, int((end_ts - start_ts) / seconds) + 2)
    now = int(time.time())

    for source_name in DATA_SOURCE_ORDER:
        if source_name == "bybit":
            df = _bybit_fetch_range(symbol, norm, start_ts, end_ts, candles_needed)
        elif source_name == "okx":
            df = _okx_fetch_range(symbol, norm, start_ts, end_ts, candles_needed)
        else:
            continue
        if df is not None and not df.empty:
            ts_epoch = df["ts"].astype("datetime64[ns, UTC]").astype("int64") // 10**9
            close_epoch = ts_epoch + seconds
            df = df[(ts_epoch >= start_ts) & (close_epoch <= min(end_ts, now))].copy()
            if not df.empty:
                return df.reset_index(drop=True)
    return None


def _bybit_fetch_range(
    symbol: str, norm_tf: str, start_ts: int, end_ts: int, candles_needed: int
) -> Optional[pd.DataFrame]:
    pair = _bybit_pair(symbol)
    interval = BYBIT_INTERVAL_MAP.get(norm_tf)
    if pair is None or interval is None:
        return None
    chunks: List[pd.DataFrame] = []
    cursor_end_ms = int(end_ts * 1000)
    remaining = candles_needed
    safety = 0
    while remaining > 0 and safety < 20:
        block = min(remaining, BYBIT_MAX_LIMIT)
        raw = _bybit_request(pair, interval, block, end_ms=cursor_end_ms)
        if not raw:
            break
        chunk = _bybit_to_df(raw)
        chunks.append(chunk)
        oldest_ms = int(chunk["start"].astype("datetime64[ns, UTC]").astype("int64").min() // 10**6)
        if oldest_ms <= start_ts * 1000:
            break
        cursor_end_ms = oldest_ms - 1
        remaining -= block
        safety += 1
    if not chunks:
        return None
    df = (
        pd.concat(chunks, ignore_index=True)
        .drop_duplicates(subset=["start"])
        .sort_values("start")
        .reset_index(drop=True)
    )
    out = df.rename(
        columns={
            "start": "ts", "open": "Open", "high": "High",
            "low": "Low", "close": "Close", "volume": "Volume",
            "turnover": "QuoteVolume",
        }
    )[["ts", "Open", "High", "Low", "Close", "Volume", "QuoteVolume"]]
    out["Trades"] = 0
    out["__source__"] = "bybit"
    return out


def _okx_fetch_range(
    symbol: str, norm_tf: str, start_ts: int, end_ts: int, candles_needed: int
) -> Optional[pd.DataFrame]:
    pair = _okx_pair(symbol)
    interval = OKX_INTERVAL_MAP.get(norm_tf)
    if pair is None or interval is None:
        return None
    chunks: List[pd.DataFrame] = []
    cursor_after = int(end_ts * 1000)
    remaining = candles_needed
    safety = 0
    while remaining > 0 and safety < 20:
        block = min(remaining, OKX_MAX_LIMIT)
        raw = _okx_request(pair, interval, block, after_ms=cursor_after)
        if not raw:
            break
        chunk = _okx_to_df(raw)
        chunks.append(chunk)
        oldest_ms = int(chunk["ts"].astype("datetime64[ns, UTC]").astype("int64").min() // 10**6)
        if oldest_ms <= start_ts * 1000:
            break
        cursor_after = oldest_ms - 1
        remaining -= block
        safety += 1
    if not chunks:
        return None
    df = (
        pd.concat(chunks, ignore_index=True)
        .drop_duplicates(subset=["ts"])
        .sort_values("ts")
        .reset_index(drop=True)
    )
    out = df.rename(
        columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume", "volCcyQuote": "QuoteVolume",
        }
    )[["ts", "Open", "High", "Low", "Close", "Volume", "QuoteVolume"]]
    out["Trades"] = 0
    out["__source__"] = "okx"
    return out


def fetch_latest_price(symbol: str) -> Optional[float]:
    """Bybit ticker primero, OKX fallback."""
    pair = _bybit_pair(symbol)
    if pair is not None:
        try:
            r = requests.get(
                f"{BYBIT_BASE_URL}/v5/market/tickers",
                params={"category": "spot", "symbol": pair},
                timeout=HTTP_TIMEOUT,
            )
            if r.status_code == 200:
                payload = r.json()
                if payload.get("retCode") == 0:
                    items = payload.get("result", {}).get("list", [])
                    if items:
                        price = float(items[0].get("lastPrice", 0) or 0)
                        if price > 0:
                            return price
        except Exception:
            pass

    okx_pair = _okx_pair(symbol)
    if okx_pair is not None:
        try:
            r = requests.get(
                f"{OKX_BASE_URL}/api/v5/market/ticker",
                params={"instId": okx_pair},
                timeout=HTTP_TIMEOUT,
            )
            if r.status_code == 200:
                payload = r.json()
                if payload.get("code") == "0":
                    items = payload.get("data", [])
                    if items:
                        price = float(items[0].get("last", 0) or 0)
                        if price > 0:
                            return price
        except Exception:
            pass

    return None


# ── Health check ──────────────────────────────────────────────────────────────
def health_check() -> Tuple[bool, str]:
    df = fetch_klines("BTC", "4h", 5)
    if df is None or df.empty:
        return False, "no klines from any source"
    src = df["__source__"].iloc[-1] if "__source__" in df.columns else "?"
    if df["Volume"].sum() <= 0:
        return False, f"zero volume from {src}"
    return True, f"ok | source={src} | last_close={df.iloc[-1]['Close']:.2f}"


if __name__ == "__main__":
    ok, msg = health_check()
    print(f"Health: {ok} | {msg}")
