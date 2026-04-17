import hashlib
import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

# ── Configuración ──────────────────────────────────────────────────────────────
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CG_API_KEY = os.getenv("COINGECKO_API_KEY", "")

DB_FILE = os.getenv("ALERT_DB_FILE", "alerts_state.db")
LEGACY_STATE_FILE = os.getenv("LEGACY_STATE_FILE", "alert_state.json")
VS_CURRENCY = os.getenv("VS_CURRENCY", "usd")
OHLC_DAYS = int(os.getenv("OHLC_DAYS", "90"))
COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "24"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "5.5"))
MIN_RR = float(os.getenv("MIN_RR", "2.0"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
SLEEP_BETWEEN_ASSETS = float(os.getenv("SLEEP_BETWEEN_ASSETS", "1.25"))
FIB_LOOKBACK = int(os.getenv("FIB_LOOKBACK", "34"))

SIDE_LONG = "LONG"
ACTIVE = "ACTIVE"
INVALIDATED = "INVALIDATED"
EXPIRED = "EXPIRED"


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram no configurado. Se omite el envío.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"⚠️ Telegram respondió {response.status_code}: {response.text[:250]}")
    except Exception as exc:
        print(f"❌ Error enviando a Telegram: {exc}")


# ── Persistencia ───────────────────────────────────────────────────────────────
def get_db_connection(db_file: str = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
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
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
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
        imported = 0
        for symbol, raw_ts in legacy_data.items():
            if not isinstance(symbol, str):
                continue
            if not isinstance(raw_ts, (int, float)):
                continue
            ts = int(raw_ts)
            conn.execute(
                "INSERT INTO legacy_symbol_cooldowns(symbol, sent_at) VALUES(?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET sent_at = excluded.sent_at",
                (symbol, ts),
            )
            imported += 1
        conn.commit()
        print(f"📦 Migración legacy completada: {imported} cooldowns importados.")
    except Exception as exc:
        print(f"⚠️ No se pudo migrar {LEGACY_STATE_FILE}: {exc}")
    finally:
        set_meta(conn, "legacy_import_done", "1")


# ── Datos de mercado ───────────────────────────────────────────────────────────
def get_data(cg_id: str) -> Optional[pd.DataFrame]:
    url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc"
    headers = {"accept": "application/json"}
    if CG_API_KEY:
        headers["x-cg-demo-api-key"] = CG_API_KEY

    params = {"vs_currency": VS_CURRENCY, "days": str(OHLC_DAYS)}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"⚠️ {cg_id}: CoinGecko respondió {response.status_code}")
            return None

        payload = response.json()
        if not payload or len(payload) < 220:
            print(f"⚠️ {cg_id}: datos insuficientes ({len(payload) if payload else 0} velas).")
            return None

        df = pd.DataFrame(payload, columns=["ts", "Open", "High", "Low", "Close"])
        df[["Open", "High", "Low", "Close"]] = df[["Open", "High", "Low", "Close"]].astype(float)
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.sort_values("ts").drop_duplicates(subset=["ts"]).reset_index(drop=True)
        return df
    except Exception as exc:
        print(f"❌ Error obteniendo datos para {cg_id}: {exc}")
        return None


# ── Indicadores ────────────────────────────────────────────────────────────────
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

    plus_dm = pd.Series(
        [up if (up > down and up > 0) else 0.0 for up, down in zip(up_move.fillna(0), down_move.fillna(0))],
        index=df.index,
    )
    minus_dm = pd.Series(
        [down if (down > up and down > 0) else 0.0 for up, down in zip(up_move.fillna(0), down_move.fillna(0))],
        index=df.index,
    )

    atr = compute_atr(df, period)
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr.replace(0, 1e-9))
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr.replace(0, 1e-9))
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9))
    adx = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return adx, plus_di, minus_di


