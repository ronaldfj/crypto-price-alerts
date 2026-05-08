
"""
Crypto Sentinel Bot — versión simplificada y más ejecutable.

Objetivo de esta iteración:
- aumentar la frecuencia útil de alertas sin bajar demasiado la calidad
- simplificar la lógica: 4H manda, 1D y 15m acompañan
- evitar que el execution gate mate señales demasiado pronto
- reducir resúmenes vacíos de "no hay nada"
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests

import data_source


# ── Universo ──────────────────────────────────────────────────────────────────
CRYPTO_IDS = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "polkadot": "DOT",
    "the-open-network": "TON",
    "litecoin": "LTC",
    "ripple": "XRP",
    "tron": "TRX",
    "stellar": "XLM",
    "solana": "SOL",
    "chainlink": "LINK",
    "binancecoin": "BNB",
}

ASSET_GROUPS = {
    "BTC": "Majors",
    "ETH": "Majors",
    "TON": "Layer1",
    "SOL": "Layer1",
    "DOT": "Layer1",
    "BNB": "Exchange",
    "LINK": "Infra",
    "TRX": "Payments",
    "XRP": "Payments",
    "XLM": "Payments",
    "LTC": "Legacy",
}

BINANCE_QUOTE = os.getenv("BINANCE_QUOTE", "USDT")
BINANCE_PAIRS = {
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

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"
VALID_SIDES = (SIDE_LONG, SIDE_SHORT)

ACTIVE = "ACTIVE"
INVALIDATED = "INVALIDATED"
EXPIRED = "EXPIRED"
CLOSED = "CLOSED"

VALIDATION_PENDING = "PENDING"
VALIDATION_RESOLVED = "RESOLVED"

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CG_API_KEY = os.getenv("COINGECKO_API_KEY", "")

DB_FILE = os.getenv("ALERT_DB_FILE", "alerts_state.db")
LEGACY_STATE_FILE = os.getenv("LEGACY_STATE_FILE", "alert_state.json")
MARKET_CONTEXT_FILE = os.getenv("MARKET_CONTEXT_FILE", "market_context.json")

MACRO_TIMEFRAME = os.getenv("MACRO_TIMEFRAME", "1d")
TRADING_TIMEFRAME = os.getenv("TRADING_TIMEFRAME", "4h")
ENTRY_TIMEFRAME = os.getenv("ENTRY_TIMEFRAME", "15m")

COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "12"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "5.2"))
MIN_RR = float(os.getenv("MIN_RR", "1.35"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
SLEEP_BETWEEN_ASSETS = float(os.getenv("SLEEP_BETWEEN_ASSETS", "0.6"))
FIB_LOOKBACK = int(os.getenv("FIB_LOOKBACK", "55"))
SETUP_SWING_LOOKBACK = int(os.getenv("SETUP_SWING_LOOKBACK", "12"))
MAX_ALERTS_PER_RUN = int(os.getenv("MAX_ALERTS_PER_RUN", "3"))
MAX_ALERTS_PER_GROUP = int(os.getenv("MAX_ALERTS_PER_GROUP", "2"))
SEND_RUN_SUMMARY = os.getenv("SEND_RUN_SUMMARY", "false").lower() == "true"
ENABLE_RANKING = os.getenv("ENABLE_RANKING", "true").lower() == "true"

DEFAULT_ALLOWED_SIDES = os.getenv("DEFAULT_ALLOWED_SIDES", "LONG,SHORT")
ENABLE_SHORT_ALERTS = os.getenv("ENABLE_SHORT_ALERTS", "true").lower() == "true"

ALERT_FORWARD_BARS = int(os.getenv("ALERT_FORWARD_BARS", "18"))
SEND_OUTCOME_UPDATES = os.getenv("SEND_OUTCOME_UPDATES", "false").lower() == "true"

TACTICAL_MIN_SCORE = float(os.getenv("TACTICAL_MIN_SCORE", "5.8"))
ENABLE_EXECUTION_QUALITY_GATE = os.getenv("ENABLE_EXECUTION_QUALITY_GATE", "true").lower() == "true"

# Gate relajado: solo bloquea cuando ya es claramente tarde o inválido.
EXECUTION_BLOCK_PROGRESS_TO_TP1 = float(os.getenv("EXECUTION_BLOCK_PROGRESS_TO_TP1", "0.85"))
EXECUTION_CAUTION_PROGRESS_TO_TP1 = float(os.getenv("EXECUTION_CAUTION_PROGRESS_TO_TP1", "0.55"))
EXECUTION_MIN_CURRENT_RR_TP1 = float(os.getenv("EXECUTION_MIN_CURRENT_RR_TP1", "0.25"))
EXECUTION_CAUTION_CURRENT_RR_TP1 = float(os.getenv("EXECUTION_CAUTION_CURRENT_RR_TP1", "0.55"))

DEFAULT_TRADE_USD = float(os.getenv("DEFAULT_TRADE_USD", "10"))
ENABLE_BINANCE_DEEPLINK = os.getenv("ENABLE_BINANCE_DEEPLINK", "true").lower() == "true"
ENABLE_TRADINGVIEW_LINK = os.getenv("ENABLE_TRADINGVIEW_LINK", "true").lower() == "true"


# ── Utils Telegram / links ────────────────────────────────────────────────────
def send_telegram(message: str, reply_markup: Optional[Dict[str, Any]] = None) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram no configurado. Se omite el envío.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"⚠️ Telegram respondió {response.status_code}: {response.text[:240]}")
            return False
        body = response.json()
        return bool(body.get("ok", False))
    except Exception as exc:
        print(f"❌ Error enviando a Telegram: {exc}")
        return False


def binance_spot_url(symbol: str) -> Optional[str]:
    pair = BINANCE_PAIRS.get(symbol)
    if not pair:
        return None
    base = pair.replace(BINANCE_QUOTE, "")
    return f"https://www.binance.com/en/trade/{base}_{BINANCE_QUOTE}?type=spot"


def tradingview_url(symbol: str) -> str:
    pair = BINANCE_PAIRS.get(symbol, f"{symbol}{BINANCE_QUOTE}")
    return f"https://www.tradingview.com/chart/?symbol=BINANCE:{pair}"


def estimate_qty(price: float, usd_amount: float = DEFAULT_TRADE_USD) -> str:
    if price <= 0 or not math.isfinite(price):
        return "0"
    qty = usd_amount / price
    if price >= 1000:
        return f"{qty:.6f}"
    if price >= 1:
        return f"{qty:.4f}"
    if price >= 0.01:
        return f"{qty:.2f}"
    return f"{qty:.0f}"


def build_inline_keyboard(candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not ENABLE_BINANCE_DEEPLINK:
        return None
    spot_url = binance_spot_url(candidate["symbol"])
    if not spot_url:
        return None
    rows: List[List[Dict[str, str]]] = [[{"text": f"💱 Binance Spot {candidate['symbol']}/{BINANCE_QUOTE}", "url": spot_url}]]
    if ENABLE_TRADINGVIEW_LINK:
        rows[0].append({"text": "📈 TradingView", "url": tradingview_url(candidate["symbol"])})
    return {"inline_keyboard": rows}


# ── Persistencia ──────────────────────────────────────────────────────────────
def get_db_connection(db_file: str = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _alerts_extra_columns() -> Dict[str, str]:
    return {
        "tp1": "REAL",
        "tp2": "REAL",
        "tp1_rr": "REAL",
        "tp2_rr": "REAL",
        "move_to_be_rr": "REAL",
        "breakeven_trigger": "REAL",
        "risk_multiplier": "REAL",
        "expiry_ts": "INTEGER",
        "validation_status": f"TEXT NOT NULL DEFAULT '{VALIDATION_PENDING}'",
        "validation_result": "TEXT",
        "validated_at": "INTEGER",
        "outcome_price": "REAL",
        "outcome_rr": "REAL",
        "bars_to_outcome": "INTEGER",
        "tp1_hit": "INTEGER NOT NULL DEFAULT 0",
        "tp2_hit": "INTEGER NOT NULL DEFAULT 0",
        "close_at_expiry": "REAL",
        "outcome_note": "TEXT",
        "validation_notified_at": "INTEGER",
    }


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            cg_id TEXT NOT NULL,
            side TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            setup_key TEXT NOT NULL,
            setup_hash TEXT NOT NULL,
            regime TEXT NOT NULL,
            rsi_bucket TEXT NOT NULL,
            fib_zone TEXT NOT NULL,
            price_bucket TEXT NOT NULL,
            candle_ts INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            rr_ratio REAL NOT NULL,
            score REAL NOT NULL,
            adx REAL NOT NULL,
            rsi REAL NOT NULL,
            atr REAL NOT NULL,
            reasons_json TEXT NOT NULL,
            invalidation_reason TEXT,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            sent_at INTEGER NOT NULL,
            invalidated_at INTEGER,
            improved_from_alert_id INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_alerts_symbol_side_timeframe
        ON alerts(symbol, side, timeframe, status, sent_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_alerts_setup_hash
        ON alerts(setup_hash, status, sent_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS legacy_symbol_cooldowns (
            symbol TEXT PRIMARY KEY,
            sent_at INTEGER NOT NULL
        )
        """
    )
    _migrate_alerts_table(conn)
    conn.commit()


