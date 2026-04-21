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
MARKET_CONTEXT_FILE = os.getenv("MARKET_CONTEXT_FILE", "market_context.json")
VS_CURRENCY = os.getenv("VS_CURRENCY", "usd")

DAILY_LOOKBACK_DAYS = int(os.getenv("DAILY_LOOKBACK_DAYS", "365"))
HOURLY_LOOKBACK_DAYS = int(os.getenv("HOURLY_LOOKBACK_DAYS", "90"))
INTRADAY_LOOKBACK_DAYS = int(os.getenv("INTRADAY_LOOKBACK_DAYS", "1"))
HOURLY_INTERVAL = os.getenv("HOURLY_INTERVAL", "hourly")

MACRO_TIMEFRAME = os.getenv("MACRO_TIMEFRAME", "1D")
TRADING_TIMEFRAME = os.getenv("TRADING_TIMEFRAME", "4h")
ENTRY_TIMEFRAME = os.getenv("ENTRY_TIMEFRAME", "15min")

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


# ── Contexto manual ───────────────────────────────────────────────────────────
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


def fetch_btc_dominance() -> Optional[float]:
    """Obtiene dominancia de BTC desde /global. Alta (>58%) penaliza altcoins,
    baja (<44%) les da viento de cola por rotación de capital."""
    url = "https://api.coingecko.com/api/v3/global"
    headers = {"accept": "application/json"}
    if CG_API_KEY:
        headers["x-cg-demo-api-key"] = CG_API_KEY
    try:
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            pct = r.json().get("data", {}).get("market_cap_percentage", {}).get("btc")
            return round(float(pct), 1) if pct else None
    except Exception:
        pass
    return None


def normalize_context(context: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    global_ctx = context.get("GLOBAL", {})
    asset_ctx = context.get(symbol, {})
    if isinstance(global_ctx, dict):
        merged.update(global_ctx)
    if isinstance(asset_ctx, dict):
        merged.update(asset_ctx)
    return merged


# ── Datos de mercado ──────────────────────────────────────────────────────────
def get_market_prices(cg_id: str, days: int, interval: Optional[str] = None) -> Optional[pd.DataFrame]:
    url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart"
    headers = {"accept": "application/json"}
    if CG_API_KEY:
        headers["x-cg-demo-api-key"] = CG_API_KEY

    params = {
        "vs_currency": VS_CURRENCY,
        "days": str(days),
        "precision": "full",
    }
    if interval:
        params["interval"] = interval

    try:
        response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"⚠️ {cg_id}: CoinGecko respondió {response.status_code}: {response.text[:180]}")
            return None

        payload = response.json()
        prices = payload.get("prices", []) if isinstance(payload, dict) else []
        if len(prices) < 50:
            print(f"⚠️ {cg_id}: datos insuficientes ({len(prices)} puntos).")
            return None

        df = pd.DataFrame(prices, columns=["ts", "price"])
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.dropna().sort_values("ts").drop_duplicates(subset=["ts"]).reset_index(drop=True)
        return df
    except Exception as exc:
        print(f"❌ Error obteniendo datos para {cg_id}: {exc}")
        return None