# ── Utilidades de setup ────────────────────────────────────────────────────────
def infer_timeframe(df: pd.DataFrame) -> str:
    if len(df) < 3:
        return "unknown"
    diffs = df["ts"].diff().dropna().dt.total_seconds()
    median_secs = int(diffs.median())
    if median_secs % 86400 == 0:
        days = max(1, median_secs // 86400)
        return f"{days}d"
    if median_secs % 3600 == 0:
        hours = max(1, median_secs // 3600)
        return f"{hours}h"
    if median_secs % 60 == 0:
        minutes = max(1, median_secs // 60)
        return f"{minutes}m"
    return f"{median_secs}s"


def select_last_closed_index(df: pd.DataFrame) -> int:
    if len(df) < 3:
        return len(df) - 1

    diffs = df["ts"].diff().dropna().dt.total_seconds()
    interval_secs = float(diffs.median()) if not diffs.empty else 0.0
    if interval_secs <= 0:
        return len(df) - 2

    last_open_ts = df.iloc[-1]["ts"].timestamp()
    now_ts = time.time()
    if now_ts < (last_open_ts + interval_secs):
        return len(df) - 2
    return len(df) - 1


def get_regime(close: float, ema20: float, ema50: float, ema200: float) -> str:
    if close > ema20 > ema50 > ema200:
        return "BULL_STRONG"
    if close > ema50 > ema200 and ema20 > ema50:
        return "BULL_PULLBACK"
    if close > ema200:
        return "BULL_WEAK"
    return "NON_BULL"


def rsi_bucket(rsi: float) -> str:
    low = int(max(0, min(95, math.floor(rsi / 5) * 5)))
    high = low + 4
    return f"{low}-{high}"


def price_bucket(price: float, atr: float) -> str:
    step = atr * 0.75 if atr > 0 else price * 0.01
    if step <= 0:
        step = max(price * 0.01, 0.0001)
    bucket = int(round(price / step))
    return f"P{bucket}"


def determine_fib_context(df_closed: pd.DataFrame, idx: int, lookback: int = FIB_LOOKBACK) -> Dict[str, Any]:
    start = max(0, idx - lookback + 1)
    window = df_closed.iloc[start : idx + 1].copy()
    if len(window) < max(14, lookback // 2):
        return {
            "fib_value": None,
            "fib_zone": "OUTSIDE",
            "swing_low": None,
            "swing_high": None,
            "range": None,
            "zone_strength": 0,
        }

    low_idx = window["Low"].idxmin()
    high_idx = window["High"].idxmax()
    swing_low = float(window.loc[low_idx, "Low"])
    swing_high = float(window.loc[high_idx, "High"])
    current_close = float(df_closed.iloc[idx]["Close"])

    if not math.isfinite(swing_low) or not math.isfinite(swing_high) or swing_high <= swing_low:
        return {
            "fib_value": None,
            "fib_zone": "OUTSIDE",
            "swing_low": None,
            "swing_high": None,
            "range": None,
            "zone_strength": 0,
        }

    # Queremos una estructura alcista: primero el low y luego el high.
    if low_idx >= high_idx:
        return {
            "fib_value": None,
            "fib_zone": "OUTSIDE",
            "swing_low": swing_low,
            "swing_high": swing_high,
            "range": swing_high - swing_low,
            "zone_strength": 0,
        }

    fib_value = (swing_high - current_close) / (swing_high - swing_low)

    if fib_value < 0.236:
        fib_zone = "0.000-0.236"
        strength = 1
    elif fib_value < 0.382:
        fib_zone = "0.236-0.382"
        strength = 2
    elif fib_value < 0.500:
        fib_zone = "0.382-0.500"
        strength = 3
    elif fib_value < 0.618:
        fib_zone = "0.500-0.618"
        strength = 4
    elif fib_value <= 0.786:
        fib_zone = "0.618-0.786"
        strength = 3
    else:
        fib_zone = "DEEP_OR_BROKEN"
        strength = 0

    return {
        "fib_value": fib_value,
        "fib_zone": fib_zone,
        "swing_low": swing_low,
        "swing_high": swing_high,
        "range": swing_high - swing_low,
        "zone_strength": strength,
    }


def build_setup_key(signal: Dict[str, Any]) -> str:
    return "|".join(
        [
            signal["symbol"],
            signal["side"],
            signal["timeframe"],
            signal["regime"],
            signal["rsi_bucket"],
            signal["fib_zone"],
            signal["price_bucket"],
        ]
    )


def build_setup_hash(setup_key: str) -> str:
    return hashlib.sha256(setup_key.encode("utf-8")).hexdigest()


def fib_zone_strength(zone: str) -> int:
    mapping = {
        "0.000-0.236": 1,
        "0.236-0.382": 2,
        "0.382-0.500": 3,
        "0.500-0.618": 4,
        "0.618-0.786": 3,
        "DEEP_OR_BROKEN": 0,
        "OUTSIDE": 0,
    }
    return mapping.get(zone, 0)


def similarity_score(signal: Dict[str, Any], old: sqlite3.Row) -> float:
    score = 0.0

    if signal["regime"] == old["regime"]:
        score += 0.30
    if signal["rsi_bucket"] == old["rsi_bucket"]:
        score += 0.15
    if signal["fib_zone"] == old["fib_zone"]:
        score += 0.15
    if signal["price_bucket"] == old["price_bucket"]:
        score += 0.20

    price_tolerance = max(signal["atr"] * 0.75, signal["entry_price"] * 0.006)
    if abs(signal["entry_price"] - old["entry_price"]) <= price_tolerance:
        score += 0.10

    adx_delta = abs(signal["adx"] - old["adx"])
    if adx_delta <= 5:
        score += 0.05

    rr_delta = abs(signal["rr_ratio"] - old["rr_ratio"])
    if rr_delta <= 0.35:
        score += 0.05

    return score


def is_material_improvement(signal: Dict[str, Any], old: sqlite3.Row) -> bool:
    checks = 0

    if signal["score"] >= old["score"] + 1.0:
        checks += 1
    if signal["rr_ratio"] >= old["rr_ratio"] * 1.15:
        checks += 1
    if signal["adx"] >= old["adx"] + 4:
        checks += 1
    if fib_zone_strength(signal["fib_zone"]) > fib_zone_strength(old["fib_zone"]):
        checks += 1

    old_rsi_center = bucket_center(old["rsi_bucket"])
    new_rsi_center = bucket_center(signal["rsi_bucket"])
    if abs(new_rsi_center - 55) < abs(old_rsi_center - 55):
        checks += 1

    return checks >= 2


def bucket_center(bucket: str) -> float:
    try:
        low_s, high_s = bucket.split("-")
        return (float(low_s) + float(high_s)) / 2
    except Exception:
        return 50.0


# ── Evaluación ─────────────────────────────────────────────────────────────────
def evaluate(df: pd.DataFrame, symbol: str, cg_id: str) -> Optional[Dict[str, Any]]:
    try:
        if len(df) < 220:
            return None

        df = df.copy()
        df["ema20"] = df["Close"].ewm(span=20, adjust=False).mean()
        df["ema50"] = df["Close"].ewm(span=50, adjust=False).mean()
        df["ema200"] = df["Close"].ewm(span=200, adjust=False).mean()
        df["rsi"] = compute_rsi(df["Close"], 14)
        df["atr"] = compute_atr(df, 14)
        df["adx"], df["plus_di"], df["minus_di"] = compute_adx(df, 14)

        closed_idx = select_last_closed_index(df)
        row = df.iloc[closed_idx]
        timeframe = infer_timeframe(df)
        fib = determine_fib_context(df, closed_idx, FIB_LOOKBACK)

        close_price = float(row["Close"])
        ema20 = float(row["ema20"])
        ema50 = float(row["ema50"])
        ema200 = float(row["ema200"])
        rsi = float(row["rsi"])
        atr = float(row["atr"])
        adx = float(row["adx"])
        plus_di = float(row["plus_di"])
        minus_di = float(row["minus_di"])

        if any(math.isnan(v) for v in [close_price, ema20, ema50, ema200, rsi, atr, adx, plus_di, minus_di]):
            return None

        regime = get_regime(close_price, ema20, ema50, ema200)
        reasons: List[str] = []
        score = 0.0

        if close_price > ema200:
            score += 1.5
            reasons.append("Precio > EMA200")
        if ema20 > ema50 > ema200:
            score += 1.5
            reasons.append("Estructura EMA20 > EMA50 > EMA200")
        if close_price > ema20:
            score += 1.0
            reasons.append("Precio > EMA20")

        ema20_prev = float(df.iloc[closed_idx - 1]["ema20"])
        ema50_prev = float(df.iloc[closed_idx - 1]["ema50"])
        if ema20 > ema20_prev and ema50 > ema50_prev:
            score += 0.75
            reasons.append("Pendiente positiva en EMA20/EMA50")

        if 48 <= rsi <= 62:
            score += 1.25
            reasons.append(f"RSI balanceado ({rsi:.1f})")
        elif 45 <= rsi < 48 or 62 < rsi <= 68:
            score += 0.75
            reasons.append(f"RSI aceptable ({rsi:.1f})")

        if adx >= 20:
            score += 1.0
            reasons.append(f"ADX con fuerza ({adx:.1f})")
        elif adx >= 17:
            score += 0.5
            reasons.append(f"ADX moderado ({adx:.1f})")

        if plus_di > minus_di:
            score += 0.5
            reasons.append("Dominancia alcista (+DI > -DI)")

        if fib["fib_zone"] in {"0.382-0.500", "0.500-0.618"}:
            score += 1.0
            reasons.append(f"Retroceso Fibonacci sano ({fib['fib_zone']})")
        elif fib["fib_zone"] in {"0.236-0.382", "0.618-0.786"}:
            score += 0.5
            reasons.append(f"Fibonacci utilizable ({fib['fib_zone']})")

        swing_low = fib["swing_low"] if fib["swing_low"] is not None else float(df.iloc[max(0, closed_idx - 20): closed_idx + 1]["Low"].min())
        swing_high = fib["swing_high"] if fib["swing_high"] is not None else float(df.iloc[max(0, closed_idx - 20): closed_idx + 1]["High"].max())
        swing_range = max((fib["range"] or 0.0), atr * 3)

        stop_loss = min(close_price - (1.8 * atr), swing_low - (0.25 * atr))
        if stop_loss >= close_price:
            stop_loss = close_price - (1.8 * atr)
        risk = close_price - stop_loss
        if risk <= 0:
            return None

        extension_target = swing_high + (0.272 * swing_range)
        rr_floor_target = close_price + (risk * 2.0)
        take_profit = max(extension_target, rr_floor_target)
        rr_ratio = (take_profit - close_price) / risk

        if rr_ratio >= MIN_RR:
            score += 0.5
            reasons.append(f"R:R suficiente ({rr_ratio:.2f})")

        side = SIDE_LONG
        signal = {
            "symbol": symbol,
            "cg_id": cg_id,
            "side": side,
            "timeframe": timeframe,
            "candle_ts": int(row["ts"].timestamp()),
            "entry_price": close_price,
            "price": close_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "rr_ratio": rr_ratio,
            "score": round(score, 2),
            "reasons": reasons,
            "regime": regime,
            "rsi": rsi,
            "rsi_bucket": rsi_bucket(rsi),
            "fib_value": fib["fib_value"],
            "fib_zone": fib["fib_zone"],
            "price_bucket": price_bucket(close_price, atr),
            "atr": atr,
            "adx": adx,
            "ema20": ema20,
            "ema50": ema50,
            "ema200": ema200,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "swing_low": swing_low,
            "swing_high": swing_high,
        }
        signal["setup_key"] = build_setup_key(signal)
        signal["setup_hash"] = build_setup_hash(signal["setup_key"])

        signal["alert"] = (
            regime in {"BULL_STRONG", "BULL_PULLBACK"}
            and rr_ratio >= MIN_RR
            and score >= MIN_SCORE
            and fib["fib_zone"] != "DEEP_OR_BROKEN"
        )

        print(
            f"🔍 {symbol} [{timeframe}] | score={signal['score']:.2f} | "
            f"rr={signal['rr_ratio']:.2f} | regime={regime} | fib={signal['fib_zone']}"
        )
        return signal
    except Exception as exc:
        print(f"❌ Error evaluando {symbol}: {exc}")
        return None


# ── Lógica de deduplicación e invalidación ─────────────────────────────────────
def get_recent_active_alerts(
    conn: sqlite3.Connection,
    symbol: str,
    side: str,
    timeframe: str,
    cutoff_ts: int,
) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM alerts
        WHERE symbol = ?
          AND side = ?
          AND timeframe = ?
          AND status = ?
          AND sent_at >= ?
        ORDER BY sent_at DESC
        """,
        (symbol, side, timeframe, ACTIVE, cutoff_ts),
    ).fetchall()


def get_latest_active_alert(
    conn: sqlite3.Connection,
    symbol: str,
    side: str,
    timeframe: str,
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM alerts
        WHERE symbol = ?
          AND side = ?
          AND timeframe = ?
          AND status = ?
        ORDER BY sent_at DESC
        LIMIT 1
        """,
        (symbol, side, timeframe, ACTIVE),
    ).fetchone()


def should_invalidate(signal: Dict[str, Any], active_alert: sqlite3.Row) -> Tuple[bool, str]:
    if signal["entry_price"] <= active_alert["stop_loss"]:
        return True, "Precio cerró por debajo del stop técnico"
    if signal["ema20"] <= signal["ema50"]:
        return True, "EMA20 perdió la ventaja sobre EMA50"
    if signal["entry_price"] <= signal["ema50"]:
        return True, "Precio cerró por debajo de EMA50"
    if signal["rsi"] < 42:
        return True, "RSI cayó por debajo del umbral de continuidad"
    if signal["fib_zone"] == "DEEP_OR_BROKEN":
        return True, "Retroceso Fibonacci demasiado profundo"
    return False, ""


def invalidate_alert(conn: sqlite3.Connection, alert_id: int, reason: str) -> None:
    now_ts = int(time.time())
    conn.execute(
        """
        UPDATE alerts
        SET status = ?, invalidated_at = ?, invalidation_reason = ?
        WHERE id = ? AND status = ?
        """,
        (INVALIDATED, now_ts, reason, alert_id, ACTIVE),
    )
    conn.commit()


def check_legacy_cooldown(conn: sqlite3.Connection, symbol: str, cutoff_ts: int) -> bool:
    row = conn.execute(
        "SELECT sent_at FROM legacy_symbol_cooldowns WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    return bool(row and row["sent_at"] >= cutoff_ts)


def should_send_alert(conn: sqlite3.Connection, signal: Dict[str, Any]) -> Tuple[bool, Optional[int], str]:
    now_ts = int(time.time())
    cutoff_ts = now_ts - (COOLDOWN_HOURS * 3600)

    if check_legacy_cooldown(conn, signal["symbol"], cutoff_ts):
        return False, None, "Cooldown heredado desde alert_state.json"

    active_alerts = get_recent_active_alerts(
        conn,
        signal["symbol"],
        signal["side"],
        signal["timeframe"],
        cutoff_ts,
    )

    if not active_alerts:
        return True, None, "No hay setup activo similar en las últimas 24h"

    for old in active_alerts:
        if old["setup_hash"] == signal["setup_hash"]:
            if is_material_improvement(signal, old):
                return True, old["id"], "Misma idea, pero con mejora material"
            return False, old["id"], "Setup idéntico dentro de la ventana de 24h"

        similarity = similarity_score(signal, old)
        if similarity >= 0.80:
            if is_material_improvement(signal, old):
                return True, old["id"], f"Setup similar ({similarity:.2f}) con mejora material"
            return False, old["id"], f"Setup similar ({similarity:.2f}) dentro de la ventana de 24h"

    return True, None, "El setup es suficientemente distinto"


def expire_old_active_alerts(conn: sqlite3.Connection) -> None:
    cutoff_ts = int(time.time()) - (COOLDOWN_HOURS * 3600)
    conn.execute(
        "UPDATE alerts SET status = ? WHERE status = ? AND sent_at < ?",
        (EXPIRED, ACTIVE, cutoff_ts),
    )
    conn.commit()


# ── Registro ───────────────────────────────────────────────────────────────────
def save_alert(conn: sqlite3.Connection, signal: Dict[str, Any], improved_from_alert_id: Optional[int]) -> None:
    now_ts = int(time.time())
    conn.execute(
        """
        INSERT INTO alerts (
            symbol, cg_id, side, timeframe, setup_key, setup_hash, regime,
            rsi_bucket, fib_zone, price_bucket, candle_ts, entry_price,
            stop_loss, take_profit, rr_ratio, score, adx, rsi, atr,
            reasons_json, status, sent_at, improved_from_alert_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal["symbol"],
            signal["cg_id"],
            signal["side"],
            signal["timeframe"],
            signal["setup_key"],
            signal["setup_hash"],
            signal["regime"],
            signal["rsi_bucket"],
            signal["fib_zone"],
            signal["price_bucket"],
            signal["candle_ts"],
            signal["entry_price"],
            signal["stop_loss"],
            signal["take_profit"],
            signal["rr_ratio"],
            signal["score"],
            signal["adx"],
            signal["rsi"],
            signal["atr"],
            json.dumps(signal["reasons"], ensure_ascii=False),
            ACTIVE,
            now_ts,
            improved_from_alert_id,
        ),
    )
    conn.commit()


# ── Mensaje ────────────────────────────────────────────────────────────────────
def format_message(signal: Dict[str, Any], decision_reason: str) -> str:
    fib_label = signal["fib_zone"]
    rsi_label = f"{signal['rsi']:.1f}"
    return (
        f"🚀 *ALERTA COMPRA: {signal['symbol']}*\n\n"
        f"⏱️ *Timeframe:* {signal['timeframe']}\n"
        f"💰 *Precio:* ${signal['entry_price']:.4f}\n"
        f"📊 *Score:* {signal['score']:.2f}\n"
        f"⚖️ *R:R:* {signal['rr_ratio']:.2f}\n"
        f"📐 *Fibonacci:* {fib_label}\n"
        f"📈 *RSI:* {rsi_label}\n"
        f"🔥 *ADX:* {signal['adx']:.1f}\n"
        f"🎯 *TARGET (TP):* ${signal['take_profit']:.4f}\n"
        f"🛑 *STOP (SL):* ${signal['stop_loss']:.4f}\n\n"
        f"🧠 *Setup:* {signal['setup_key']}\n"
        f"📝 *Motivo de envío:* {decision_reason}\n"
        f"🔎 *Confluencias:* {', '.join(signal['reasons'])}"
    )


# ── Flujo principal ────────────────────────────────────────────────────────────
def process_symbol(conn: sqlite3.Connection, cg_id: str, symbol: str) -> bool:
    df = get_data(cg_id)
    if df is None:
        return False

    signal = evaluate(df, symbol, cg_id)
    if signal is None:
        return False

    latest_active = get_latest_active_alert(conn, symbol, SIDE_LONG, signal["timeframe"])
    if latest_active:
        invalidate, reason = should_invalidate(signal, latest_active)
        if invalidate:
            invalidate_alert(conn, latest_active["id"], reason)
            print(f"🧹 {symbol}: setup previo invalidado -> {reason}")

    if not signal["alert"]:
        print(f"ℹ️ {symbol}: setup no califica para alerta.")
        return False

    should_send, improved_from_alert_id, decision_reason = should_send_alert(conn, signal)
    if not should_send:
        print(f"⏳ {symbol}: alerta suprimida -> {decision_reason}")
        return False

    message = format_message(signal, decision_reason)
    send_telegram(message)
    save_alert(conn, signal, improved_from_alert_id)
    print(f"✅ {symbol}: alerta enviada.")
    return True


def main() -> None:
    start_ts = time.time()
    conn = get_db_connection()
    try:
        init_db(conn)
        import_legacy_state_if_needed(conn)
        expire_old_active_alerts(conn)

        print(f"🚀 Iniciando escaneo de {len(CRYPTO_IDS)} activos...")
        total_alerts = 0

        for cg_id, symbol in CRYPTO_IDS.items():
            try:
                if process_symbol(conn, cg_id, symbol):
                    total_alerts += 1
            except Exception as exc:
                print(f"❌ Error procesando {symbol}: {exc}")
            time.sleep(SLEEP_BETWEEN_ASSETS)

        elapsed = time.time() - start_ts
        print(f"🏁 Fin del escaneo. Alertas enviadas: {total_alerts}. Duración: {elapsed:.1f}s")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