def _migrate_alerts_table(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    for col_name, ddl in _alerts_extra_columns().items():
        if col_name not in existing:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {col_name} {ddl}")

    tf_seconds = timeframe_to_seconds(TRADING_TIMEFRAME)
    conn.execute(
        """
        UPDATE alerts
        SET expiry_ts = COALESCE(expiry_ts, candle_ts + ?),
            validation_status = COALESCE(validation_status, ?)
        """,
        (ALERT_FORWARD_BARS * tf_seconds, VALIDATION_PENDING),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def import_legacy_state_if_needed(conn: sqlite3.Connection) -> None:
    if get_meta(conn, "legacy_import_done", "0") == "1":
        return

    legacy_path = Path(LEGACY_STATE_FILE)
    if not legacy_path.exists():
        set_meta(conn, "legacy_import_done", "1")
        return

    try:
        legacy_data = json.loads(legacy_path.read_text())
        if isinstance(legacy_data, dict):
            for symbol, raw_ts in legacy_data.items():
                if isinstance(symbol, str) and isinstance(raw_ts, (int, float)):
                    conn.execute(
                        """
                        INSERT INTO legacy_symbol_cooldowns(symbol, sent_at) VALUES(?, ?)
                        ON CONFLICT(symbol) DO UPDATE SET sent_at = excluded.sent_at
                        """,
                        (symbol.upper(), int(raw_ts)),
                    )
            conn.commit()
    except Exception as exc:
        print(f"⚠️ No se pudo migrar {LEGACY_STATE_FILE}: {exc}")
    finally:
        set_meta(conn, "legacy_import_done", "1")


# ── Contexto ──────────────────────────────────────────────────────────────────
def load_market_context(path: str = MARKET_CONTEXT_FILE) -> Dict[str, Any]:
    context_path = Path(path)
    if not context_path.exists():
        return {}
    try:
        raw = json.loads(context_path.read_text())
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:
        print(f"⚠️ No se pudo leer {path}: {exc}")
        return {}


def normalize_context(context: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    global_ctx = context.get("GLOBAL", {})
    asset_ctx = context.get(symbol, {})
    if isinstance(global_ctx, dict):
        merged.update(global_ctx)
    if isinstance(asset_ctx, dict):
        merged.update(asset_ctx)
    return merged


def context_float(context: Dict[str, Any], key: str, default: float) -> float:
    raw = context.get(key, default)
    try:
        value = float(raw)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def context_bool(context: Dict[str, Any], key: str, default: bool = False) -> bool:
    raw = context.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(raw)


def parse_allowed_sides(context: Dict[str, Any]) -> List[str]:
    raw = context.get("allowed_sides")
    parsed: List[str]
    if isinstance(raw, list):
        parsed = [str(side).upper().strip() for side in raw if str(side).upper().strip() in VALID_SIDES]
    elif isinstance(raw, str) and raw.strip():
        parsed = [chunk.strip().upper() for chunk in raw.split(",") if chunk.strip().upper() in VALID_SIDES]
    else:
        parsed = [chunk.strip().upper() for chunk in DEFAULT_ALLOWED_SIDES.split(",") if chunk.strip().upper() in VALID_SIDES]

    if not parsed:
        parsed = [SIDE_LONG]

    if not ENABLE_SHORT_ALERTS:
        parsed = [side for side in parsed if side != SIDE_SHORT] or [SIDE_LONG]

    return list(dict.fromkeys(parsed))


# ── Datos ─────────────────────────────────────────────────────────────────────
def fetch_ohlc_for_symbol(symbol: str, timeframe: str, candles_needed: int) -> Optional[pd.DataFrame]:
    return data_source.fetch_klines(symbol, timeframe, candles_needed, drop_unclosed=True)


def fetch_btc_dominance() -> Optional[float]:
    url = "https://api.coingecko.com/api/v3/global"
    headers = {"accept": "application/json"}
    if CG_API_KEY:
        headers["x-cg-demo-api-key"] = CG_API_KEY
    try:
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            pct = r.json().get("data", {}).get("market_cap_percentage", {}).get("btc")
            return round(float(pct), 1) if pct is not None else None
    except Exception:
        pass
    return None


# ── Indicadores ───────────────────────────────────────────────────────────────
def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def compute_adx(df: pd.DataFrame, period: int = 14) -> Tuple[pd.Series, pd.Series, pd.Series]:
    up_move = df["High"].diff()
    down_move = -df["Low"].diff()

    plus_dm = pd.Series(0.0, index=df.index)
    minus_dm = pd.Series(0.0, index=df.index)

    plus_mask = (up_move > down_move) & (up_move > 0)
    minus_mask = (down_move > up_move) & (down_move > 0)
    plus_dm[plus_mask] = up_move[plus_mask]
    minus_dm[minus_mask] = down_move[minus_mask]

    atr = compute_atr(df, period)
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr.replace(0, 1e-9))
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr.replace(0, 1e-9))
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9))
    adx = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return adx, plus_di, minus_di


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    work["ema20"] = work["Close"].ewm(span=20, adjust=False).mean()
    work["ema50"] = work["Close"].ewm(span=50, adjust=False).mean()
    work["ema200"] = work["Close"].ewm(span=200, adjust=False).mean()
    work["rsi"] = compute_rsi(work["Close"], 14)
    work["atr"] = compute_atr(work, 14)
    work["adx"], work["plus_di"], work["minus_di"] = compute_adx(work, 14)
    return work.dropna().reset_index(drop=True)


def fibonacci_context(df: pd.DataFrame, lookback: int = FIB_LOOKBACK) -> Dict[str, Any]:
    recent = df.tail(lookback).copy()
    swing_low = float(recent["Low"].min())
    swing_high = float(recent["High"].max())
    close = float(recent.iloc[-1]["Close"])
    amplitude = max(swing_high - swing_low, 1e-9)

    retracement = (swing_high - close) / amplitude
    pullback_from_low = (close - swing_low) / amplitude
    retracement = max(0.0, min(1.0, retracement))
    pullback_from_low = max(0.0, min(1.0, pullback_from_low))

    return {
        "swing_low": swing_low,
        "swing_high": swing_high,
        "retracement": retracement,
        "pullback_from_low": pullback_from_low,
        "fib_382": swing_high - amplitude * 0.382,
        "fib_500": swing_high - amplitude * 0.500,
        "fib_618": swing_high - amplitude * 0.618,
        "fib_786": swing_high - amplitude * 0.786,
        "fib_from_low_382": swing_low + amplitude * 0.382,
        "fib_from_low_500": swing_low + amplitude * 0.500,
        "fib_from_low_618": swing_low + amplitude * 0.618,
        "fib_from_low_786": swing_low + amplitude * 0.786,
        "amplitude": amplitude,
    }


def compute_vwap_proximity(df: pd.DataFrame, lookback: int = 20) -> Dict[str, Any]:
    recent = df.tail(lookback).copy()
    typical = (recent["High"] + recent["Low"] + recent["Close"]) / 3
    weight = recent["Volume"] if "Volume" in recent.columns and recent["Volume"].notna().any() else (recent["High"] - recent["Low"]).clip(lower=1e-9)
    vwap = (typical * weight).sum() / max(weight.sum(), 1e-9)
    close = float(recent.iloc[-1]["Close"])
    distance_pct = (close - vwap) / max(vwap, 1e-9) * 100
    return {
        "vwap": round(float(vwap), 6),
        "distance_pct": round(float(distance_pct), 2),
        "above_vwap": close >= vwap,
    }


