"""
ITERACIÓN 3 — Multi-Timeframe + Score calibrado
Agrega: confirmación en timeframe diario, score reescrito como
        condiciones independientes, mejor gestión de rate limits,
        y modo dry-run para pruebas.
"""
import os
import time
import json
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import yfinance as yf
import requests

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("crypto-alert")

# ── Configuración ─────────────────────────────────────────────────────────────
CRYPTO_SYMBOLS = [
    'BTC-USD', 'ETH-USD', 'BNB-USD', 'SOL-USD', 'XRP-USD',
    'ADA-USD', 'AVAX-USD', 'DOT-USD', 'LINK-USD', 'MATIC-USD', # En Yahoo suele ser MATIC aún
    'LTC-USD', 'NEAR-USD', 'SUI1-USD', 'FET-USD', 'RENDER-USD',
    'TAO1-USD', 'INJ-USD', 'STX1-USD', 'PEPE1-USD', 'SHIB-USD'
]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE         = "alert_state.json"
COOLDOWN           = 14400   # 4 horas

# DRY_RUN=true → imprime alertas pero no las envía (útil en pruebas)
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Thresholds del sistema de score
# Diseñado como condiciones de entrada con pesos diferentes
# Condición MÍNIMA: tendencia horaria + diaria alineadas (3.5 pts)
# Condición IDEAL: todas las señales confirmadas (7.5 pts)
MIN_SCORE_ALERT = 4.5   # Requiere al menos: tendencia macro + diaria + 1 señal más
MIN_RR          = 1.8   # Reducido ligeramente: 1.8x sigue siendo buena relación


# ── Dataclass de señal ────────────────────────────────────────────────────────
@dataclass
class CryptoSignal:
    symbol:       str
    price:        float
    score:        float
    rr:           float
    tp:           float
    stop:         float
    atr:          float
    daily_trend:  str = "neutral"  # "bull", "bear", "neutral"
    reasons:      list = field(default_factory=list)
    blocked_by:   list = field(default_factory=list)  # razones para NO alertar

    @property
    def should_alert(self) -> bool:
        return self.score >= MIN_SCORE_ALERT and self.rr >= MIN_RR and not self.blocked_by


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(msg: str) -> bool:
    if DRY_RUN:
        log.info(f"[DRY RUN] Mensaje:\n{msg}")
        return True
    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN no configurado")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=15
        )
        if r.status_code != 200:
            log.warning(f"Telegram {r.status_code}: {r.text[:80]}")
            return False
        return True
    except Exception as e:
        log.error(f"Telegram: {e}")
        return False


# ── Estado ────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    try:
        p = Path(STATE_FILE)
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception as e:
        log.error(f"Cargando estado: {e}")
        return {}

def save_state(state: dict) -> None:
    try:
        Path(STATE_FILE).write_text(json.dumps(state, indent=2))
    except Exception as e:
        log.error(f"Guardando estado: {e}")


# ── Descarga ──────────────────────────────────────────────────────────────────
def fetch_ohlcv(symbol: str, period: str, interval: str,
                min_bars: int) -> Optional[pd.DataFrame]:
    """Descarga OHLCV y valida. Retorna None si los datos no son suficientes."""
    try:
        df = yf.Ticker(symbol).history(period=period, interval=interval)
    except Exception as e:
        log.warning(f"{symbol} [{interval}]: error en descarga — {e}")
        return None

    required = {"High", "Low", "Close", "Volume"}
    if df is None or df.empty or not required.issubset(df.columns):
        return None

    df = df.dropna(subset=list(required))
    if len(df) < min_bars:
        log.warning(f"{symbol} [{interval}]: {len(df)} barras < {min_bars} requeridas")
        return None

    return df


