import hashlib
import html
import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CG_API_KEY = os.getenv("COINGECKO_API_KEY", "")

DB_FILE = os.getenv("ALERT_DB_FILE", "alerts_state.db")
LEGACY_STATE_FILE = os.getenv("LEGACY_STATE_FILE", "alert_state.json")
VS_CURRENCY = os.getenv("VS_CURRENCY", "usd")
MARKET_CHART_DAYS = int(os.getenv("MARKET_CHART_DAYS", "90"))
BASE_INTERVAL = os.getenv("BASE_INTERVAL", "hourly")
TRADING_TIMEFRAME = os.getenv("TRADING_TIMEFRAME", "4h")
COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "24"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "6.0"))
MIN_RR = float(os.getenv("MIN_RR", "2.0"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
SLEEP_BETWEEN_ASSETS = float(os.getenv("SLEEP_BETWEEN_ASSETS", "1.0"))
FIB_LOOKBACK = int(os.getenv("FIB_LOOKBACK", "55"))
ENABLE_RANKING = os.getenv("ENABLE_RANKING", "true").lower() == "true"
MAX_ALERTS_PER_RUN = int(os.getenv("MAX_ALERTS_PER_RUN", "2"))
MAX_ALERTS_PER_GROUP = int(os.getenv("MAX_ALERTS_PER_GROUP", "1"))
SEND_RUN_SUMMARY = os.getenv("SEND_RUN_SUMMARY", "true").lower() == "true"

SIDE_LONG = "LONG"
ACTIVE = "ACTIVE"
INVALIDATED = "INVALIDATED"
EXPIRED = "EXPIRED"


def send_telegram(message: str) -> bool:
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
    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"⚠️ Telegram respondió {response.status_code}: {response.text[:300]}")
            return False
        body = response.json()
        if not body.get("ok", False):
            print(f"⚠️ Telegram rechazó el mensaje: {str(body)[:300]}")
            return False
        return True
    except Exception as exc:
        print(f"❌ Error enviando a Telegram: {exc}")
        return False


# ── Persistencia SQLite ───────────────────────────────────────────────────────
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
            if not isinstance(symbol, str) or not isinstance(raw_ts, (int, float)):
                continue
            conn.execute(
                "INSERT INTO legacy_symbol_cooldowns(symbol, sent_at) VALUES(?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET sent_at = excluded.sent_at",
                (symbol, int(raw_ts)),
            )
            imported += 1
        conn.commit()
        print(f"📦 Migración legacy completada: {imported} cooldowns importados.")
    except Exception as exc:
        print(f"⚠️ No se pudo migrar {LEGACY_STATE_FILE}: {exc}")
    finally:
        set_meta(conn, "legacy_import_done", "1")


# ── Datos de mercado ──────────────────────────────────────────────────────────
def get_hourly_prices(cg_id: str) -> Optional[pd.DataFrame]:
    url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart"
    headers = {"accept": "application/json"}
    if CG_API_KEY:
        headers["x-cg-demo-api-key"] = CG_API_KEY

    params = {
        "vs_currency": VS_CURRENCY,
        "days": str(MARKET_CHART_DAYS),
        "interval": BASE_INTERVAL,
        "precision": "full",
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"⚠️ {cg_id}: CoinGecko respondió {response.status_code}: {response.text[:180]}")
            return None

        payload = response.json()
        prices = payload.get("prices", []) if isinstance(payload, dict) else []
        if len(prices) < 500:
            print(f"⚠️ {cg_id}: datos horarios insuficientes ({len(prices)} puntos).")
            return None

        df = pd.DataFrame(prices, columns=["ts", "price"])
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.dropna().sort_values("ts").drop_duplicates(subset=["ts"]).reset_index(drop=True)
        return df
    except Exception as exc:
        print(f"❌ Error obteniendo datos para {cg_id}: {exc}")
        return None


def build_ohlc_from_hourly(price_df: pd.DataFrame) -> Optional[pd.DataFrame]:
    if price_df is None or price_df.empty:
        return None

    df = price_df.copy().set_index("ts")
    ohlc = df["price"].resample(TRADING_TIMEFRAME, label="right", closed="right").ohlc()
    ohlc.columns = ["Open", "High", "Low", "Close"]
    ohlc = ohlc.dropna().reset_index()

    if len(ohlc) < 220:
        print(f"⚠️ OHLC reconstruido insuficiente ({len(ohlc)} velas {TRADING_TIMEFRAME}).")
        return None

    return ohlc.iloc[:-1].reset_index(drop=True)


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


def fibonacci_context(df: pd.DataFrame, lookback: int = FIB_LOOKBACK) -> Dict[str, Any]:
    recent = df.tail(lookback).copy()
    swing_low = float(recent["Low"].min())
    swing_high = float(recent["High"].max())
    close = float(recent.iloc[-1]["Close"])

    amplitude = max(swing_high - swing_low, 1e-9)
    retracement = (swing_high - close) / amplitude
    retracement = max(0.0, min(1.0, retracement))

    return {
        "swing_low": swing_low,
        "swing_high": swing_high,
        "retracement": retracement,
        "fib_382": swing_high - amplitude * 0.382,
        "fib_500": swing_high - amplitude * 0.500,
        "fib_618": swing_high - amplitude * 0.618,
        "fib_786": swing_high - amplitude * 0.786,
    }


def compute_vwap_proximity(df: pd.DataFrame, lookback: int = 20) -> Dict[str, Any]:
    """
    Calcula el VWAP aproximado usando las últimas `lookback` velas 4H.
    Dado que CoinGecko no provee volumen real, usamos un VWAP sintético
    basado en precio típico ponderado por rango de vela (proxy de actividad).

    Retorna la distancia porcentual del precio actual al VWAP y si el precio
    está por encima o debajo — clave para evaluar si estamos en zona de valor
    o sobreextendidos.
    """
    recent = df.tail(lookback).copy()
    typical_price = (recent["High"] + recent["Low"] + recent["Close"]) / 3
    # Rango como proxy de volumen: velas con más rango = más actividad
    candle_range = (recent["High"] - recent["Low"]).clip(lower=1e-9)
    vwap = (typical_price * candle_range).sum() / candle_range.sum()
    close = float(recent.iloc[-1]["Close"])
    distance_pct = (close - vwap) / vwap * 100

    return {
        "vwap": round(float(vwap), 6),
        "distance_pct": round(distance_pct, 2),
        "above_vwap": close > vwap,
    }


def compute_volume_momentum(df: pd.DataFrame, lookback: int = 10) -> Dict[str, Any]:
    """
    Evalúa el momentum de volumen relativo usando rango de velas como proxy.
    Compara el rango promedio de las últimas 3 velas vs el promedio histórico.

    Un rally con rango decreciente = compradores sin convicción.
    Un rally con rango creciente = momentum institucional real.
    """
    recent = df.tail(lookback).copy()
    candle_range = recent["High"] - recent["Low"]
    avg_range = float(candle_range.mean())
    last_3_avg = float(candle_range.tail(3).mean())
    relative_volume = last_3_avg / max(avg_range, 1e-9)

    # Detectar si el precio sube pero el rango cae (divergencia bajista)
    price_up = float(recent.iloc[-1]["Close"]) > float(recent.iloc[-3]["Close"])
    range_declining = last_3_avg < avg_range * 0.85

    return {
        "relative_volume": round(relative_volume, 2),
        "divergence": price_up and range_declining,  # Sube precio, cae volumen
        "strong_momentum": relative_volume >= 1.15,  # Volumen superior al promedio
    }


# ── Setup key / buckets ──────────────────────────────────────────────────────
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


def fib_zone(retracement: float) -> str:
    if 0.382 <= retracement < 0.500:
        return "0.382-0.500"
    if 0.500 <= retracement < 0.618:
        return "0.500-0.618"
    if 0.618 <= retracement <= 0.786:
        return "0.618-0.786"
    return "OUTSIDE"


def price_bucket(price: float, atr: float) -> str:
    step = max(atr * 0.75, price * 0.005, 1e-9)
    return str(int(round(price / step)))


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


def asset_group(symbol: str) -> str:
    return ASSET_GROUPS.get(symbol, "Other")


# ── Señal ─────────────────────────────────────────────────────────────────────
def evaluate(df: pd.DataFrame, symbol: str, cg_id: str) -> Optional[Dict[str, Any]]:
    if df is None or len(df) < 220:
        print(f"⚠️ {cg_id}: velas insuficientes tras reconstrucción ({0 if df is None else len(df)}).")
        return None

    work = df.copy()
    work["ema20"] = work["Close"].ewm(span=20, adjust=False).mean()
    work["ema50"] = work["Close"].ewm(span=50, adjust=False).mean()
    work["ema200"] = work["Close"].ewm(span=200, adjust=False).mean()
    work["rsi"] = compute_rsi(work["Close"], 14)
    work["atr"] = compute_atr(work, 14)
    work["adx"], work["plus_di"], work["minus_di"] = compute_adx(work, 14)
    work = work.dropna().reset_index(drop=True)

    if len(work) < 210:
        print(f"⚠️ {symbol}: historial útil insuficiente tras indicadores ({len(work)} velas).")
        return None

    last = work.iloc[-1]
    prev = work.iloc[-2]
    fib = fibonacci_context(work, FIB_LOOKBACK)

    close = float(last["Close"])
    atr = float(last["atr"])
    adx = float(last["adx"])
    rsi = float(last["rsi"])
    plus_di = float(last["plus_di"])
    minus_di = float(last["minus_di"])
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])
    ema200 = float(last["ema200"])

    if not all(math.isfinite(x) for x in [close, atr, adx, rsi, plus_di, minus_di, ema20, ema50, ema200]):
        return None

    score = 0.0
    reasons: List[str] = []

    regime = get_regime(last)
    bullish_cross = float(prev["ema20"]) <= float(prev["ema50"]) and ema20 > ema50

    if regime == "BULL_STACK":
        score += 2.5
        reasons.append("Régimen alcista EMA20>EMA50>EMA200")
    if close > ema20:
        score += 1.0
        reasons.append("Precio sobre EMA20")
    if close > ema50:
        score += 1.0
        reasons.append("Precio sobre EMA50")
    if close > ema200:
        score += 1.0
        reasons.append("Precio sobre EMA200")
    if bullish_cross:
        score += 0.75
        reasons.append("Cruce EMA20/EMA50 confirmado")
    if 48 <= rsi <= 64:
        score += 1.0
        reasons.append(f"RSI sano ({rsi:.1f})")
    elif 45 <= rsi <= 68:
        score += 0.5
        reasons.append(f"RSI aceptable ({rsi:.1f})")
    if adx >= 22 and plus_di > minus_di:
        score += 1.25
        reasons.append(f"ADX con dirección ({adx:.1f})")
    elif adx >= 18 and plus_di > minus_di:
        score += 0.75
        reasons.append(f"ADX emergente ({adx:.1f})")

    zone = fib_zone(fib["retracement"])
    if zone == "0.382-0.500":
        score += 0.5
        reasons.append("Retroceso Fib 0.382-0.500")
    elif zone == "0.500-0.618":
        score += 1.0
        reasons.append("Retroceso Fib 0.500-0.618")
    elif zone == "0.618-0.786":
        score += 0.75
        reasons.append("Retroceso Fib 0.618-0.786")

    # ── VWAP: ¿Está el precio cerca del valor institucional? ──────────────────
    vwap_data = compute_vwap_proximity(work, lookback=20)
    vwap_dist = vwap_data["distance_pct"]
    if vwap_data["above_vwap"] and vwap_dist <= 1.5:
        # Precio sobre VWAP y cerca — zona de valor, entrada limpia
        score += 1.0
        reasons.append(f"Precio cerca del VWAP (+{vwap_dist:.1f}%)")
    elif vwap_data["above_vwap"] and vwap_dist <= 3.5:
        # Algo extendido pero aún razonable
        score += 0.4
        reasons.append(f"Precio sobre VWAP (+{vwap_dist:.1f}%)")
    elif vwap_data["above_vwap"] and vwap_dist > 3.5:
        # Precio muy extendido respecto al VWAP — sobrecompra de corto plazo
        score -= 1.0
        reasons.append(f"⚠️ Precio sobreextendido sobre VWAP (+{vwap_dist:.1f}%)")
    elif not vwap_data["above_vwap"]:
        # Precio bajo el VWAP — institucionales aún no están comprando aquí
        score -= 0.5
        reasons.append(f"Precio bajo VWAP ({vwap_dist:.1f}%)")

    # ── Momentum de volumen relativo ──────────────────────────────────────────
    vol_data = compute_volume_momentum(work, lookback=10)
    if vol_data["divergence"]:
        # Rally sin convicción: precio sube pero rango cae — señal de trampa
        score -= 1.5
        reasons.append("⚠️ Divergencia volumen: precio sube, momentum cae")
    elif vol_data["strong_momentum"]:
        score += 0.75
        reasons.append("Momentum de volumen fuerte")

    stop_loss = max(fib["swing_low"], close - (atr * 1.8))
    if stop_loss >= close:
        stop_loss = close - max(atr * 1.5, close * 0.01)
    take_profit = close + max((close - stop_loss) * 2.4, atr * 2.8)
    rr_ratio = (take_profit - close) / max(close - stop_loss, 1e-9)

    valid = (
        regime == "BULL_STACK"
        and close > ema200
        and plus_di > minus_di
        and adx >= 18
        and rr_ratio >= MIN_RR
        and score >= MIN_SCORE
    )

    candidate = {
        "symbol": symbol,
        "cg_id": cg_id,
        "side": SIDE_LONG,
        "timeframe": TRADING_TIMEFRAME,
        "regime": regime,
        "rsi_bucket": rsi_bucket(rsi),
        "fib_zone": zone,
        "price_bucket": price_bucket(close, atr),
        "candle_ts": int(pd.Timestamp(last["ts"]).timestamp()),
        "entry_price": close,
        "stop_loss": float(stop_loss),
        "take_profit": float(take_profit),
        "rr_ratio": float(rr_ratio),
        "score": round(score, 2),
        "adx": round(adx, 2),
        "rsi": round(rsi, 2),
        "atr": float(atr),
        "reasons": reasons,
        "bullish_cross": bullish_cross,
        "fib_retracement": round(float(fib["retracement"]), 4),
        "swing_low": float(fib["swing_low"]),
        "swing_high": float(fib["swing_high"]),
        "asset_group": asset_group(symbol),
        "vwap": vwap_data["vwap"],
        "vwap_distance_pct": vwap_data["distance_pct"],
        "above_vwap": vwap_data["above_vwap"],
        "volume_divergence": vol_data["divergence"],
        "volume_strong": vol_data["strong_momentum"],
        "alert": valid,
    }
    candidate["setup_key"] = build_setup_key(candidate)
    candidate["setup_hash"] = build_setup_hash(candidate["setup_key"])

    print(
        f"🔍 {symbol}: score={candidate['score']}, rr={candidate['rr_ratio']:.2f}, "
        f"regime={candidate['regime']}, fib={candidate['fib_zone']}, alert={candidate['alert']}"
    )
    return candidate