def compute_volume_momentum(df: pd.DataFrame, lookback: int = 10) -> Dict[str, Any]:
    recent = df.tail(lookback).copy()
    has_real_volume = "Volume" in recent.columns and recent["Volume"].notna().sum() >= max(4, len(recent) // 2)
    vol = recent["Volume"].fillna(0.0) if has_real_volume else (recent["High"] - recent["Low"]).clip(lower=1e-9)
    avg_vol = float(vol.mean())
    last_3_avg = float(vol.tail(3).mean())
    relative_volume = last_3_avg / max(avg_vol, 1e-9)
    last_close = float(recent.iloc[-1]["Close"])
    ref_close = float(recent.iloc[max(len(recent) - 4, 0)]["Close"])
    price_up = last_close > ref_close
    price_down = last_close < ref_close
    vol_declining = last_3_avg < avg_vol * 0.85
    return {
        "relative_volume": round(relative_volume, 2),
        "divergence_up": price_up and vol_declining,
        "divergence_down": price_down and vol_declining,
        "strong_momentum": relative_volume >= 1.08,
        "volume_source": "real" if has_real_volume else "proxy",
    }


def get_regime(row: pd.Series) -> str:
    ema20 = float(row["ema20"])
    ema50 = float(row["ema50"])
    ema200 = float(row["ema200"])
    if ema20 > ema50 > ema200:
        return "BULL_STACK"
    if ema20 < ema50 < ema200:
        return "BEAR_STACK"
    return "MIXED"


def rsi_bucket(rsi: float) -> str:
    base = int(max(0, min(95, math.floor(rsi / 5) * 5)))
    return f"{base:02d}-{base + 4:02d}"


def fib_zone(value: float) -> str:
    if 0.236 <= value < 0.382:
        return "0.236-0.382"
    if 0.382 <= value < 0.500:
        return "0.382-0.500"
    if 0.500 <= value < 0.618:
        return "0.500-0.618"
    if 0.618 <= value <= 0.786:
        return "0.618-0.786"
    return "OUTSIDE"


def price_bucket(price: float, atr: float) -> str:
    step = max(atr * 0.7, price * 0.0045, 1e-9)
    return str(int(round(price / step)))


def timeframe_to_seconds(timeframe: str) -> int:
    tf = timeframe.strip().lower()
    mapping = {
        "15m": 15 * 60,
        "15min": 15 * 60,
        "1h": 60 * 60,
        "4h": 4 * 60 * 60,
        "1d": 24 * 60 * 60,
        "1w": 7 * 24 * 60 * 60,
    }
    return mapping.get(tf, 0)


def asset_group(symbol: str) -> str:
    return ASSET_GROUPS.get(symbol, "Other")


def side_icon(side: str) -> str:
    return "🟢" if side == SIDE_LONG else "🔴"


def side_label(side: str) -> str:
    return "COMPRA" if side == SIDE_LONG else "VENTA"


def side_word(side: str) -> str:
    return "long" if side == SIDE_LONG else "short"


def row_side_distance(candidate: Dict[str, Any]) -> float:
    if candidate["side"] == SIDE_LONG:
        barrier = float(candidate.get("swing_high", candidate["entry_price"]))
        return max((barrier - candidate["entry_price"]) / max(candidate["entry_price"], 1e-9) * 100, 0.0)
    barrier = float(candidate.get("swing_low", candidate["entry_price"]))
    return max((candidate["entry_price"] - barrier) / max(candidate["entry_price"], 1e-9) * 100, 0.0)


def build_setup_key(candidate: Dict[str, Any]) -> str:
    return "|".join(
        [
            candidate["symbol"],
            candidate["side"],
            candidate["timeframe"],
            candidate["regime"],
            candidate["rsi_bucket"],
            candidate["fib_zone"],
            candidate["price_bucket"],
        ]
    )


def build_setup_hash(setup_key: str) -> str:
    return hashlib.sha256(setup_key.encode("utf-8")).hexdigest()


# ── Confirmación 1D ───────────────────────────────────────────────────────────
def evaluate_macro_confirmation(
    daily_df: pd.DataFrame,
    symbol: str,
    context: Dict[str, Any],
    side: str = SIDE_LONG,
) -> Optional[Dict[str, Any]]:
    work = add_indicators(daily_df)
    if len(work) < 210:
        print(f"⚠️ {symbol}: histórico diario insuficiente ({len(work)} velas útiles).")
        return None

    last = work.iloc[-1]
    close = float(last["Close"])
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])
    ema200 = float(last["ema200"])
    rsi = float(last["rsi"])
    adx = float(last["adx"])
    plus_di = float(last["plus_di"])
    minus_di = float(last["minus_di"])
    regime = get_regime(last)

    side = side.upper()
    reasons: List[str] = []
    score_adjustment = 0.0
    rank_adjustment = 0.0

    caution_level = str(context.get("caution_level", "NORMAL")).upper()
    note = str(context.get("note", "")).strip()
    hard_block = context_bool(context, "hard_block_long" if side == SIDE_LONG else "hard_block_short", False)
    barrier_near = context_bool(context, "long_resistance_near" if side == SIDE_LONG else "short_support_near", False)
    barrier_label = str(context.get("long_resistance_label" if side == SIDE_LONG else "short_support_label", "")).strip()

    tp1_rr = context_float(context, "tp1_rr", 1.0)
    tp2_rr = context_float(context, "tp2_rr", 1.6)
    max_rr = context_float(context, "max_rr_long" if side == SIDE_LONG else "max_rr_short", tp2_rr)
    move_to_be_rr = context_float(context, "move_to_be_rr", min(tp1_rr, 1.0))
    risk_multiplier = context_float(context, "risk_multiplier", 1.0)

    if side == SIDE_LONG:
        structural_ok = close > ema200 or ema20 > ema50
        direction_ok = plus_di >= minus_di * 0.92
        rsi_ok = rsi >= 42
        if close > ema50:
            reasons.append("1D arriba de EMA50")
            score_adjustment += 0.35
        if ema20 > ema50:
            reasons.append("1D con sesgo alcista")
            score_adjustment += 0.25
        if plus_di > minus_di and adx >= 16:
            reasons.append(f"1D acompaña dirección (ADX {adx:.1f})")
            score_adjustment += 0.30
        elif minus_di > plus_di and adx >= 18:
            reasons.append("1D todavía no acompaña del todo")
            score_adjustment -= 0.25
        if rsi < 40:
            reasons.append(f"RSI diario débil ({rsi:.1f})")
            score_adjustment -= 0.35
    else:
        structural_ok = close < ema200 or ema20 < ema50
        direction_ok = minus_di >= plus_di * 0.92
        rsi_ok = rsi <= 58
        if close < ema50:
            reasons.append("1D debajo de EMA50")
            score_adjustment += 0.35
        if ema20 < ema50:
            reasons.append("1D con sesgo bajista")
            score_adjustment += 0.25
        if minus_di > plus_di and adx >= 16:
            reasons.append(f"1D acompaña dirección (ADX {adx:.1f})")
            score_adjustment += 0.30
        elif plus_di > minus_di and adx >= 18:
            reasons.append("1D todavía no acompaña del todo")
            score_adjustment -= 0.25
        if rsi > 60:
            reasons.append(f"RSI diario demasiado alto para short ({rsi:.1f})")
            score_adjustment -= 0.35

    if caution_level == "HIGH":
        score_adjustment -= 0.25
        rank_adjustment -= 0.80
        reasons.append("Contexto manual en cautela alta")
    elif caution_level == "EXTREME":
        score_adjustment -= 0.60
        rank_adjustment -= 1.60
        reasons.append("Contexto manual en cautela extrema")

    if barrier_near:
        score_adjustment -= 0.20
        rank_adjustment -= 0.70
        if barrier_label:
            reasons.append(f"Nivel macro cercano: {barrier_label}")
        else:
            reasons.append("Nivel macro cercano")

    if note:
        reasons.append(note)

    technical_ok = structural_ok and direction_ok and rsi_ok and not hard_block

    return {
        "ok": technical_ok,
        "side": side,
        "regime": regime,
        "close": round(close, 6),
        "ema20": round(ema20, 6),
        "ema50": round(ema50, 6),
        "ema200": round(ema200, 6),
        "rsi": round(rsi, 2),
        "adx": round(adx, 2),
        "plus_di": round(plus_di, 2),
        "minus_di": round(minus_di, 2),
        "caution_level": caution_level,
        "barrier_near": barrier_near,
        "barrier_label": barrier_label,
        "tp1_rr": round(max(tp1_rr, 0.8), 2),
        "tp2_rr": round(max(tp2_rr, 1.15), 2),
        "max_rr": round(max(max_rr, 1.15), 2),
        "move_to_be_rr": round(max(move_to_be_rr, 0.7), 2),
        "risk_multiplier": round(max(risk_multiplier, 0.25), 2),
        "score_adjustment": round(score_adjustment, 2),
        "rank_adjustment": round(rank_adjustment, 2),
        "reasons": reasons,
    }