# ── Indicadores ───────────────────────────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Aplica todos los indicadores sobre el dataframe. Modifica in-place."""

    # EMA
    df['ema20']  = df['Close'].ewm(span=20,  adjust=False).mean()
    df['ema50']  = df['Close'].ewm(span=50,  adjust=False).mean()
    df['ema200'] = df['Close'].ewm(span=200, adjust=False).mean()

    # RSI (Wilder)
    delta = df['Close'].diff()
    gain  = delta.where(delta > 0, 0.0).ewm(com=13, adjust=False).mean()
    loss  = (-delta.where(delta < 0, 0.0)).ewm(com=13, adjust=False).mean()
    df['rsi'] = 100 - (100 / (1 + gain / (loss + 1e-9)))

    # True Range y ATR (Wilder)
    prev_c = df['Close'].shift(1)
    tr     = pd.concat([
        df['High'] - df['Low'],
        (df['High'] - prev_c).abs(),
        (df['Low']  - prev_c).abs()
    ], axis=1).max(axis=1)
    df['atr'] = tr.ewm(com=13, adjust=False).mean()

    # ADX (real)
    up_m   = df['High'].diff()
    dn_m   = (-df['Low'].diff())
    pdm    = up_m.where((up_m > dn_m) & (up_m > 0), 0.0)
    ndm    = dn_m.where((dn_m > up_m) & (dn_m > 0), 0.0)
    pdi    = 100 * pdm.ewm(com=13, adjust=False).mean() / (df['atr'] + 1e-9)
    ndi    = 100 * ndm.ewm(com=13, adjust=False).mean() / (df['atr'] + 1e-9)
    dx     = 100 * (pdi - ndi).abs() / (pdi + ndi + 1e-9)
    df['adx']     = dx.ewm(com=13, adjust=False).mean()
    df['plus_di'] = pdi
    df['minus_di']= ndi

    # MACD
    ema12        = df['Close'].ewm(span=12, adjust=False).mean()
    ema26        = df['Close'].ewm(span=26, adjust=False).mean()
    df['macd']      = ema12 - ema26
    df['macd_sig']  = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_sig']

    # Volumen relativo
    df['vol_ratio'] = df['Volume'] / (df['Volume'].rolling(20).mean() + 1e-9)

    return df


# ── Tendencia diaria (nuevo en iter3) ─────────────────────────────────────────
def get_daily_trend(symbol: str) -> str:
    """
    Retorna 'bull', 'bear' o 'neutral' basado en el timeframe diario.
    Lógica: precio sobre EMA50 diario + ADX > 20 = bull
            precio bajo EMA50 diario + ADX > 20 = bear
            resto = neutral
    """
    df = fetch_ohlcv(symbol, period="200d", interval="1d", min_bars=60)
    if df is None:
        return "neutral"

    df = add_indicators(df)
    last = df.iloc[-1]

    if last['Close'] > last['ema50'] and last['adx'] > 20:
        return "bull"
    if last['Close'] < last['ema50'] and last['adx'] > 20:
        return "bear"
    return "neutral"


# ── Evaluación principal ──────────────────────────────────────────────────────
def evaluate_crypto(symbol: str) -> Optional[CryptoSignal]:
    # Timeframe horario
    df = fetch_ohlcv(symbol, period="60d", interval="1h", min_bars=250)
    if df is None:
        return None

    df = add_indicators(df)
    last = df.iloc[-2]   # Evitar vela en formación
    prev = df.iloc[-3]

    # Tendencia diaria
    daily_trend = get_daily_trend(symbol)

    score, reasons, blocked = 0.0, [], []

    # ── BLOQUEOS DUROS (condiciones que invalidan la señal) ───────────────────

    # Bloquear si la tendencia diaria es bajista (contra-tendencia)
    if daily_trend == "bear":
        blocked.append("Tendencia diaria BAJISTA — señal horaria contra-tendencia")

    # Bloquear si el RSI ya está sobrecomprado
    if last['rsi'] > 75:
        blocked.append(f"RSI sobrecomprado ({last['rsi']:.1f}) — riesgo de reversión")

    # ── SEÑALES DE ENTRADA ────────────────────────────────────────────────────

    # 1. Tendencia macro horaria (EMA200 1h) — peso alto
    if last['Close'] > last['ema200']:
        score += 2.0
        reasons.append("Tendencia alcista horaria (>EMA200)")

    # 2. Alineación con tendencia diaria
    if daily_trend == "bull":
        score += 1.5
        reasons.append("Tendencia diaria alcista confirmada")
    elif daily_trend == "neutral":
        score += 0.5
        reasons.append("Tendencia diaria neutral")

    # 3. ADX horario con dirección
    if last['adx'] > 25 and last['plus_di'] > last['minus_di']:
        score += 1.0
        reasons.append(f"Fuerza horaria ADX={last['adx']:.1f}")

    # 4. RSI con momentum (no sobrecomprado)
    if 40 < last['rsi'] < 65 and last['rsi'] > prev['rsi']:
        score += 1.0
        reasons.append(f"RSI momentum ({last['rsi']:.1f}↑)")

    # 5. Cruce MACD horario
    if last['macd_hist'] > 0 and prev['macd_hist'] <= 0:
        score += 1.0
        reasons.append("Cruce MACD alcista (1h)")

    # 6. Cruce EMA20 o precio sobre EMA20 con confirmación
    if last['Close'] > last['ema20'] and prev['Close'] <= prev['ema20']:
        score += 1.0
        reasons.append("Cruce alcista EMA20 (1h)")

    # 7. Volumen elevado como confirmación (bonus, no bloqueo)
    if last['vol_ratio'] > 1.5:
        score += 0.5
        reasons.append(f"Volumen confirmado ({last['vol_ratio']:.1f}x promedio)")

    # ── Gestión de riesgo ─────────────────────────────────────────────────────
    atr  = max(last['atr'], last['Close'] * 0.01)
    stop = last['Close'] - (atr * 1.5)
    tp   = last['Close'] + (atr * 3.0)
    rr   = (tp - last['Close']) / max(last['Close'] - stop, 1e-9)

    return CryptoSignal(
        symbol      = symbol.replace("-USD", ""),
        price       = last['Close'],
        score       = score,
        rr          = rr,
        tp          = tp,
        stop        = stop,
        atr         = atr,
        daily_trend = daily_trend,
        reasons     = reasons,
        blocked_by  = blocked
    )