def build_ohlc_from_prices(price_df: pd.DataFrame, timeframe: str, min_candles: int) -> Optional[pd.DataFrame]:
    if price_df is None or price_df.empty:
        return None

    df = price_df.copy().set_index("ts")
    ohlc = df["price"].resample(timeframe, label="right", closed="right").ohlc()
    ohlc.columns = ["Open", "High", "Low", "Close"]
    ohlc = ohlc.dropna().reset_index()

    if len(ohlc) <= min_candles:
        print(f"⚠️ OHLC insuficiente ({len(ohlc)} velas {timeframe}).")
        return None

    # Solo velas cerradas.
    closed = ohlc.iloc[:-1].reset_index(drop=True)
    if len(closed) < min_candles:
        print(f"⚠️ Velas cerradas insuficientes ({len(closed)} velas {timeframe}).")
        return None
    return closed


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
    recent = df.tail(lookback).copy()
    typical_price = (recent["High"] + recent["Low"] + recent["Close"]) / 3
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
    Usa volumen real (USD) si la columna Volume está disponible y tiene
    cobertura suficiente. Cae al proxy de rango de vela si no.
    Indica la fuente usada para transparencia en la alerta.
    """
    recent = df.tail(lookback).copy()

    has_real_volume = (
        "Volume" in recent.columns
        and recent["Volume"].notna().sum() >= max(4, len(recent) // 2)
    )

    if has_real_volume:
        vol_series = recent["Volume"].fillna(0.0)
        avg_vol = float(vol_series.mean())
        last_3_avg = float(vol_series.tail(3).mean())
        volume_source = "real"
    else:
        vol_series = recent["High"] - recent["Low"]
        avg_vol = float(vol_series.mean())
        last_3_avg = float(vol_series.tail(3).mean())
        volume_source = "proxy"

    relative_volume = last_3_avg / max(avg_vol, 1e-9)

    # Requiere al menos 4 velas para comparación de precio válida
    if len(recent) >= 4:
        price_up = float(recent.iloc[-1]["Close"]) > float(recent.iloc[-4]["Close"])
    else:
        price_up = False

    vol_declining = last_3_avg < avg_vol * 0.85

    return {
        "relative_volume": round(relative_volume, 2),
        "divergence": price_up and vol_declining,
        "strong_momentum": relative_volume >= 1.15,
        "volume_source": volume_source,
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



def bool_icon(flag: bool) -> str:
    return "✅" if flag else "❌"


def context_float(context: Dict[str, Any], key: str, default: float) -> float:
    raw = context.get(key, default)
    try:
        value = float(raw)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


# ── Confirmación 1D ───────────────────────────────────────────────────────────
def evaluate_macro_confirmation(daily_df: pd.DataFrame, symbol: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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

    reasons: List[str] = []
    adjustments = {
        "score": 0.0,
        "rank": 0.0,
    }

    allowed_sides = [str(side).upper() for side in context.get("allowed_sides", [SIDE_LONG])]
    hard_block_long = bool(context.get("hard_block_long", False))
    caution_level = str(context.get("caution_level", "NORMAL")).upper()
    macro_regime = str(context.get("macro_regime", "UNSPECIFIED")).upper()
    macro_bias = str(context.get("macro_bias", "UNSPECIFIED")).upper()
    short_term_bias = str(context.get("short_term_bias", "UNSPECIFIED")).upper()
    long_resistance_near = bool(context.get("long_resistance_near", False))
    long_resistance_label = str(context.get("long_resistance_label", "")).strip()
    note = str(context.get("note", "")).strip()
    fast_exit_mode = bool(context.get("fast_exit_mode", False))

    tp1_rr = context_float(context, "tp1_rr", 1.0)
    tp2_rr = context_float(context, "tp2_rr", 1.8)
    max_rr_long = context_float(context, "max_rr_long", tp2_rr)
    move_to_be_rr = context_float(context, "move_to_be_rr", min(tp1_rr, 0.9))
    risk_multiplier = context_float(context, "risk_multiplier", 1.0)
    min_structural_room_rr = context_float(context, "min_structural_room_rr", 1.10)
    reject_if_distance_to_resistance_pct_below = context_float(
        context, "reject_if_distance_to_resistance_pct_below", 0.0
    )

    require_breakout_raw = context.get("require_breakout_above")
    try:
        require_breakout_above = float(require_breakout_raw) if require_breakout_raw is not None else None
    except (TypeError, ValueError):
        require_breakout_above = None

    technical_ok = (
        close > ema20
        and ema20 >= ema50
        and rsi >= 45
        and rsi <= 70
        and (plus_di >= minus_di or adx < 18)
    )

    if regime == "BULL_STACK":
        reasons.append("Diario con estructura alcista")
    elif technical_ok:
        reasons.append("Diario permite longs, aunque no está en pila alcista completa")
    else:
        reasons.append("Diario no confirma longs con claridad")

    if hard_block_long or SIDE_LONG not in allowed_sides:
        macro_ok = False
        reasons.append("Bloqueo manual del lado long")
    else:
        macro_ok = technical_ok

    if require_breakout_above is not None and close < require_breakout_above:
        macro_ok = False
        reasons.append(f"Se exige ruptura/aceptación sobre {require_breakout_above:,.0f} antes de longs")

    if long_resistance_near:
        reasons.append(f"Resistencia macro cerca: {long_resistance_label or 'nivel mayor'}")
    if fast_exit_mode:
        reasons.append("Gestión táctica: salida rápida")
    if note:
        reasons.append(note)

    adjustments["score"] += context_float(context, "long_score_adjustment", 0.0)
    adjustments["rank"] += context_float(context, "long_rank_adjustment", 0.0)

    caution_penalty_map = {
        "LOW": 0.0,
        "NORMAL": 0.0,
        "MEDIUM": -0.25,
        "HIGH": -0.6,
        "EXTREME": -1.0,
    }
    adjustments["score"] += caution_penalty_map.get(caution_level, 0.0)
    if long_resistance_near:
        adjustments["rank"] -= 2.0

    btc_dominance = context.get("btc_dominance")
    dominance_note = ""
    if btc_dominance is not None and symbol != "BTC":
        if btc_dominance >= 58:
            adjustments["score"] -= 0.5
            adjustments["rank"] -= 2.0
            dominance_note = f"BTC dominance alta ({btc_dominance:.1f}%) — altcoins rezagadas"
            reasons.append(dominance_note)
        elif btc_dominance <= 44:
            adjustments["score"] += 0.3
            dominance_note = f"BTC dominance baja ({btc_dominance:.1f}%) — rotación a altcoins"
            reasons.append(dominance_note)

    return {
        "ok": macro_ok,
        "btc_dominance": btc_dominance,
        "dominance_note": dominance_note,
        "regime": regime,
        "rsi": round(rsi, 2),
        "adx": round(adx, 2),
        "close": close,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "technical_ok": technical_ok,
        "macro_regime": macro_regime,
        "macro_bias": macro_bias,
        "short_term_bias": short_term_bias,
        "caution_level": caution_level,
        "long_resistance_near": long_resistance_near,
        "long_resistance_label": long_resistance_label,
        "short_support_label": str(context.get("short_support_label", "")).strip(),
        "fast_exit_mode": fast_exit_mode,
        "note": note,
        "tp1_rr": round(max(tp1_rr, 0.6), 2),
        "tp2_rr": round(max(tp2_rr, max(tp1_rr + 0.2, 0.9)), 2),
        "max_rr_long": round(max(max_rr_long, 0.9), 2),
        "move_to_be_rr": round(max(min(move_to_be_rr, tp1_rr), 0.5), 2),
        "risk_multiplier": round(max(risk_multiplier, 0.1), 2),
        "min_structural_room_rr": round(max(min_structural_room_rr, 0.8), 2),
        "reject_if_distance_to_resistance_pct_below": round(
            max(reject_if_distance_to_resistance_pct_below, 0.0), 2
        ),
        "require_breakout_above": require_breakout_above,
        "score_adjustment": round(adjustments["score"], 2),
        "rank_adjustment": round(adjustments["rank"], 2),
        "reasons": reasons,
    }


# ── Confirmación 4H ───────────────────────────────────────────────────────────

def evaluate_setup_confirmation(fourh_df: pd.DataFrame, symbol: str, cg_id: str) -> Optional[Dict[str, Any]]:
    work = add_indicators(fourh_df)
    if len(work) < 210:
        print(f"⚠️ {symbol}: histórico 4h insuficiente ({len(work)} velas útiles).")
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
    elif close > ema50 > ema200 and ema20 >= ema50 * 0.997:
        score += 1.5
        reasons.append("Estructura 4H casi en pila alcista")
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
    elif 44 <= rsi <= 69:
        score += 0.5
        reasons.append(f"RSI aceptable ({rsi:.1f})")
    if adx >= 22 and plus_di > minus_di:
        score += 1.25
        reasons.append(f"ADX con dirección ({adx:.1f})")
    elif adx >= 16 and plus_di >= minus_di * 0.97:
        score += 0.65
        reasons.append(f"ADX utilizable ({adx:.1f})")

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

    vwap_data = compute_vwap_proximity(work, lookback=20)
    vwap_dist = vwap_data["distance_pct"]
    if vwap_data["above_vwap"] and vwap_dist <= 1.5:
        score += 1.0
        reasons.append(f"Precio cerca del VWAP (+{vwap_dist:.1f}%)")
    elif vwap_data["above_vwap"] and vwap_dist <= 3.5:
        score += 0.4
        reasons.append(f"Precio sobre VWAP (+{vwap_dist:.1f}%)")
    elif vwap_data["above_vwap"] and vwap_dist > 3.5:
        score -= 1.0
        reasons.append(f"Precio sobreextendido sobre VWAP (+{vwap_dist:.1f}%)")
    elif not vwap_data["above_vwap"]:
        score -= 0.5
        reasons.append(f"Precio bajo VWAP ({vwap_dist:.1f}%)")

    vol_data = compute_volume_momentum(work, lookback=10)
    if vol_data["divergence"]:
        score -= 1.5
        reasons.append("Divergencia volumen: precio sube, momentum cae")
    elif vol_data["strong_momentum"]:
        score += 0.75
        reasons.append("Momentum de volumen fuerte")

    distance_to_swing_high_pct = ((fib["swing_high"] - close) / max(close, 1e-9)) * 100
    if distance_to_swing_high_pct <= 0.8:
        score -= 1.25
        reasons.append(f"Precio a {distance_to_swing_high_pct:.2f}% del swing high")
    elif distance_to_swing_high_pct <= 1.5:
        score -= 0.60
        reasons.append(f"Precio cerca de resistencia ({distance_to_swing_high_pct:.2f}% del swing high)")

    atr_stop = close - (atr * 1.8)
    structure_stop = fib["swing_low"]
    stop_loss = atr_stop if (close - structure_stop) > atr * 4.0 else max(structure_stop, atr_stop)
    if stop_loss >= close:
        stop_loss = close - max(atr * 1.5, close * 0.01)

    risk = max(close - stop_loss, 1e-9)

    amplitude = max(fib["swing_high"] - fib["swing_low"], 1e-9)
    buffer = max(atr * 0.25, close * 0.001)
    tp_extension_114 = fib["swing_high"] + amplitude * 0.141 - buffer
    tp_extension_127 = fib["swing_high"] + amplitude * 0.272 - buffer

    if adx >= 26:
        tp1_rr = 1.10
        tp2_rr = 2.20
    elif adx >= 21:
        tp1_rr = 1.00
        tp2_rr = 1.80
    else:
        tp1_rr = 0.90
        tp2_rr = 1.40

    if distance_to_swing_high_pct <= 1.0:
        tp2_rr = min(tp2_rr, 1.40)
    elif distance_to_swing_high_pct <= 1.8:
        tp2_rr = min(tp2_rr, 1.80)

    tp1 = close + risk * tp1_rr
    tp2_rr_price = close + risk * tp2_rr
    tp2_fib_price = tp_extension_114 if distance_to_swing_high_pct <= 1.0 else tp_extension_127
    tp2 = min(tp2_rr_price, tp2_fib_price) if tp2_fib_price > close else tp2_rr_price
    tp2 = max(tp2, tp1 + risk * 0.20)

    take_profit = tp2
    rr_ratio = (take_profit - close) / risk

    setup_score_floor = max(MIN_SCORE - 0.5, 5.25)
    trend_ok = regime == "BULL_STACK" or (close > ema50 > ema200 and ema20 >= ema50 * 0.997)
    direction_ok = plus_di >= minus_di * 0.97
    strength_ok = adx >= 16
    setup_ok = (
        trend_ok
        and close >= ema200 * 0.995
        and direction_ok
        and strength_ok
        and rr_ratio >= 1.05
        and score >= setup_score_floor
    )

    return {
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
        "tp1": float(tp1),
        "tp2": float(tp2),
        "tp1_rr": round(tp1_rr, 2),
        "tp2_rr": round((tp2 - close) / risk, 2),
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
        "distance_to_swing_high_pct": round(distance_to_swing_high_pct, 2),
        "setup_ok": setup_ok,
        "setup_score_floor": round(setup_score_floor, 2),
    }

# ── Confirmación 15m ──────────────────────────────────────────────────────────

def evaluate_timing_confirmation(entry_df: pd.DataFrame, symbol: str) -> Optional[Dict[str, Any]]:
    work = add_indicators(entry_df)
    if len(work) < 60:
        print(f"⚠️ {symbol}: histórico 15m insuficiente ({len(work)} velas útiles).")
        return None

    last = work.iloc[-1]
    close = float(last["Close"])
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])
    rsi = float(last["rsi"])
    atr = float(last["atr"])

    vwap_data = compute_vwap_proximity(work, lookback=16)
    vol_data = compute_volume_momentum(work, lookback=12)
    local_high = float(work.tail(24)["High"].max())
    distance_to_local_high_pct = ((local_high - close) / max(close, 1e-9)) * 100

    reasons: List[str] = []
    score_adjustment = 0.0
    rank_adjustment = 0.0
    points = 0.0
    hard_fail = False

    trend_gap_pct = ((ema20 - ema50) / max(ema50, 1e-9)) * 100

    if ema20 >= ema50:
        reasons.append("15m acompaña la dirección")
        points += 1.2
    elif close > ema50 and trend_gap_pct >= -0.35:
        reasons.append("15m casi alineado; pullback controlado")
        points += 0.6
        score_adjustment -= 0.10
    else:
        reasons.append("15m pierde estructura inmediata")
        hard_fail = True

    if close >= ema20 * 0.996:
        reasons.append("Precio ejecutable respecto a EMA20 15m")
        points += 1.2
    elif close >= ema20 * 0.992:
        reasons.append("Precio ligeramente bajo EMA20 15m")
        points += 0.4
        score_adjustment -= 0.15
    else:
        reasons.append("Precio demasiado débil contra EMA20 15m")
        hard_fail = True

    if 40 <= rsi <= 72:
        reasons.append(f"RSI 15m razonable ({rsi:.1f})")
        points += 0.9
    elif 37 <= rsi <= 75:
        reasons.append(f"RSI 15m usable ({rsi:.1f})")
        points += 0.3
    else:
        reasons.append(f"RSI 15m incómodo ({rsi:.1f})")
        hard_fail = True

    if vol_data["divergence"]:
        reasons.append("Divergencia de momentum en 15m")
        score_adjustment -= 0.35
        rank_adjustment -= 1.0
    elif vol_data["strong_momentum"]:
        reasons.append("Momentum de entrada 15m fuerte")
        score_adjustment += 0.25
        points += 0.8

    if vwap_data["above_vwap"]:
        if abs(vwap_data["distance_pct"]) <= 3.2:
            reasons.append(f"Precio aceptable vs VWAP 15m ({vwap_data['distance_pct']:+.1f}%)")
            points += 0.8
        elif vwap_data["distance_pct"] <= 4.5:
            reasons.append(f"Entrada algo estirada sobre VWAP 15m ({vwap_data['distance_pct']:+.1f}%)")
            score_adjustment -= 0.20
        else:
            reasons.append(f"Entrada demasiado extendida sobre VWAP 15m ({vwap_data['distance_pct']:+.1f}%)")
            hard_fail = True
    else:
        if vwap_data["distance_pct"] >= -1.2:
            reasons.append(f"Ligero descuento bajo VWAP 15m ({vwap_data['distance_pct']:+.1f}%)")
            points += 0.25
        elif vwap_data["distance_pct"] >= -2.5:
            reasons.append(f"Bajo VWAP 15m, requiere rebote ({vwap_data['distance_pct']:+.1f}%)")
            score_adjustment -= 0.15
        else:
            reasons.append(f"Demasiado bajo VWAP 15m ({vwap_data['distance_pct']:+.1f}%)")
            hard_fail = True

    if distance_to_local_high_pct <= 0.20:
        reasons.append(f"Pegado a micro-resistencia ({distance_to_local_high_pct:.2f}% del máximo local)")
        score_adjustment -= 0.35
        rank_adjustment -= 0.8
        hard_fail = True
    elif distance_to_local_high_pct <= 0.60:
        reasons.append(f"Cerca del máximo local ({distance_to_local_high_pct:.2f}%)")
        score_adjustment -= 0.10
        rank_adjustment -= 0.4
    elif distance_to_local_high_pct <= 1.20:
        reasons.append(f"Aún con espacio táctico antes del máximo local ({distance_to_local_high_pct:.2f}%)")
        points += 0.25

    timing_ok = (not hard_fail) and points >= 2.4

    return {
        "ok": timing_ok,
        "points": round(points, 2),
        "rsi": round(rsi, 2),
        "ema20": ema20,
        "ema50": ema50,
        "atr": atr,
        "vwap": vwap_data["vwap"],
        "vwap_distance_pct": vwap_data["distance_pct"],
        "volume_divergence": vol_data["divergence"],
        "volume_strong": vol_data["strong_momentum"],
        "distance_to_local_high_pct": round(distance_to_local_high_pct, 2),
        "score_adjustment": round(score_adjustment, 2),
        "rank_adjustment": round(rank_adjustment, 2),
        "reasons": reasons,
    }


def apply_context_execution_policy(candidate: Dict[str, Any], macro_eval: Dict[str, Any]) -> Dict[str, Any]:
    risk = max(candidate["entry_price"] - candidate["stop_loss"], 1e-9)

    policy_tp1_rr = float(macro_eval.get("tp1_rr", candidate.get("tp1_rr", 1.0)))
    policy_tp2_rr = float(macro_eval.get("tp2_rr", candidate.get("tp2_rr", 1.8)))
    policy_max_rr = float(macro_eval.get("max_rr_long", policy_tp2_rr))
    min_structural_room_rr = float(macro_eval.get("min_structural_room_rr", 1.10))
    reject_distance_pct = float(macro_eval.get("reject_if_distance_to_resistance_pct_below", 0.0))

    base_tp1_rr = float(candidate.get("tp1_rr", 1.0))
    base_tp2_rr = float(candidate.get("tp2_rr", candidate.get("rr_ratio", 1.8)))

    final_tp1_rr = min(base_tp1_rr, policy_tp1_rr)
    final_tp2_rr = min(base_tp2_rr, policy_tp2_rr, policy_max_rr)
    final_tp1_rr = max(min(final_tp1_rr, final_tp2_rr - 0.15), 0.60)
    final_tp2_rr = max(final_tp2_rr, final_tp1_rr + 0.15)

    distance_to_resistance_pct = float(candidate.get("distance_to_swing_high_pct", 99.0))
    if reject_distance_pct > 0 and distance_to_resistance_pct <= reject_distance_pct:
        candidate["setup_ok"] = False
        candidate["reasons"].append(
            f"Policy: resistencia demasiado cerca ({distance_to_resistance_pct:.2f}% <= {reject_distance_pct:.2f}%)"
        )

    candidate["tp1_rr"] = round(final_tp1_rr, 2)
    candidate["tp2_rr"] = round(final_tp2_rr, 2)
    candidate["tp1"] = float(candidate["entry_price"] + risk * final_tp1_rr)
    candidate["tp2"] = float(candidate["entry_price"] + risk * final_tp2_rr)
    candidate["take_profit"] = candidate["tp2"]
    candidate["rr_ratio"] = round((candidate["take_profit"] - candidate["entry_price"]) / risk, 2)
    candidate["move_to_be_rr"] = round(min(float(macro_eval.get("move_to_be_rr", final_tp1_rr)), final_tp1_rr), 2)
    candidate["breakeven_trigger"] = float(candidate["entry_price"] + risk * candidate["move_to_be_rr"])
    candidate["risk_multiplier"] = round(float(macro_eval.get("risk_multiplier", 1.0)), 2)

    if candidate["rr_ratio"] < min_structural_room_rr:
        candidate["setup_ok"] = False
        candidate["reasons"].append(
            f"Policy: espacio estructural insuficiente tras cap de target ({candidate['rr_ratio']:.2f}R)"
        )

    if macro_eval.get("fast_exit_mode"):
        candidate["reasons"].append("Policy: usar parcial en TP1 y mover a BE rápido")

    return candidate


# ── Integración multi-timeframe ───────────────────────────────────────────────

def build_candidate(
    symbol: str,
    cg_id: str,
    macro_eval: Dict[str, Any],
    setup_eval: Dict[str, Any],
    timing_eval: Dict[str, Any],
) -> Dict[str, Any]:
    candidate = dict(setup_eval)

    score_adjustment = macro_eval["score_adjustment"] + timing_eval["score_adjustment"]
    candidate["score"] = round(candidate["score"] + score_adjustment, 2)
    candidate["rank_adjustment"] = round(macro_eval["rank_adjustment"] + timing_eval["rank_adjustment"], 2)

    setup_score_floor = float(setup_eval.get("setup_score_floor", max(MIN_SCORE - 0.5, 5.25)))
    candidate["macro_ok"] = macro_eval["ok"]
    candidate["timing_ok"] = timing_eval["ok"]
    candidate["setup_ok"] = setup_eval["setup_ok"] and candidate["score"] >= setup_score_floor
    candidate["macro"] = macro_eval
    candidate["timing"] = timing_eval

    combined_reasons = list(setup_eval["reasons"])
    combined_reasons.extend([f"1D: {text}" for text in macro_eval["reasons"]])
    combined_reasons.extend([f"15m: {text}" for text in timing_eval["reasons"]])
    candidate["reasons"] = combined_reasons

    candidate = apply_context_execution_policy(candidate, macro_eval)
    candidate["confirmations_passed"] = int(candidate["macro_ok"]) + int(candidate["setup_ok"]) + int(candidate["timing_ok"])
    candidate["alert"] = candidate["confirmations_passed"] == 3
    candidate["setup_key"] = build_setup_key(candidate)
    candidate["setup_hash"] = build_setup_hash(candidate["setup_key"])

    print(
        f"🔍 {symbol}: 1D={bool_icon(candidate['macro_ok'])}, "
        f"4H={bool_icon(candidate['setup_ok'])}, "
        f"15m={bool_icon(candidate['timing_ok'])}, "
        f"score={candidate['score']:.2f}, rr={candidate['rr_ratio']:.2f}, alert={candidate['alert']}"
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
        if not candidate.get("macro_ok", True):
            reason = "Confirmación macro perdida"
        elif not candidate.get("timing_ok", True):
            reason = "Timing de entrada perdido"
        elif candidate["regime"] != "BULL_STACK":
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

    if candidate.get("distance_to_swing_high_pct", 9.0) <= 0.8:
        rank -= 1.2
        notes.append("cerca swing high")

    rank += float(candidate.get("rank_adjustment", 0.0))
    if candidate.get("rank_adjustment", 0.0):
        notes.append(f"ajuste macro/timing {candidate['rank_adjustment']:+.2f}")

    if candidate.get("confirmations_passed", 0) < 3:
        rank -= 50.0
        notes.append("sin 3/3 confirmaciones")

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



def build_human_signal_summary(candidate: Dict[str, Any]) -> Dict[str, str]:
    macro_eval = candidate.get("macro", {})
    caution_level = str(macro_eval.get("caution_level", "NORMAL")).upper()
    resistance_near = bool(macro_eval.get("long_resistance_near", False))
    fast_exit_mode = bool(macro_eval.get("fast_exit_mode", False))
    timing_points = float(candidate.get("timing", {}).get("points", 0.0))
    final_score = float(candidate.get("score", 0.0))
    adx = float(candidate.get("adx", 0.0))

    if candidate.get("macro_ok") and candidate.get("setup_ok") and candidate.get("timing_ok"):
        if final_score >= 8.0 and timing_points >= 4.0 and adx >= 25 and not resistance_near and caution_level not in {"HIGH", "EXTREME"}:
            label = "COMPRA FUERTE"
            reading = "El activo mantiene contexto favorable, setup sólido y timing aceptable."
            recommendation = "Se puede considerar entrada, respetando stop y sin perseguir demasiado el precio."
        elif final_score >= 6.8:
            label = "COMPRA VÁLIDA"
            reading = "La señal de compra está confirmada y el contexto técnico acompaña."
            recommendation = "Entrada posible, idealmente sin perseguir demasiado el precio y respetando el stop."
        else:
            label = "COMPRA CON CAUTELA"
            reading = "Hay señal de compra, pero con advertencias que justifican una ejecución prudente."
            recommendation = "Se puede operar con cautela; mejor si confirma continuidad o aparece un pullback corto."
    elif candidate.get("macro_ok") and candidate.get("setup_ok") and not candidate.get("timing_ok"):
        label = "SEÑAL DE COMPRA, PERO DÉBIL"
        reading = "El contexto y el setup acompañan, pero el momento de entrada aún no convence."
        recommendation = "No entrar todavía. Esperar que el 15m confirme mejor o que el precio mejore la entrada."
    elif not candidate.get("macro_ok") and candidate.get("setup_ok"):
        label = "SETUP ALCISTA, PERO CONTRA MACRO"
        reading = "La estructura operativa existe, pero el contexto diario no acompaña."
        recommendation = "Solo vigilar. Evitar longs agresivos mientras el diario no mejore."
    elif candidate.get("macro_ok") and not candidate.get("setup_ok"):
        label = "AÚN NO HAY SETUP OPERABLE"
        reading = "El contexto permite longs, pero el 4H todavía no confirma una oportunidad limpia."
        recommendation = "Esperar a que el 4H reconstruya mejor la estructura antes de operar."
    else:
        label = "DESCARTAR"
        reading = "La señal no tiene suficiente respaldo entre contexto, setup y timing."
        recommendation = "No operar este activo por ahora."

    if not candidate.get("timing_ok"):
        main_risk = "El timing de entrada sigue flojo y podrías entrar tarde o con poco impulso."
    elif not candidate.get("macro_ok"):
        main_risk = "El contexto diario no acompaña y el precio puede rechazar aunque el 4H luzca bien."
    elif resistance_near:
        main_risk = "Hay resistencia macro cerca y el precio podría frenarse antes de desarrollar el movimiento."
    elif candidate.get("volume_divergence"):
        main_risk = "El impulso muestra divergencia y aumenta la probabilidad de retroceso."
    elif float(candidate.get("distance_to_swing_high_pct", 9.0)) <= 0.8:
        main_risk = "El precio está muy cerca del swing high reciente y puede reaccionar allí."
    elif fast_exit_mode:
        main_risk = "El escenario exige gestión táctica; la posición no debería darse mucho espacio."
    else:
        main_risk = "El riesgo técnico parece controlado mientras respete el stop."

    return {
        "label": label,
        "reading": reading,
        "main_risk": main_risk,
        "recommendation": recommendation,
    }



def sort_watch_candidates(watch_candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ranked = []
    for candidate in watch_candidates:
        clone = dict(candidate)
        clone["human_summary"] = build_human_signal_summary(clone)
        clone["watch_priority"] = round(
            float(clone.get("setup_score", clone.get("score", 0.0))) * 2.0
            + float(clone.get("score", 0.0))
            + (1.0 if clone.get("macro_ok") else 0.0)
            + (0.5 if clone.get("timing_ok") else 0.0)
            + min(float(clone.get("adx", 0.0)), 30.0) * 0.05,
            2,
        )
        ranked.append(clone)

    ranked.sort(
        key=lambda item: (
            item.get("watch_priority", 0.0),
            item.get("setup_score", item.get("score", 0.0)),
            item.get("score", 0.0),
            item.get("adx", 0.0),
        ),
        reverse=True,
    )
    return ranked


# ── Formato de mensajes ───────────────────────────────────────────────────────
def format_message(candidate: Dict[str, Any], decision_reason: str) -> str:
    esc = html.escape
    human = build_human_signal_summary(candidate)
    reasons_text = esc(", ".join(candidate["reasons"]))
    rank_line = ""
    if ENABLE_RANKING:
        rank_line = (
            f"🏅 <b>Prioridad:</b> {candidate['rank_score']:.2f}"
            f" | Grupo: {esc(candidate['asset_group'])}\n"
        )

    vwap_dist = candidate.get("vwap_distance_pct", 0.0)
    vwap_icon = "🟢" if candidate.get("above_vwap") and abs(vwap_dist) <= 1.5 else ("🟡" if abs(vwap_dist) <= 3.5 else "🔴")
    vwap_label = f"{'+' if vwap_dist >= 0 else ''}{vwap_dist:.1f}% vs VWAP ${candidate.get('vwap', 0):.4f}"

    if candidate.get("volume_divergence"):
        vol_label = "⚠️ Divergencia (sube precio, cae momentum)"
    elif candidate.get("volume_strong"):
        vol_label = "✅ Momentum fuerte"
    else:
        vol_label = "➖ Momentum neutral"

    macro_eval = candidate.get("macro", {})
    timing_eval = candidate.get("timing", {})
    macro_line = (
        f"🌐 <b>Macro 1D:</b> {bool_icon(candidate['macro_ok'])} | "
        f"{esc(macro_eval.get('macro_regime', 'UNSPECIFIED'))} | "
        f"sesgo {esc(macro_eval.get('macro_bias', 'UNSPECIFIED'))}\n"
    )
    timing_line = (
        f"🎯 <b>Timing 15m:</b> {bool_icon(candidate['timing_ok'])} | "
        f"RSI {timing_eval.get('rsi', 0):.2f} | "
        f"VWAP {timing_eval.get('vwap_distance_pct', 0):+.1f}%\n"
    )
    setup_line = f"⚙️ <b>Setup 4H:</b> {bool_icon(candidate['setup_ok'])} | confirmaciones {candidate['confirmations_passed']}/3\n"

    caution_line = ""
    if macro_eval.get("caution_level"):
        caution_line += f"⚠️ <b>Cautela:</b> {esc(str(macro_eval['caution_level']))}\n"
    if macro_eval.get("long_resistance_label"):
        caution_line += f"🚧 <b>Resistencia mayor:</b> {esc(macro_eval['long_resistance_label'])}\n"
    if macro_eval.get("fast_exit_mode"):
        caution_line += "🏃 <b>Gestión:</b> salida rápida\n"
    if macro_eval.get("note"):
        caution_line += f"📝 <b>Nota macro:</b> {esc(macro_eval['note'])}\n"

    risk_multiplier = float(candidate.get("risk_multiplier", 1.0))
    execution_line = (
        f"🧪 <b>Plan de salida:</b> TP1 ${candidate.get('tp1', 0):.4f} ({candidate.get('tp1_rr', 0):.2f}R) | "
        f"TP2 ${candidate.get('tp2', candidate['take_profit']):.4f} ({candidate.get('tp2_rr', candidate['rr_ratio']):.2f}R)\n"
        f"🛟 <b>Breakeven:</b> mover SL al tocar ${candidate.get('breakeven_trigger', 0):.4f} "
        f"({candidate.get('move_to_be_rr', 0):.2f}R) | tamaño x{risk_multiplier:.2f}\n"
    )

    return (
        f"🚀 <b>ALERTA COMPRA: {esc(candidate['symbol'])}</b>\n"
        f"🗣️ <b>Lectura:</b> {esc(human['label'])}\n\n"
        f"📌 <b>Resumen:</b> {esc(human['reading'])}\n"
        f"⚠️ <b>Riesgo principal:</b> {esc(human['main_risk'])}\n"
        f"✅ <b>Recomendación:</b> {esc(human['recommendation'])}\n\n"
        f"⏱️ <b>Timeframe:</b> {esc(candidate['timeframe'])}\n"
        f"💰 <b>Precio:</b> ${candidate['entry_price']:.4f}\n"
        f"📊 <b>Score:</b> {candidate['score']:.2f}\n"
        f"📈 <b>ADX:</b> {candidate['adx']:.2f}\n"
        f"📉 <b>RSI:</b> {candidate['rsi']:.2f}\n"
        f"🧭 <b>Régimen:</b> {esc(candidate['regime'])}\n"
        f"🧩 <b>Fib:</b> {esc(candidate['fib_zone'])}\n"
        f"⚖️ <b>R:R final:</b> {candidate['rr_ratio']:.2f}\n"
        f"🛑 <b>STOP (SL):</b> ${candidate['stop_loss']:.4f}\n"
        f"{execution_line}"
        f"{vwap_icon} <b>VWAP:</b> {esc(vwap_label)}\n"
        f"📦 <b>Volumen:</b> {esc(vol_label)}\n"
        f"{macro_line}"
        f"{setup_line}"
        f"{timing_line}"
        f"{rank_line}"
        f"{caution_line}\n"
        f"📝 <b>Análisis:</b> {reasons_text}\n"
        f"🧠 <b>Motivo de envío:</b> {esc(decision_reason)}"
    )



def format_run_summary(
    selected: List[Dict[str, Any]],
    deferred: List[Dict[str, Any]],
    blocked: List[str],
    total_ready: int,
    watch_candidates: Optional[List[Dict[str, Any]]] = None,
) -> str:
    esc = html.escape
    lines = ["📋 <b>Resumen de ejecución</b>", ""]
    lines.append(f"✅ <b>Setups con 3/3 confirmaciones:</b> {total_ready}")
    if ENABLE_RANKING:
        lines.append(f"🚦 <b>Ranking activo:</b> sí | Límite: {MAX_ALERTS_PER_RUN} por corrida")
        lines.append(f"🧱 <b>Límite por grupo:</b> {MAX_ALERTS_PER_GROUP}")
    else:
        lines.append("🚦 <b>Ranking activo:</b> no")

    lines.append("")
    if selected:
        lines.append("🏆 <b>Enviadas:</b>")
        for item in selected:
            human = build_human_signal_summary(item)
            lines.append(
                f"• {esc(item['symbol'])} | {esc(human['label'])} | "
                f"prioridad {item['rank_score']:.2f}"
            )
    else:
        lines.append("🏆 <b>Enviadas:</b> 0")

    if deferred:
        lines.append("")
        lines.append("⏸️ <b>Diferidas por ranking/diversificación:</b>")
        for item in deferred[:8]:
            human = build_human_signal_summary(item)
            lines.append(
                f"• {esc(item['symbol'])} | {esc(human['label'])} | "
                f"prioridad {item['rank_score']:.2f} | grupo {esc(item['asset_group'])}"
            )

    if watch_candidates:
        lines.append("")
        lines.append("👀 <b>Vigilancia táctica:</b>")
        for item in watch_candidates[:5]:
            human = item.get("human_summary") or build_human_signal_summary(item)
            lines.append(
                f"• {esc(item['symbol'])} | {esc(human['label'])} | "
                f"{esc(human['recommendation'])}"
            )

    if blocked:
        lines.append("")
        lines.append("🛡️ <b>Descartadas u omitidas:</b>")
        for text in blocked[:12]:
            lines.append(f"• {esc(text)}")

    return "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    start = time.time()
    conn = get_db_connection(DB_FILE)
    init_db(conn)
    import_legacy_state_if_needed(conn)
    market_context = load_market_context(MARKET_CONTEXT_FILE)
    btc_dominance = fetch_btc_dominance()
    if btc_dominance is not None:
        print(f"📊 BTC Dominance: {btc_dominance:.1f}%")
    else:
        print("⚠️ BTC Dominance no disponible — se omite del análisis macro.")

    print(f"🚀 Iniciando escaneo de {len(CRYPTO_IDS)} activos...")
    sent_count = 0
    total_ready = 0
    ready_candidates: List[Dict[str, Any]] = []
    watch_candidates: List[Dict[str, Any]] = []
    blocked_messages: List[str] = []

    for cg_id, symbol in CRYPTO_IDS.items():
        daily_prices = get_market_prices(cg_id, DAILY_LOOKBACK_DAYS, interval=None)
        fourh_prices = get_market_prices(cg_id, HOURLY_LOOKBACK_DAYS, interval=HOURLY_INTERVAL)
        intraday_prices = get_market_prices(cg_id, INTRADAY_LOOKBACK_DAYS, interval=None)

        daily_df = build_ohlc_from_prices(daily_prices, "1D", 220) if daily_prices is not None else None
        fourh_df = build_ohlc_from_prices(fourh_prices, TRADING_TIMEFRAME, 220) if fourh_prices is not None else None
        entry_df = build_ohlc_from_prices(intraday_prices, ENTRY_TIMEFRAME, 60) if intraday_prices is not None else None

        if daily_df is None or fourh_df is None or entry_df is None:
            blocked_messages.append(f"{symbol}: datos insuficientes para 1D/4H/15m")
            time.sleep(SLEEP_BETWEEN_ASSETS)
            continue

        normalized_context = normalize_context(market_context, symbol)
        if btc_dominance is not None:
            normalized_context["btc_dominance"] = btc_dominance
        macro_eval = evaluate_macro_confirmation(daily_df, symbol, normalized_context)
        setup_eval = evaluate_setup_confirmation(fourh_df, symbol, cg_id)
        timing_eval = evaluate_timing_confirmation(entry_df, symbol)

        if not macro_eval or not setup_eval or not timing_eval:
            blocked_messages.append(f"{symbol}: no se pudo evaluar alguna confirmación")
            time.sleep(SLEEP_BETWEEN_ASSETS)
            continue

        candidate = build_candidate(symbol, cg_id, macro_eval, setup_eval, timing_eval)
        invalidate_old_alerts(conn, candidate)

        if not candidate["alert"]:
            reason = f"{symbol}: confirmaciones {candidate['confirmations_passed']}/3"
            blocked_messages.append(reason)
            if candidate.get("macro_ok") or candidate.get("setup_ok"):
                watch_candidates.append(candidate)
            time.sleep(SLEEP_BETWEEN_ASSETS)
            continue

        total_ready += 1
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
    sorted_watch_candidates = sort_watch_candidates(watch_candidates) if watch_candidates else []
    if ranked_candidates:
        print("🏅 Ranking interno:")
        for idx, item in enumerate(ranked_candidates, start=1):
            print(
                f"   {idx}. {item['symbol']} | prioridad={item['rank_score']:.2f} | "
                f"grupo={item['asset_group']} | 3/3 | score={item['score']:.2f}"
            )

    selected_candidates, deferred_candidates = select_ranked_candidates(ranked_candidates)

    for item in deferred_candidates:
        print(
            f"⏸️ {item['symbol']}: diferida por ranking/diversificación. "
            f"prioridad={item['rank_score']:.2f}, grupo={item['asset_group']}"
        )

    if sorted_watch_candidates:
        print("👀 Vigilancia táctica:")
        for item in sorted_watch_candidates[:5]:
            human = item.get("human_summary") or build_human_signal_summary(item)
            print(f"   - {item['symbol']}: {human['label']}")

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
                total_ready,
                sorted_watch_candidates,
            )
        )
        if not summary_sent:
            print("⚠️ No se pudo enviar el resumen de ejecución.")

    duration = round(time.time() - start, 1)
    print(f"🏁 Fin del escaneo. Alertas enviadas: {sent_count}. Duración: {duration}s")
    conn.close()


if __name__ == "__main__":
    main()