# ── Confirmación 4H ───────────────────────────────────────────────────────────
def evaluate_setup_confirmation(
    fourh_df: pd.DataFrame,
    symbol: str,
    cg_id: str,
    side: str = SIDE_LONG,
) -> Optional[Dict[str, Any]]:
    work = add_indicators(fourh_df)
    if len(work) < 210:
        print(f"⚠️ {symbol}: histórico 4h insuficiente ({len(work)} velas útiles).")
        return None

    side = side.upper()
    last = work.iloc[-1]
    prev = work.iloc[-2]
    fib = fibonacci_context(work, FIB_LOOKBACK)
    vwap_data = compute_vwap_proximity(work, lookback=20)
    vol_data = compute_volume_momentum(work, lookback=12)

    close = float(last["Close"])
    atr = float(last["atr"])
    adx = float(last["adx"])
    rsi = float(last["rsi"])
    plus_di = float(last["plus_di"])
    minus_di = float(last["minus_di"])
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])
    ema200 = float(last["ema200"])
    regime = get_regime(last)

    if not all(math.isfinite(x) for x in [close, atr, adx, rsi, plus_di, minus_di, ema20, ema50, ema200]):
        return None

    score = 0.0
    reasons: List[str] = []
    swing = work.tail(SETUP_SWING_LOOKBACK)

    if side == SIDE_LONG:
        trend_ok = close > ema50 and ema20 >= ema50
        direction_ok = plus_di >= minus_di * 0.95
        strength_ok = adx >= 14
        bullish_cross = float(prev["ema20"]) <= float(prev["ema50"]) and ema20 > ema50
        stop_anchor = float(swing["Low"].min())
        stop_loss = min(stop_anchor, ema50) - atr * 0.90
        risk = max(close - stop_loss, atr * 0.45, close * 0.004)
        stop_loss = close - risk
        tp1_rr = 1.0
        rr_ratio = 1.6
        tp1 = close + risk * tp1_rr
        tp2 = close + risk * rr_ratio
        zone_value = fib["retracement"]
        zone = fib_zone(zone_value)
        distance_to_barrier_pct = max((fib["swing_high"] - close) / max(close, 1e-9) * 100, 0.0)
        above_vwap = vwap_data["above_vwap"]

        if trend_ok:
            score += 1.8
            reasons.append("4H alcista sobre EMA50")
        elif close > ema20:
            score += 1.0
            reasons.append("4H sostiene EMA20")
        else:
            reasons.append("4H sin estructura clara")
        if direction_ok:
            score += 1.0
            reasons.append(f"DI acompaña (ADX {adx:.1f})")
        elif adx >= 12:
            score += 0.35
            reasons.append("Dirección aceptable, pero no limpia")
        if 46 <= rsi <= 66:
            score += 1.0
            reasons.append(f"RSI operativo ({rsi:.1f})")
        elif 40 <= rsi < 46:
            score += 0.6
            reasons.append(f"RSI recuperando ({rsi:.1f})")
        elif rsi > 72:
            score -= 0.45
            reasons.append(f"RSI algo caliente ({rsi:.1f})")
        if zone in {"0.382-0.500", "0.500-0.618", "0.236-0.382"}:
            score += 0.85
            reasons.append(f"Zona fib útil ({zone})")
        elif zone == "0.618-0.786":
            score += 0.45
            reasons.append(f"Pullback profundo pero válido ({zone})")
        if bullish_cross:
            score += 0.65
            reasons.append("Cruce EMA20>EMA50 reciente")
        if above_vwap and vwap_data["distance_pct"] <= 2.2:
            score += 0.45
            reasons.append("Precio bien posicionado vs VWAP 4H")
        elif vwap_data["distance_pct"] > 3.4:
            score -= 0.35
            reasons.append("Precio algo extendido sobre VWAP 4H")
        if vol_data["strong_momentum"]:
            score += 0.50
            reasons.append(f"Momentum de volumen ({vol_data['relative_volume']:.2f}x)")
        elif vol_data["divergence_up"]:
            score -= 0.30
            reasons.append("Sube con volumen flojo")
    else:
        trend_ok = close < ema50 and ema20 <= ema50
        direction_ok = minus_di >= plus_di * 0.95
        strength_ok = adx >= 14
        bullish_cross = False
        bearish_cross = float(prev["ema20"]) >= float(prev["ema50"]) and ema20 < ema50
        stop_anchor = float(swing["High"].max())
        stop_loss = max(stop_anchor, ema50) + atr * 0.90
        risk = max(stop_loss - close, atr * 0.45, close * 0.004)
        stop_loss = close + risk
        tp1_rr = 1.0
        rr_ratio = 1.6
        tp1 = close - risk * tp1_rr
        tp2 = close - risk * rr_ratio
        zone_value = fib["pullback_from_low"]
        zone = fib_zone(zone_value)
        distance_to_barrier_pct = max((close - fib["swing_low"]) / max(close, 1e-9) * 100, 0.0)
        above_vwap = vwap_data["above_vwap"]

        if trend_ok:
            score += 1.8
            reasons.append("4H bajista bajo EMA50")
        elif close < ema20:
            score += 1.0
            reasons.append("4H sostiene EMA20 a la baja")
        else:
            reasons.append("4H sin estructura clara")
        if direction_ok:
            score += 1.0
            reasons.append(f"DI acompaña (ADX {adx:.1f})")
        elif adx >= 12:
            score += 0.35
            reasons.append("Dirección aceptable, pero no limpia")
        if 34 <= rsi <= 54:
            score += 1.0
            reasons.append(f"RSI operativo ({rsi:.1f})")
        elif 54 < rsi <= 60:
            score += 0.6
            reasons.append(f"RSI girando desde arriba ({rsi:.1f})")
        elif rsi < 28:
            score -= 0.45
            reasons.append(f"RSI algo extendido ({rsi:.1f})")
        if zone in {"0.382-0.500", "0.500-0.618", "0.236-0.382"}:
            score += 0.85
            reasons.append(f"Zona fib útil ({zone})")
        elif zone == "0.618-0.786":
            score += 0.45
            reasons.append(f"Pullback profundo pero válido ({zone})")
        if bearish_cross:
            score += 0.65
            reasons.append("Cruce EMA20<EMA50 reciente")
        if (not above_vwap) and abs(vwap_data["distance_pct"]) <= 2.2:
            score += 0.45
            reasons.append("Precio bien posicionado vs VWAP 4H")
        elif vwap_data["distance_pct"] < -3.4:
            score -= 0.35
            reasons.append("Precio algo extendido bajo VWAP 4H")
        if vol_data["strong_momentum"]:
            score += 0.50
            reasons.append(f"Momentum de volumen ({vol_data['relative_volume']:.2f}x)")
        elif vol_data["divergence_down"]:
            score -= 0.30
            reasons.append("Cae con volumen flojo")

    if distance_to_barrier_pct <= 0.45:
        score -= 0.70
        reasons.append(f"Poco espacio estructural ({distance_to_barrier_pct:.2f}%)")
    elif distance_to_barrier_pct <= 1.00:
        score -= 0.25
        reasons.append(f"Espacio estructural justo ({distance_to_barrier_pct:.2f}%)")

    risk_pct = (risk / max(close, 1e-9)) * 100
    if risk_pct < 0.35:
        score -= 0.40
        reasons.append("Stop demasiado apretado")
    elif risk_pct > 6.50:
        score -= 0.35
        reasons.append("Stop demasiado ancho")

    setup_score_floor = max(MIN_SCORE - 0.8, 4.6)
    setup_ok = trend_ok and direction_ok and strength_ok and rr_ratio >= 1.15 and score >= setup_score_floor

    payload = {
        "symbol": symbol,
        "cg_id": cg_id,
        "side": side,
        "timeframe": TRADING_TIMEFRAME,
        "regime": regime,
        "rsi_bucket": rsi_bucket(rsi),
        "fib_zone": zone,
        "price_bucket": price_bucket(close, atr),
        "candle_ts": int(pd.Timestamp(last["ts"]).timestamp()),
        "entry_price": float(close),
        "stop_loss": float(stop_loss),
        "take_profit": float(tp2),
        "tp1": float(tp1),
        "tp2": float(tp2),
        "tp1_rr": round(tp1_rr, 2),
        "tp2_rr": round(rr_ratio, 2),
        "rr_ratio": round(rr_ratio, 2),
        "score": round(score, 2),
        "adx": round(adx, 2),
        "rsi": round(rsi, 2),
        "atr": float(atr),
        "reasons": reasons,
        "fib_retracement": round(float(zone_value), 4),
        "swing_low": float(fib["swing_low"]),
        "swing_high": float(fib["swing_high"]),
        "asset_group": asset_group(symbol),
        "vwap": vwap_data["vwap"],
        "vwap_distance_pct": vwap_data["distance_pct"],
        "above_vwap": above_vwap,
        "volume_divergence": vol_data["divergence_up"] if side == SIDE_LONG else vol_data["divergence_down"],
        "volume_strong": vol_data["strong_momentum"],
        "setup_ok": setup_ok,
        "setup_score_floor": round(setup_score_floor, 2),
        "distance_to_barrier_pct": round(distance_to_barrier_pct, 2),
        "bullish_cross": bool(side == SIDE_LONG and bullish_cross),
        "bearish_cross": bool(side == SIDE_SHORT and locals().get("bearish_cross", False)),
    }
    return payload