# ── Formateo de alerta ────────────────────────────────────────────────────────
def format_alert(sig: CryptoSignal) -> str:
    trend_emoji = {"bull": "📈", "bear": "📉", "neutral": "➡️"}
    score_emoji = "🔥" if sig.score >= 6.0 else "⚡"
    return (
        f"{score_emoji} *ALERTA CRIPTO: {sig.symbol}*\n\n"
        f"💰 *Precio:* ${sig.price:.6g}\n"
        f"📊 *Score:* {sig.score:.1f}/8.0\n"
        f"⚖️ *R:R:* {sig.rr:.2f}x\n"
        f"{trend_emoji.get(sig.daily_trend, '➡️')} *Tendencia diaria:* {sig.daily_trend.upper()}\n\n"
        f"🎯 *TARGET:* ${sig.tp:.6g}\n"
        f"🛑 *STOP:*   ${sig.stop:.6g}\n"
        f"📏 *ATR:*    ${sig.atr:.6g}\n\n"
        f"📝 *Señales activas:*\n" +
        "\n".join(f"  • {r}" for r in sig.reasons)
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    state = load_state()
    now   = time.time()

    mode = "DRY RUN" if DRY_RUN else "PRODUCCIÓN"
    log.info(f"Iniciando escaneo [{mode}] — {len(CRYPTO_SYMBOLS)} activos")

    stats = {k: 0 for k in ("scanned", "cooldown", "no_data", "blocked", "no_signal", "alerts")}

    for symbol in CRYPTO_SYMBOLS:
        last_alert = state.get(symbol, 0)
        remaining  = COOLDOWN - (now - last_alert)
        if remaining > 0:
            log.info(f"COOLDOWN {symbol}: {remaining/3600:.1f}h")
            stats["cooldown"] += 1
            continue

        stats["scanned"] += 1
        try:
            sig = evaluate_crypto(symbol)

            if sig is None:
                stats["no_data"] += 1
                continue

            # Log siempre el resultado
            trend_icon = "📈" if sig.daily_trend == "bull" else ("📉" if sig.daily_trend == "bear" else "➡️")
            log.info(
                f"{sig.symbol}: score={sig.score:.1f} | R:R={sig.rr:.2f} | "
                f"trend={trend_icon}{sig.daily_trend} | "
                f"{'⚠️BLOQUEADO' if sig.blocked_by else ('✅ALERTA' if sig.should_alert else '○sin señal')}"
            )

            if sig.blocked_by:
                stats["blocked"] += 1
                for reason in sig.blocked_by:
                    log.info(f"  └ Bloqueado: {reason}")
            elif sig.should_alert:
                if send_telegram(format_alert(sig)):
                    state[symbol] = now
                    save_state(state)
                    stats["alerts"] += 1
            else:
                stats["no_signal"] += 1

            # Rate limiting más agresivo para evitar bloqueos de Yahoo Finance
            time.sleep(2.0)

        except Exception as e:
            log.error(f"{symbol}: excepción — {e}", exc_info=True)

    log.info(
        f"Fin | Escaneados:{stats['scanned']} | "
        f"Cooldown:{stats['cooldown']} | "
        f"Sin datos:{stats['no_data']} | "
        f"Bloqueados:{stats['blocked']} | "
        f"Sin señal:{stats['no_signal']} | "
        f"Alertas:{stats['alerts']}"
    )


if __name__ == "__main__":
    main()