# ── Invalidación / deduplicación ──────────────────────────────────────────────
def invalidate_old_alerts(conn: sqlite3.Connection, candidate: Dict[str, Any]) -> None:
    rows = conn.execute(
        """
        SELECT *
        FROM alerts
        WHERE symbol = ?
          AND side = ?
          AND timeframe = ?
          AND status = 'ACTIVE'
        ORDER BY sent_at DESC
        """,
        (candidate["symbol"], candidate["side"], candidate["timeframe"]),
    ).fetchall()

    now_ts = int(time.time())
    for row in rows:
        reason = None
        if candidate["regime"] != "BULL_STACK":
            reason = "Regimen perdido"
        elif candidate["entry_price"] <= float(row["stop_loss"]):
            reason = "Stop tecnico vulnerado"
        elif candidate["rsi"] < 40:
            reason = "RSI debilitado"
        elif candidate["entry_price"] < float(row["entry_price"]) - max(candidate["atr"], candidate["entry_price"] * 0.01):
            reason = "Precio deteriorado"

        if reason:
            conn.execute(
                """
                UPDATE alerts
                SET status = ?, invalidated_at = ?, invalidation_reason = ?
                WHERE id = ?
                """,
                (INVALIDATED, now_ts, reason, row["id"]),
            )
    conn.commit()