# ── Confirmación 15m ──────────────────────────────────────────────────────────
def evaluate_timing_confirmation(
    entry_df: pd.DataFrame,
    symbol: str,
    side: str = SIDE_LONG,
) -> Optional[Dict[str, Any]]:
    work = add_indicators(entry_df)
    if len(work) < 60:
        print(f"⚠️ {symbol}: histórico 15m insuficiente ({len(work)} velas útiles).")
        return None

    last = work.iloc[-1]
    prev = work.iloc[-2]
    close = float(last["Close"])
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])
    rsi = float(last["rsi"])
    atr = float(last["atr"])
    vwap_data = compute_vwap_proximity(work, lookback=16)

    reasons: List[str] = []
    score_adjustment = 0.0
    rank_adjustment = 0.0
    points = 0.0
    hard_fail = False

    trend_gap_pct = ((ema20 - ema50) / max(ema50, 1e-9)) * 100
    side = side.upper()

    if side == SIDE_LONG:
        if ema20 >= ema50:
            points += 1.0
            reasons.append("15m acompaña")
        elif close > ema50 and trend_gap_pct >= -0.35:
            points += 0.5
            reasons.append("15m casi alineado")
        else:
            hard_fail = True
            reasons.append("15m todavía en contra")

        if 42 <= rsi <= 67:
            points += 0.8
            reasons.append(f"RSI 15m sano ({rsi:.1f})")
        elif 36 <= rsi < 42:
            points += 0.4
            reasons.append(f"RSI 15m recuperando ({rsi:.1f})")
        elif rsi > 74:
            score_adjustment -= 0.30
            reasons.append(f"RSI 15m caliente ({rsi:.1f})")

        if close > ema20 and float(prev["Close"]) <= float(prev["ema20"]):
            points += 0.7
            reasons.append("Reclaim de EMA20")
        elif close >= float(work.tail(12)["High"].max()) * 0.997:
            points += 0.6
            reasons.append("Cerca de ruptura local")

        if abs(vwap_data["distance_pct"]) <= 1.4:
            points += 0.55
            reasons.append(f"Buena entrada vs VWAP ({vwap_data['distance_pct']:+.1f}%)")
        elif vwap_data["distance_pct"] > 3.8:
            hard_fail = True
            reasons.append(f"Demasiado perseguida sobre VWAP ({vwap_data['distance_pct']:+.1f}%)")
        elif vwap_data["distance_pct"] > 2.4:
            score_adjustment -= 0.20
            rank_adjustment -= 0.30
            reasons.append(f"Entrada algo estirada ({vwap_data['distance_pct']:+.1f}%)")
    else:
        if ema20 <= ema50:
            points += 1.0
            reasons.append("15m acompaña")
        elif close < ema50 and trend_gap_pct <= 0.35:
            points += 0.5
            reasons.append("15m casi alineado")
        else:
            hard_fail = True
            reasons.append("15m todavía en contra")

        if 33 <= rsi <= 58:
            points += 0.8
            reasons.append(f"RSI 15m sano ({rsi:.1f})")
        elif 58 < rsi <= 64:
            points += 0.4
            reasons.append(f"RSI 15m girando ({rsi:.1f})")
        elif rsi < 26:
            score_adjustment -= 0.30
            reasons.append(f"RSI 15m muy extendido ({rsi:.1f})")

        if close < ema20 and float(prev["Close"]) >= float(prev["ema20"]):
            points += 0.7
            reasons.append("Rechazo de EMA20")
        elif close <= float(work.tail(12)["Low"].min()) * 1.003:
            points += 0.6
            reasons.append("Cerca de breakdown local")

        if abs(vwap_data["distance_pct"]) <= 1.4:
            points += 0.55
            reasons.append(f"Buena entrada vs VWAP ({vwap_data['distance_pct']:+.1f}%)")
        elif vwap_data["distance_pct"] < -3.8:
            hard_fail = True
            reasons.append(f"Demasiado perseguida bajo VWAP ({vwap_data['distance_pct']:+.1f}%)")
        elif vwap_data["distance_pct"] < -2.4:
            score_adjustment -= 0.20
            rank_adjustment -= 0.30
            reasons.append(f"Entrada algo estirada ({vwap_data['distance_pct']:+.1f}%)")

    timing_ok = (not hard_fail) and points >= 1.6

    return {
        "ok": timing_ok,
        "side": side,
        "points": round(points, 2),
        "rsi": round(rsi, 2),
        "ema20": round(ema20, 6),
        "ema50": round(ema50, 6),
        "atr": round(atr, 6),
        "vwap": vwap_data["vwap"],
        "vwap_distance_pct": vwap_data["distance_pct"],
        "score_adjustment": round(score_adjustment, 2),
        "rank_adjustment": round(rank_adjustment, 2),
        "reasons": reasons,
    }


# ── Construcción de candidato ─────────────────────────────────────────────────
def apply_context_execution_policy(candidate: Dict[str, Any], macro_eval: Dict[str, Any]) -> Dict[str, Any]:
    side = candidate["side"]
    risk = max(abs(candidate["entry_price"] - candidate["stop_loss"]), 1e-9)

    policy_tp1_rr = float(macro_eval.get("tp1_rr", candidate.get("tp1_rr", 1.0)))
    policy_tp2_rr = float(macro_eval.get("tp2_rr", candidate.get("tp2_rr", 1.6)))
    policy_max_rr = float(macro_eval.get("max_rr", policy_tp2_rr))
    final_tp1_rr = max(min(policy_tp1_rr, policy_tp2_rr - 0.15), 0.8)
    final_tp2_rr = max(min(policy_tp2_rr, policy_max_rr), final_tp1_rr + 0.15)

    if side == SIDE_LONG:
        candidate["tp1"] = float(candidate["entry_price"] + risk * final_tp1_rr)
        candidate["tp2"] = float(candidate["entry_price"] + risk * final_tp2_rr)
    else:
        candidate["tp1"] = float(candidate["entry_price"] - risk * final_tp1_rr)
        candidate["tp2"] = float(candidate["entry_price"] - risk * final_tp2_rr)

    candidate["tp1_rr"] = round(final_tp1_rr, 2)
    candidate["tp2_rr"] = round(final_tp2_rr, 2)
    candidate["take_profit"] = candidate["tp2"]
    candidate["rr_ratio"] = round(final_tp2_rr, 2)
    candidate["move_to_be_rr"] = round(float(macro_eval.get("move_to_be_rr", min(final_tp1_rr, 1.0))), 2)
    candidate["breakeven_trigger"] = float(candidate["entry_price"] + risk * candidate["move_to_be_rr"]) if side == SIDE_LONG else float(candidate["entry_price"] - risk * candidate["move_to_be_rr"])
    candidate["risk_multiplier"] = round(float(macro_eval.get("risk_multiplier", 1.0)), 2)

    # Política relajada: el contexto manual solo penaliza si el espacio macro es microscópico.
    barrier_distance_pct = row_side_distance(candidate)
    if barrier_distance_pct <= 0.30:
        candidate["score"] = round(candidate["score"] - 0.45, 2)
        candidate["reasons"].append(f"Espacio macro mínimo ({barrier_distance_pct:.2f}%)")

    return candidate


def compute_required_min_rr(candidate: Dict[str, Any], macro_eval: Dict[str, Any]) -> float:
    base = MIN_RR
    caution = str(macro_eval.get("caution_level", "NORMAL")).upper()
    if caution == "EXTREME":
        base = max(base, 1.45)
    return round(base, 2)


def compute_tactical_eligibility(
    candidate: Dict[str, Any],
    macro_eval: Dict[str, Any],
    timing_eval: Dict[str, Any],
) -> Tuple[bool, str]:
    confirmations = int(candidate.get("macro_ok", False)) + int(candidate.get("setup_ok", False)) + int(candidate.get("timing_ok", False))
    tactical = (
        candidate.get("setup_ok", False)
        and confirmations >= 2
        and float(candidate.get("score", 0.0)) >= TACTICAL_MIN_SCORE
        and float(candidate.get("rr_ratio", 0.0)) >= compute_required_min_rr(candidate, macro_eval)
    )
    reason = "two_of_three" if tactical else ""
    return tactical, reason


def build_candidate(
    symbol: str,
    cg_id: str,
    macro_eval: Dict[str, Any],
    setup_eval: Dict[str, Any],
    timing_eval: Dict[str, Any],
) -> Dict[str, Any]:
    candidate = dict(setup_eval)
    score_adjustment = float(macro_eval.get("score_adjustment", 0.0)) + float(timing_eval.get("score_adjustment", 0.0))
    rank_adjustment = float(macro_eval.get("rank_adjustment", 0.0)) + float(timing_eval.get("rank_adjustment", 0.0))

    candidate["score"] = round(float(candidate["score"]) + score_adjustment, 2)
    candidate["rank_adjustment"] = round(rank_adjustment, 2)
    candidate["macro_ok"] = bool(macro_eval.get("ok", False))
    candidate["timing_ok"] = bool(timing_eval.get("ok", False))
    candidate["setup_ok"] = bool(setup_eval.get("setup_ok", False))
    candidate["macro"] = macro_eval
    candidate["timing"] = timing_eval

    reasons = list(setup_eval.get("reasons", []))
    reasons.extend([f"1D: {text}" for text in macro_eval.get("reasons", [])])
    reasons.extend([f"15m: {text}" for text in timing_eval.get("reasons", [])])
    candidate["reasons"] = reasons

    candidate = apply_context_execution_policy(candidate, macro_eval)
    candidate["confirmations_passed"] = int(candidate["macro_ok"]) + int(candidate["setup_ok"]) + int(candidate["timing_ok"])
    candidate["required_min_rr"] = compute_required_min_rr(candidate, macro_eval)

    full_alert = (
        candidate["setup_ok"]
        and candidate["score"] >= MIN_SCORE
        and candidate["rr_ratio"] >= candidate["required_min_rr"]
        and candidate["confirmations_passed"] >= 2
    )
    tactical_alert, tactical_reason = compute_tactical_eligibility(candidate, macro_eval, timing_eval)

    candidate["full_alert"] = full_alert
    candidate["tactical_alert"] = tactical_alert and not full_alert
    if full_alert:
        candidate["alert"] = True
        candidate["alert_profile"] = "FULL" if candidate["confirmations_passed"] == 3 else "TACTICAL"
        candidate["profile_reason"] = "3_of_3" if candidate["confirmations_passed"] == 3 else "2_of_3"
    elif tactical_alert:
        candidate["alert"] = True
        candidate["alert_profile"] = "TACTICAL"
        candidate["profile_reason"] = tactical_reason
    else:
        candidate["alert"] = False
        candidate["alert_profile"] = "NONE"
        candidate["profile_reason"] = ""

    candidate["setup_key"] = build_setup_key(candidate)
    candidate["setup_hash"] = build_setup_hash(candidate["setup_key"])
    return candidate


# ── Execution gate ────────────────────────────────────────────────────────────
def execution_metrics_for_candidate(candidate: Dict[str, Any], current_price: Optional[float]) -> Dict[str, Any]:
    side = candidate["side"]
    signal_price = float(candidate["entry_price"])
    current = float(current_price if current_price is not None else signal_price)
    stop = float(candidate["stop_loss"])
    tp1 = float(candidate.get("tp1", candidate.get("take_profit", signal_price)))
    tp2 = float(candidate.get("tp2", candidate.get("take_profit", tp1)))

    if side == SIDE_LONG:
        current_risk = current - stop
        favorable_move = current - signal_price
        original_tp1_distance = max(tp1 - signal_price, 1e-9)
        valid_side = current > stop
        tp1_remaining = tp1 - current
        tp2_remaining = tp2 - current
    else:
        current_risk = stop - current
        favorable_move = signal_price - current
        original_tp1_distance = max(signal_price - tp1, 1e-9)
        valid_side = current < stop
        tp1_remaining = current - tp1
        tp2_remaining = current - tp2

    progress_to_tp1 = favorable_move / original_tp1_distance
    current_rr_tp1 = tp1_remaining / max(current_risk, 1e-9) if valid_side else -99.0
    current_rr_tp2 = tp2_remaining / max(current_risk, 1e-9) if valid_side else -99.0

    return {
        "signal_price": round(signal_price, 8),
        "current_price": round(current, 8),
        "original_rr": round(float(candidate.get("rr_ratio", 0.0)), 2),
        "current_rr_tp1": round(current_rr_tp1, 2),
        "current_rr_tp2": round(current_rr_tp2, 2),
        "progress_to_tp1": round(progress_to_tp1, 4),
        "progress_to_tp1_pct": round(progress_to_tp1 * 100, 1),
        "current_risk": round(current_risk, 8),
        "valid_side": bool(valid_side),
    }


def apply_execution_quality_gate(candidate: Dict[str, Any], current_price: Optional[float]) -> Dict[str, Any]:
    metrics = execution_metrics_for_candidate(candidate, current_price)
    candidate["execution"] = metrics
    candidate["execution_state"] = "NOT_CHECKED"
    candidate["execution_decision"] = "Gate desactivado"

    if not ENABLE_EXECUTION_QUALITY_GATE:
        return candidate

    if not metrics["valid_side"]:
        candidate["alert"] = False
        candidate["execution_state"] = "INVALID_NOW"
        candidate["execution_decision"] = "El precio actual ya invalida el setup."
        return candidate

    if metrics["progress_to_tp1"] >= EXECUTION_BLOCK_PROGRESS_TO_TP1 or metrics["current_rr_tp1"] < EXECUTION_MIN_CURRENT_RR_TP1:
        candidate["alert"] = False
        candidate["execution_state"] = "LATE"
        candidate["execution_decision"] = "La entrada ya corrió demasiado."
        return candidate

    if metrics["progress_to_tp1"] >= EXECUTION_CAUTION_PROGRESS_TO_TP1 or metrics["current_rr_tp1"] < EXECUTION_CAUTION_CURRENT_RR_TP1:
        candidate["score"] = round(candidate["score"] - 0.35, 2)
        candidate["execution_state"] = "CAUTION"
        candidate["execution_decision"] = "Sigue ejecutable, pero ya no tan limpia."
        if candidate["score"] < MIN_SCORE:
            candidate["alert"] = False
        return candidate

    candidate["execution_state"] = "OK"
    candidate["execution_decision"] = "Entrada todavía utilizable."
    return candidate


# ── Ranking / diversificación ────────────────────────────────────────────────
def compute_rank_score(candidate: Dict[str, Any]) -> Tuple[float, List[str]]:
    notes: List[str] = []
    rank = float(candidate["score"]) * 2.4
    notes.append(f"score {candidate['score']:.2f}")

    rr_component = min(float(candidate["rr_ratio"]), 2.2) * 1.8
    rank += rr_component
    notes.append(f"rr {candidate['rr_ratio']:.2f}")

    if float(candidate["adx"]) >= 22:
        rank += 1.2
        notes.append("adx fuerte")
    elif float(candidate["adx"]) >= 16:
        rank += 0.5
        notes.append("adx usable")

    if candidate.get("volume_strong"):
        rank += 0.7
        notes.append("volumen")

    if candidate.get("macro_ok"):
        rank += 0.8
        notes.append("macro")
    if candidate.get("timing_ok"):
        rank += 0.8
        notes.append("timing")

    if candidate.get("bullish_cross") or candidate.get("bearish_cross"):
        rank += 0.7
        notes.append("cruce")

    if candidate["symbol"] in {"BTC", "ETH"}:
        rank += 0.5
        notes.append("major")

    if candidate.get("alert_profile") == "TACTICAL":
        rank -= 0.8
        notes.append("2/3")

    rank += float(candidate.get("rank_adjustment", 0.0))
    if candidate.get("rank_adjustment", 0.0):
        notes.append(f"ajuste {candidate['rank_adjustment']:+.2f}")

    return round(rank, 2), notes


def rank_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ranked: List[Dict[str, Any]] = []
    for candidate in candidates:
        clone = dict(candidate)
        clone["rank_score"], clone["rank_notes"] = compute_rank_score(clone)
        ranked.append(clone)
    ranked.sort(key=lambda item: (item["rank_score"], item["score"], item["adx"], item["rr_ratio"]), reverse=True)
    return ranked