def is_material_improvement(candidate: Dict[str, Any], row: sqlite3.Row) -> bool:
    score_better = candidate["score"] >= float(row["score"]) + 1.0
    rr_better = candidate["rr_ratio"] >= float(row["rr_ratio"]) + 0.20
    fib_better = candidate["fib_zone"] in {"0.500-0.618", "0.618-0.786"} and row["fib_zone"] == "0.382-0.500"
    fresh_cross = candidate.get("bullish_cross", False)
    return score_better or rr_better or fib_better or fresh_cross


def is_similar_setup(candidate: Dict[str, Any], row: sqlite3.Row) -> bool:
    if row["setup_hash"] == candidate["setup_hash"]:
        return True

    score = 0.0
    if row["regime"] == candidate["regime"]:
        score += 0.35
    if row["rsi_bucket"] == candidate["rsi_bucket"]:
        score += 0.15
    if row["fib_zone"] == candidate["fib_zone"]:
        score += 0.20
    if row["price_bucket"] == candidate["price_bucket"]:
        score += 0.20

    price_close = abs(float(row["entry_price"]) - candidate["entry_price"]) <= max(candidate["atr"] * 0.8, candidate["entry_price"] * 0.006)
    if price_close:
        score += 0.10

    return score >= 0.75


def blocked_by_legacy_cooldown(conn: sqlite3.Connection, symbol: str, now_ts: int) -> bool:
    row = conn.execute(
        "SELECT sent_at FROM legacy_symbol_cooldowns WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    if not row:
        return False
    return (now_ts - int(row["sent_at"])) < COOLDOWN_HOURS * 3600


def should_send_alert(conn: sqlite3.Connection, candidate: Dict[str, Any]) -> Tuple[bool, Optional[int], str]:
    now_ts = int(time.time())
    cutoff = now_ts - (COOLDOWN_HOURS * 3600)

    if blocked_by_legacy_cooldown(conn, candidate["symbol"], now_ts):
        return False, None, "Cooldown heredado aún vigente"

    rows = conn.execute(
        """
        SELECT *
        FROM alerts
        WHERE symbol = ?
          AND side = ?
          AND timeframe = ?
          AND sent_at >= ?
          AND status = 'ACTIVE'
        ORDER BY sent_at DESC
        """,
        (candidate["symbol"], candidate["side"], candidate["timeframe"], cutoff),
    ).fetchall()

    if not rows:
        return True, None, "Sin alerta similar activa"

    for row in rows:
        if is_similar_setup(candidate, row):
            if is_material_improvement(candidate, row):
                return True, int(row["id"]), "Mejora material sobre alerta activa"
            return False, int(row["id"]), "Setup similar dentro de 24h"

    return True, None, "No hay setup comparable en 24h"


def save_alert(conn: sqlite3.Connection, candidate: Dict[str, Any], improved_from_alert_id: Optional[int]) -> None:
    now_ts = int(time.time())
    conn.execute(
        """
        INSERT INTO alerts (
            symbol, cg_id, side, timeframe, setup_key, setup_hash, regime, rsi_bucket,
            fib_zone, price_bucket, candle_ts, entry_price, stop_loss, take_profit,
            rr_ratio, score, adx, rsi, atr, reasons_json, status, sent_at,
            improved_from_alert_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            candidate["candle_ts"],
            candidate["entry_price"],
            candidate["stop_loss"],
            candidate["take_profit"],
            candidate["rr_ratio"],
            candidate["score"],
            candidate["adx"],
            candidate["rsi"],
            candidate["atr"],
            json.dumps(candidate["reasons"], ensure_ascii=False),
            ACTIVE,
            now_ts,
            improved_from_alert_id,
        ),
    )
    conn.commit()


# ── Ranking y selección ───────────────────────────────────────────────────────
def compute_rank_score(candidate: Dict[str, Any]) -> Tuple[float, List[str]]:
    notes: List[str] = []
    rank = 0.0

    rank += candidate["score"] * 9.0
    notes.append(f"score {candidate['score']:.2f}")

    adx_component = min(candidate["adx"], 40.0) * 0.6
    rank += adx_component
    notes.append(f"adx {candidate['adx']:.1f}")

    ideal_rsi = 57.0
    rsi_alignment = max(0.0, 1.0 - abs(candidate["rsi"] - ideal_rsi) / 14.0)
    rsi_component = rsi_alignment * 6.0
    rank += rsi_component
    notes.append(f"rsi-fit {rsi_component:.2f}")

    rr_component = min(candidate["rr_ratio"], 3.2) * 2.2
    rank += rr_component
    notes.append(f"rr {candidate['rr_ratio']:.2f}")

    fib_bonus_map = {
        "OUTSIDE": -2.0,
        "0.382-0.500": 0.8,
        "0.500-0.618": 2.0,
        "0.618-0.786": 1.5,
    }
    fib_component = fib_bonus_map.get(candidate["fib_zone"], 0.0)
    rank += fib_component
    notes.append(f"fib {candidate['fib_zone']}")

    if candidate.get("bullish_cross"):
        rank += 2.5
        notes.append("cruce reciente")

    risk_pct = max((candidate["entry_price"] - candidate["stop_loss"]) / max(candidate["entry_price"], 1e-9), 0.0)
    if risk_pct < 0.006:
        rank -= 1.5
        notes.append("stop estrecho")
    elif risk_pct < 0.01:
        rank -= 0.5
        notes.append("stop ajustado")

    if candidate["symbol"] in {"BTC", "ETH"}:
        rank += 1.25
        notes.append("major")

    return round(rank, 2), notes


def rank_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ranked: List[Dict[str, Any]] = []
    for candidate in candidates:
        clone = dict(candidate)
        rank_score, rank_notes = compute_rank_score(clone)
        clone["rank_score"] = rank_score
        clone["rank_notes"] = rank_notes
        ranked.append(clone)

    ranked.sort(
        key=lambda item: (
            item["rank_score"],
            item["score"],
            item["adx"],
            item["rr_ratio"],
        ),
        reverse=True,
    )
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


# ── Formato de mensajes ───────────────────────────────────────────────────────
def format_message(candidate: Dict[str, Any], decision_reason: str) -> str:
    esc = html.escape
    reasons_text = esc(", ".join(candidate["reasons"]))
    rank_line = ""
    if ENABLE_RANKING:
        rank_line = (
            f"🏅 <b>Prioridad:</b> {candidate['rank_score']:.2f}"
            f" | Grupo: {esc(candidate['asset_group'])}\n"
        )

    # Contexto VWAP
    vwap_dist = candidate.get("vwap_distance_pct", 0.0)
    vwap_icon = "🟢" if candidate.get("above_vwap") and abs(vwap_dist) <= 1.5 else ("🟡" if abs(vwap_dist) <= 3.5 else "🔴")
    vwap_label = f"{'+' if vwap_dist >= 0 else ''}{vwap_dist:.1f}% vs VWAP ${candidate.get('vwap', 0):.4f}"

    # Contexto volumen
    if candidate.get("volume_divergence"):
        vol_label = "⚠️ Divergencia (sube precio, cae momentum)"
    elif candidate.get("volume_strong"):
        vol_label = "✅ Momentum fuerte"
    else:
        vol_label = "➖ Momentum neutral"

    return (
        f"🚀 <b>ALERTA COMPRA: {esc(candidate['symbol'])}</b>\n\n"
        f"⏱️ <b>Timeframe:</b> {esc(candidate['timeframe'])}\n"
        f"💰 <b>Precio:</b> ${candidate['entry_price']:.4f}\n"
        f"📊 <b>Score:</b> {candidate['score']:.2f}\n"
        f"📈 <b>ADX:</b> {candidate['adx']:.2f}\n"
        f"📉 <b>RSI:</b> {candidate['rsi']:.2f}\n"
        f"🧭 <b>Régimen:</b> {esc(candidate['regime'])}\n"
        f"🧩 <b>Fib:</b> {esc(candidate['fib_zone'])}\n"
        f"⚖️ <b>R:R:</b> {candidate['rr_ratio']:.2f}\n"
        f"🎯 <b>TARGET (TP):</b> ${candidate['take_profit']:.4f}\n"
        f"🛑 <b>STOP (SL):</b> ${candidate['stop_loss']:.4f}\n"
        f"{vwap_icon} <b>VWAP:</b> {esc(vwap_label)}\n"
        f"📦 <b>Volumen:</b> {esc(vol_label)}\n"
        f"{rank_line}\n"
        f"📝 <b>Análisis:</b> {reasons_text}\n"
        f"🧠 <b>Motivo de envío:</b> {esc(decision_reason)}"
    )


def format_run_summary(
    selected: List[Dict[str, Any]],
    deferred: List[Dict[str, Any]],
    blocked: List[str],
    total_valid: int,
) -> str:
    esc = html.escape
    lines = ["📋 <b>Resumen de ejecución</b>", ""]
    lines.append(f"✅ <b>Setups válidos:</b> {total_valid}")
    if ENABLE_RANKING:
        lines.append(f"🚦 <b>Ranking activo:</b> sí | Límite: {MAX_ALERTS_PER_RUN} por corrida")
        lines.append(f"🧱 <b>Límite por grupo:</b> {MAX_ALERTS_PER_GROUP}")
    else:
        lines.append("🚦 <b>Ranking activo:</b> no")

    lines.append("")
    if selected:
        lines.append("🏆 <b>Enviadas:</b>")
        for item in selected:
            lines.append(
                f"• {esc(item['symbol'])} | prioridad {item['rank_score']:.2f} | "
                f"grupo {esc(item['asset_group'])} | score {item['score']:.2f}"
            )
    else:
        lines.append("🏆 <b>Enviadas:</b> 0")

    if deferred:
        lines.append("")
        lines.append("⏸️ <b>Diferidas por ranking/diversificación:</b>")
        for item in deferred[:8]:
            lines.append(
                f"• {esc(item['symbol'])} | prioridad {item['rank_score']:.2f} | "
                f"grupo {esc(item['asset_group'])}"
            )

    if blocked:
        lines.append("")
        lines.append("🛡️ <b>Omitidas por deduplicación/cooldown:</b>")
        for text in blocked[:10]:
            lines.append(f"• {esc(text)}")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    start = time.time()
    conn = get_db_connection(DB_FILE)
    init_db(conn)
    import_legacy_state_if_needed(conn)

    print(f"🚀 Iniciando escaneo de {len(CRYPTO_IDS)} activos...")
    sent_count = 0
    total_valid = 0
    ready_candidates: List[Dict[str, Any]] = []
    blocked_messages: List[str] = []

    for cg_id, symbol in CRYPTO_IDS.items():
        price_df = get_hourly_prices(cg_id)
        ohlc_df = build_ohlc_from_hourly(price_df) if price_df is not None else None
        if ohlc_df is None:
            time.sleep(SLEEP_BETWEEN_ASSETS)
            continue

        candidate = evaluate(ohlc_df, symbol, cg_id)
        if not candidate:
            time.sleep(SLEEP_BETWEEN_ASSETS)
            continue

        invalidate_old_alerts(conn, candidate)

        if not candidate["alert"]:
            time.sleep(SLEEP_BETWEEN_ASSETS)
            continue

        total_valid += 1
        should_send, improved_from_alert_id, decision_reason = should_send_alert(conn, candidate)
        if should_send:
            candidate["improved_from_alert_id"] = improved_from_alert_id
            candidate["decision_reason"] = decision_reason
            ready_candidates.append(candidate)
        else:
            blocked_message = f"{symbol}: {decision_reason}"
            blocked_messages.append(blocked_message)
            print(f"⏳ {symbol}: omitida. {decision_reason}.")

        time.sleep(SLEEP_BETWEEN_ASSETS)

    ranked_candidates = rank_candidates(ready_candidates) if ready_candidates else []
    if ranked_candidates:
        print("🏅 Ranking interno:")
        for idx, item in enumerate(ranked_candidates, start=1):
            print(
                f"   {idx}. {item['symbol']} | prioridad={item['rank_score']:.2f} | "
                f"grupo={item['asset_group']} | score={item['score']:.2f} | adx={item['adx']:.2f}"
            )

    selected_candidates, deferred_candidates = select_ranked_candidates(ranked_candidates)

    for item in deferred_candidates:
        print(
            f"⏸️ {item['symbol']}: diferida por ranking/diversificación. "
            f"prioridad={item['rank_score']:.2f}, grupo={item['asset_group']}"
        )

    for candidate in selected_candidates:
        sent_ok = send_telegram(format_message(candidate, candidate["decision_reason"]))
        if sent_ok:
            save_alert(conn, candidate, candidate.get("improved_from_alert_id"))
            sent_count += 1
        else:
            print(f"⚠️ {candidate['symbol']}: alerta no guardada porque Telegram no confirmó el envío.")

    if SEND_RUN_SUMMARY and (selected_candidates or deferred_candidates or blocked_messages):
        summary_sent = send_telegram(
            format_run_summary(
                selected_candidates,
                deferred_candidates,
                blocked_messages,
                total_valid,
            )
        )
        if not summary_sent:
            print("⚠️ No se pudo enviar el resumen de ejecución.")

    duration = round(time.time() - start, 1)
    print(f"🏁 Fin del escaneo. Alertas enviadas: {sent_count}. Duración: {duration}s")
    conn.close()


if __name__ == "__main__":
    main()