def select_ranked_candidates(ranked: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not ENABLE_RANKING:
        return ranked, []

    selected: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    group_counts: Dict[str, int] = {}

    for candidate in ranked:
        group_name = candidate["asset_group"]
        if len(selected) >= MAX_ALERTS_PER_RUN:
            deferred.append(candidate)
            continue
        if group_counts.get(group_name, 0) >= MAX_ALERTS_PER_GROUP:
            deferred.append(candidate)
            continue
        selected.append(candidate)
        group_counts[group_name] = group_counts.get(group_name, 0) + 1
    return selected, deferred


def build_human_signal_summary(candidate: Dict[str, Any]) -> Dict[str, str]:
    side = candidate["side"]
    exec_state = candidate.get("execution_state", "OK")
    if candidate.get("alert") and exec_state == "CAUTION":
        return {
            "label": f"{side_label(side)} CON CAUTELA",
            "reading": "La señal existe, pero el precio ya avanzó algo.",
            "main_risk": str(candidate.get("execution_decision", "")),
            "recommendation": "No perseguir si sigue alejándose.",
        }
    if candidate.get("alert"):
        return {
            "label": side_label(side),
            "reading": "4H tiene estructura operable y al menos una capa adicional acompaña.",
            "main_risk": "El trade pierde calidad si se extiende demasiado antes de ejecutar.",
            "recommendation": "Usar el stop estructural y no ampliar riesgo.",
        }
    return {
        "label": "SIN SEÑAL",
        "reading": "La estructura base 4H todavía no es lo bastante limpia.",
        "main_risk": "Entrada prematura o contexto sin suficiente alineación.",
        "recommendation": "Esperar mejor timing o confirmación direccional.",
    }


# ── Dedupe / cooldown ─────────────────────────────────────────────────────────
def last_symbol_alert_ts(conn: sqlite3.Connection, symbol: str) -> int:
    row = conn.execute(
        """
        SELECT MAX(sent_at) AS sent_at
        FROM (
            SELECT sent_at FROM alerts WHERE symbol = ?
            UNION ALL
            SELECT sent_at FROM legacy_symbol_cooldowns WHERE symbol = ?
        )
        """,
        (symbol, symbol),
    ).fetchone()
    if not row:
        return 0
    value = row["sent_at"]
    return int(value or 0)


def should_send_alert(conn: sqlite3.Connection, candidate: Dict[str, Any]) -> Tuple[bool, Optional[int], str]:
    now = int(time.time())
    cooldown_seconds = COOLDOWN_HOURS * 3600

    last_sent = last_symbol_alert_ts(conn, candidate["symbol"])
    if last_sent and (now - last_sent) < cooldown_seconds:
        remaining_h = (cooldown_seconds - (now - last_sent)) / 3600
        return False, None, f"cooldown activo ({remaining_h:.1f}h restantes)"

    active_similar = conn.execute(
        """
        SELECT *
        FROM alerts
        WHERE symbol = ? AND side = ? AND status = ? AND validation_status = ?
        ORDER BY sent_at DESC
        LIMIT 1
        """,
        (candidate["symbol"], candidate["side"], ACTIVE, VALIDATION_PENDING),
    ).fetchone()

    if active_similar:
        old_score = float(active_similar["score"])
        old_rr = float(active_similar["rr_ratio"])
        improved = (
            candidate["score"] >= old_score + 0.45
            or candidate["rr_ratio"] >= old_rr + 0.15
            or candidate["setup_hash"] != active_similar["setup_hash"]
        )
        if not improved:
            return False, None, "setup muy parecido y sin mejora material"
        return True, int(active_similar["id"]), "setup mejorado"

    exact = conn.execute(
        """
        SELECT id
        FROM alerts
        WHERE setup_hash = ? AND side = ? AND symbol = ? AND sent_at >= ?
        LIMIT 1
        """,
        (candidate["setup_hash"], candidate["side"], candidate["symbol"], now - cooldown_seconds),
    ).fetchone()
    if exact:
        return False, None, "setup exacto recientemente enviado"

    return True, None, "nuevo setup"


def invalidate_old_alerts(conn: sqlite3.Connection, candidate: Dict[str, Any]) -> None:
    conn.execute(
        f"""
        UPDATE alerts
        SET status = '{INVALIDATED}',
            invalidated_at = ?,
            invalidation_reason = COALESCE(invalidation_reason, 'Reemplazada por una entrada más fresca')
        WHERE symbol = ?
          AND side = ?
          AND status = '{ACTIVE}'
          AND validation_status = '{VALIDATION_PENDING}'
          AND candle_ts < ?
        """,
        (int(time.time()), candidate["symbol"], candidate["side"], candidate["candle_ts"]),
    )
    conn.commit()


def save_alert(conn: sqlite3.Connection, candidate: Dict[str, Any], improved_from_alert_id: Optional[int]) -> None:
    now = int(time.time())
    expiry_ts = int(candidate["candle_ts"]) + ALERT_FORWARD_BARS * timeframe_to_seconds(TRADING_TIMEFRAME)
    conn.execute(
        """
        INSERT INTO alerts (
            symbol, cg_id, side, timeframe, setup_key, setup_hash, regime, rsi_bucket,
            fib_zone, price_bucket, candle_ts, entry_price, stop_loss, take_profit,
            rr_ratio, score, adx, rsi, atr, reasons_json, status, sent_at,
            improved_from_alert_id, tp1, tp2, tp1_rr, tp2_rr, move_to_be_rr,
            breakeven_trigger, risk_multiplier, expiry_ts, validation_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate["symbol"],
            candidate["cg_id"],
            candidate["side"],
            candidate["timeframe"],
            candidate["setup_key"],
            candidate["setup_hash"],
            candidate["regime"],
            candidate["rsi_bucket"],
            candidate["fib_zone"],
            candidate["price_bucket"],
            int(candidate["candle_ts"]),
            float(candidate["entry_price"]),
            float(candidate["stop_loss"]),
            float(candidate["take_profit"]),
            float(candidate["rr_ratio"]),
            float(candidate["score"]),
            float(candidate["adx"]),
            float(candidate["rsi"]),
            float(candidate["atr"]),
            json.dumps(candidate["reasons"], ensure_ascii=False),
            ACTIVE,
            now,
            improved_from_alert_id,
            float(candidate["tp1"]),
            float(candidate["tp2"]),
            float(candidate["tp1_rr"]),
            float(candidate["tp2_rr"]),
            float(candidate["move_to_be_rr"]),
            float(candidate["breakeven_trigger"]),
            float(candidate.get("risk_multiplier", 1.0)),
            expiry_ts,
            VALIDATION_PENDING,
        ),
    )
    conn.commit()


# ── Validación histórica ──────────────────────────────────────────────────────
def simulate_outcome(
    candles: pd.DataFrame,
    entry_price: float,
    stop_loss: float,
    tp1: float,
    tp2: float,
    side: str,
) -> Dict[str, Any]:
    if candles is None or candles.empty:
        return {"result": "NO_DATA"}

    if side == SIDE_LONG:
        risk = entry_price - stop_loss
    else:
        risk = stop_loss - entry_price
    if risk <= 0:
        return {"result": "INVALID_RISK"}

    tp1_hit = False
    for idx, row in candles.reset_index(drop=True).iterrows():
        low = float(row["Low"])
        high = float(row["High"])
        close = float(row["Close"])
        bar = idx + 1

        if side == SIDE_LONG:
            if low <= stop_loss:
                return {"result": "SL", "price": stop_loss, "rr": -1.0, "bars": bar, "tp1_hit": int(tp1_hit), "tp2_hit": 0, "close_at_expiry": close}
            if high >= tp2:
                return {"result": "TP2", "price": tp2, "rr": round((tp2 - entry_price) / risk, 4), "bars": bar, "tp1_hit": 1, "tp2_hit": 1, "close_at_expiry": close}
            if high >= tp1:
                tp1_hit = True
        else:
            if high >= stop_loss:
                return {"result": "SL", "price": stop_loss, "rr": -1.0, "bars": bar, "tp1_hit": int(tp1_hit), "tp2_hit": 0, "close_at_expiry": close}
            if low <= tp2:
                return {"result": "TP2", "price": tp2, "rr": round((entry_price - tp2) / risk, 4), "bars": bar, "tp1_hit": 1, "tp2_hit": 1, "close_at_expiry": close}
            if low <= tp1:
                tp1_hit = True

    close = float(candles.iloc[-1]["Close"])
    rr = ((close - entry_price) / risk) if side == SIDE_LONG else ((entry_price - close) / risk)
    return {
        "result": "EXPIRED",
        "price": close,
        "rr": round(rr, 4),
        "bars": int(len(candles)),
        "tp1_hit": int(tp1_hit),
        "tp2_hit": 0,
        "close_at_expiry": close,
    }


def validate_open_alerts(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    now = int(time.time())
    pending = conn.execute(
        """
        SELECT *
        FROM alerts
        WHERE status = ? AND validation_status = ? AND expiry_ts IS NOT NULL AND expiry_ts <= ?
        ORDER BY sent_at ASC
        """,
        (ACTIVE, VALIDATION_PENDING, now),
    ).fetchall()

    resolved: List[Dict[str, Any]] = []
    if not pending:
        return resolved

    tf_seconds = timeframe_to_seconds(TRADING_TIMEFRAME)

    for row in pending:
        start_ts = int(row["candle_ts"]) + tf_seconds
        end_ts = int(row["expiry_ts"]) + tf_seconds
        candles = data_source.fetch_klines_range(row["symbol"], TRADING_TIMEFRAME, start_ts, end_ts)
        outcome = simulate_outcome(
            candles,
            float(row["entry_price"]),
            float(row["stop_loss"]),
            float(row["tp1"] or row["take_profit"]),
            float(row["tp2"] or row["take_profit"]),
            str(row["side"]),
        )

        if outcome["result"] == "NO_DATA":
            continue

        status = CLOSED if outcome["result"] == "TP2" else EXPIRED if outcome["result"] == "EXPIRED" else CLOSED
        note_map = {"TP2": "TP2 alcanzado", "SL": "Stop alcanzado", "EXPIRED": "Ventana expirada"}
        conn.execute(
            f"""
            UPDATE alerts
            SET status = ?,
                validation_status = '{VALIDATION_RESOLVED}',
                validation_result = ?,
                validated_at = ?,
                outcome_price = ?,
                outcome_rr = ?,
                bars_to_outcome = ?,
                tp1_hit = ?,
                tp2_hit = ?,
                close_at_expiry = ?,
                outcome_note = ?,
                invalidated_at = CASE WHEN ? = '{EXPIRED}' THEN ? ELSE invalidated_at END
            WHERE id = ?
            """,
            (
                status,
                outcome["result"],
                now,
                float(outcome.get("price", row["entry_price"])),
                float(outcome.get("rr", 0.0)),
                int(outcome.get("bars", 0)),
                int(outcome.get("tp1_hit", 0)),
                int(outcome.get("tp2_hit", 0)),
                float(outcome.get("close_at_expiry", row["entry_price"])),
                note_map.get(outcome["result"], outcome["result"]),
                status,
                now,
                int(row["id"]),
            ),
        )
        resolved.append(
            {
                "id": int(row["id"]),
                "symbol": str(row["symbol"]),
                "side": str(row["side"]),
                "result": str(outcome["result"]),
                "rr": float(outcome.get("rr", 0.0)),
                "note": note_map.get(outcome["result"], outcome["result"]),
            }
        )
    conn.commit()
    return resolved


def maybe_notify_validated_alerts(conn: sqlite3.Connection) -> int:
    if not SEND_OUTCOME_UPDATES:
        return 0

    rows = conn.execute(
        """
        SELECT *
        FROM alerts
        WHERE validation_status = ? AND validated_at IS NOT NULL AND validation_notified_at IS NULL
        ORDER BY validated_at ASC
        LIMIT 10
        """,
        (VALIDATION_RESOLVED,),
    ).fetchall()
    sent = 0
    for row in rows:
        rr = float(row["outcome_rr"] or 0.0)
        message = (
            f"🧾 <b>Outcome {html.escape(str(row['symbol']))} {html.escape(str(row['side']))}</b>\n"
            f"Resultado: <b>{html.escape(str(row['validation_result']))}</b>\n"
            f"R final: <b>{rr:+.2f}</b>\n"
            f"Nota: {html.escape(str(row['outcome_note'] or ''))}"
        )
        if send_telegram(message):
            conn.execute("UPDATE alerts SET validation_notified_at = ? WHERE id = ?", (int(time.time()), int(row["id"])))
            conn.commit()
            sent += 1
    return sent


# ── Formato de mensajes ───────────────────────────────────────────────────────
def esc(text: Any) -> str:
    return html.escape(str(text))


def format_message(candidate: Dict[str, Any], decision_reason: str) -> str:
    exec_state = str(candidate.get("execution_state", "OK"))
    human = candidate.get("human_summary") or build_human_signal_summary(candidate)
    rr = float(candidate["rr_ratio"])
    risk_pct = abs(candidate["entry_price"] - candidate["stop_loss"]) / max(candidate["entry_price"], 1e-9) * 100
    execution_line = ""
    if exec_state == "CAUTION":
        execution_line = f"\n⚠️ <b>Ejecución:</b> {esc(candidate.get('execution_decision', ''))}"

    reasons = "\n".join(f"• {esc(text)}" for text in candidate["reasons"][:10])

    return (
        f"{side_icon(candidate['side'])} <b>{esc(side_label(candidate['side']))} {esc(candidate['symbol'])}</b>\n\n"
        f"🧠 <b>Lectura:</b> {esc(human['reading'])}\n"
        f"🏷️ <b>Perfil:</b> {esc(candidate.get('alert_profile', 'FULL'))} | "
        f"<b>Motivo:</b> {esc(decision_reason)}\n"
        f"📊 <b>Score:</b> {candidate['score']:.2f} | <b>R:R:</b> {rr:.2f} | "
        f"<b>ADX:</b> {candidate['adx']:.1f} | <b>RSI:</b> {candidate['rsi']:.1f}\n"
        f"🧭 <b>Confirmaciones:</b> 1D {esc('OK' if candidate['macro_ok'] else 'NO')} / "
        f"4H {esc('OK' if candidate['setup_ok'] else 'NO')} / "
        f"15m {esc('OK' if candidate['timing_ok'] else 'NO')}\n"
        f"💰 <b>Entrada:</b> {candidate['entry_price']:.6f}\n"
        f"🎯 <b>TP1:</b> {candidate['tp1']:.6f} | <b>TP2:</b> {candidate['tp2']:.6f}\n"
        f"🛑 <b>SL:</b> {candidate['stop_loss']:.6f} | <b>Riesgo:</b> {risk_pct:.2f}%\n"
        f"📦 <b>Tamaño ref. ${DEFAULT_TRADE_USD:.0f}:</b> {esc(estimate_qty(candidate['entry_price']))} {esc(candidate['symbol'])}\n"
        f"📝 <b>Confluencias:</b>\n{reasons}"
        f"{execution_line}\n\n"
        f"⚠️ <b>Riesgo principal:</b> {esc(human['main_risk'])}\n"
        f"➡️ <b>Disciplina:</b> {esc(human['recommendation'])}"
    )


def format_run_summary(
    selected: List[Dict[str, Any]],
    deferred: List[Dict[str, Any]],
    blocked: List[str],
    total_ready: int,
    watch: List[Dict[str, Any]],
    resolved_alerts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    lines = ["📋 <b>Resumen Crypto Sentinel</b>", ""]
    lines.append(f"✅ Enviadas: <b>{len(selected)}</b>")
    lines.append(f"🟡 Listas pero diferidas: <b>{len(deferred)}</b>")
    lines.append(f"🔎 Setups listos antes del ranking: <b>{total_ready}</b>")

    if resolved_alerts:
        lines.append(f"🧾 Resueltas en esta corrida: <b>{len(resolved_alerts)}</b>")

    if selected:
        lines.append("")
        lines.append("<b>Top enviadas:</b>")
        for item in selected[:6]:
            lines.append(f"• {esc(item['symbol'])} {esc(item['side'])} | score {item['score']:.2f} | rr {item['rr_ratio']:.2f}")

    if watch:
        lines.append("")
        lines.append("<b>Vigilancia:</b>")
        for item in watch[:5]:
            human = item.get("human_summary") or build_human_signal_summary(item)
            lines.append(f"• {esc(item['symbol'])} {esc(item['side'])}: {esc(human['label'])}")

    if deferred:
        lines.append("")
        lines.append("<b>Diferidas por ranking:</b>")
        for item in deferred[:5]:
            lines.append(f"• {esc(item['symbol'])} {esc(item['side'])} | prioridad {item['rank_score']:.2f}")

    if blocked:
        lines.append("")
        lines.append("<b>Descartadas relevantes:</b>")
        for text in blocked[:10]:
            lines.append(f"• {esc(text)}")

    return "\n".join(lines)


def sort_watch_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    unique: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in candidates:
        key = (item["symbol"], item["side"])
        prev = unique.get(key)
        if prev is None or float(item.get("score", 0.0)) > float(prev.get("score", 0.0)):
            unique[key] = item
    return sorted(unique.values(), key=lambda item: (item.get("score", 0.0), item.get("adx", 0.0)), reverse=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    start = time.time()
    conn = get_db_connection(DB_FILE)
    init_db(conn)
    import_legacy_state_if_needed(conn)

    resolved_alerts = validate_open_alerts(conn)
    if resolved_alerts:
        print(f"🧾 Alerts validadas en esta corrida: {len(resolved_alerts)}")
    notified_alerts = maybe_notify_validated_alerts(conn)
    if notified_alerts:
        print(f"📣 Outcomes notificados: {notified_alerts}")

    market_context = load_market_context(MARKET_CONTEXT_FILE)
    btc_dominance = fetch_btc_dominance()
    if btc_dominance is not None:
        print(f"📊 BTC Dominance: {btc_dominance:.1f}%")
    else:
        print("⚠️ BTC Dominance no disponible.")

    print(f"🚀 Iniciando escaneo de {len(CRYPTO_IDS)} activos...")
    sent_count = 0
    total_ready = 0
    ready_candidates: List[Dict[str, Any]] = []
    watch_candidates: List[Dict[str, Any]] = []
    blocked_messages: List[str] = []

    for cg_id, symbol in CRYPTO_IDS.items():
        daily_df = fetch_ohlc_for_symbol(symbol, MACRO_TIMEFRAME, 260)
        fourh_df = fetch_ohlc_for_symbol(symbol, TRADING_TIMEFRAME, 260)
        entry_df = fetch_ohlc_for_symbol(symbol, ENTRY_TIMEFRAME, 200)
        current_price = data_source.fetch_latest_price(symbol)

        if daily_df is None or fourh_df is None or entry_df is None:
            blocked_messages.append(f"{symbol}: datos insuficientes para 1D/4H/15m")
            time.sleep(SLEEP_BETWEEN_ASSETS)
            continue

        if current_price is None and not entry_df.empty:
            current_price = float(entry_df.iloc[-1]["Close"])

        normalized_context = normalize_context(market_context, symbol)
        if btc_dominance is not None:
            normalized_context["btc_dominance"] = btc_dominance

        allowed_sides = parse_allowed_sides(normalized_context)
        for side in allowed_sides:
            macro_eval = evaluate_macro_confirmation(daily_df, symbol, normalized_context, side=side)
            setup_eval = evaluate_setup_confirmation(fourh_df, symbol, cg_id, side=side)
            timing_eval = evaluate_timing_confirmation(entry_df, symbol, side=side)

            if not macro_eval or not setup_eval or not timing_eval:
                blocked_messages.append(f"{symbol} {side}: no se pudo evaluar alguna confirmación")
                continue

            candidate = build_candidate(symbol, cg_id, macro_eval, setup_eval, timing_eval)
            candidate = apply_execution_quality_gate(candidate, current_price)
            candidate["human_summary"] = build_human_signal_summary(candidate)

            if not candidate["alert"]:
                reason = (
                    f"{symbol} {side}: conf {candidate['confirmations_passed']}/3 | "
                    f"score {candidate['score']:.2f} | rr {candidate['rr_ratio']:.2f}"
                )
                if candidate.get("execution_state") in {"INVALID_NOW", "LATE"}:
                    reason += f" | {candidate.get('execution_decision')}"
                blocked_messages.append(reason)
                if candidate.get("setup_ok") or candidate.get("macro_ok") or candidate.get("timing_ok"):
                    watch_candidates.append(candidate)
                continue

            total_ready += 1
            should_send, improved_from_alert_id, decision_reason = should_send_alert(conn, candidate)
            if should_send:
                candidate["improved_from_alert_id"] = improved_from_alert_id
                candidate["decision_reason"] = decision_reason
                ready_candidates.append(candidate)
            else:
                blocked_messages.append(f"{symbol} {side}: {decision_reason}")

        time.sleep(SLEEP_BETWEEN_ASSETS)

    ranked_candidates = rank_candidates(ready_candidates) if ready_candidates else []
    sorted_watch_candidates = sort_watch_candidates(watch_candidates) if watch_candidates else []

    if ranked_candidates:
        print("🏅 Ranking interno:")
        for idx, item in enumerate(ranked_candidates, start=1):
            print(
                f"   {idx}. {item['symbol']} {item['side']} | prioridad={item['rank_score']:.2f} | "
                f"perfil={item.get('alert_profile', 'FULL')} | score={item['score']:.2f}"
            )

    selected_candidates, deferred_candidates = select_ranked_candidates(ranked_candidates)

    for candidate in selected_candidates:
        invalidate_old_alerts(conn, candidate)
        keyboard = build_inline_keyboard(candidate)
        sent_ok = send_telegram(format_message(candidate, candidate["decision_reason"]), reply_markup=keyboard)
        if sent_ok:
            save_alert(conn, candidate, candidate.get("improved_from_alert_id"))
            sent_count += 1
        else:
            print(f"⚠️ {candidate['symbol']} {candidate['side']}: Telegram no confirmó el envío.")

    if SEND_RUN_SUMMARY and (selected_candidates or resolved_alerts):
        send_telegram(
            format_run_summary(
                selected_candidates,
                deferred_candidates,
                blocked_messages,
                total_ready,
                sorted_watch_candidates,
                resolved_alerts=resolved_alerts,
            )
        )

    duration = round(time.time() - start, 1)
    print(f"🏁 Fin del escaneo. Alertas enviadas: {sent_count}. Duración: {duration}s")
    conn.close()


if __name__ == "__main__":
    main()
