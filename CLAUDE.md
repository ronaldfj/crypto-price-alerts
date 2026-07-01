# Crypto Sentinel Bot — CLAUDE.md

## ¿Qué hace este proyecto?

Bot de alertas de trading para crypto. Analiza activos en 3 timeframes (1D, 4H, 15m) y envía alertas vía Telegram cuando detecta setups de alta probabilidad (LONG o SHORT). Persiste estado en SQLite y soporta backtesting histórico.

## Archivos principales

| Archivo | Rol |
|---------|-----|
| `alert.py` | Motor principal: indicadores, scoring, lógica de alerta, envío Telegram, DB |
| `data_source.py` | Fetching OHLCV desde Bybit (primario) y OKX (fallback) |
| `backtester.py` | Backtest histórico point-in-time, walk-forward 70/30, reporte de expectancy en R |
| `diagnose_scan.py` | Diagnóstico detallado del scan actual sin enviar alertas |
| `market_context.json` | Contexto macro manual por símbolo (sesgos, bloqueos, ajustes de RR) |
| `alerts_state.db` | SQLite con tabla `alerts` y cooldowns |

## Activos rastreados

BTC, ETH, SOL, BNB, XRP, TRX, XLM, DOT, TON, LTC, LINK — todos vs USDT, en Bybit Spot.

## Arquitectura de 3 timeframes

### 1D — `evaluate_macro_confirmation()`
- Determina si el sesgo macro permite el lado (LONG/SHORT)
- Aplica ajustes de score/rank desde `market_context.json`
- Fuente: CoinGecko API (`DAILY_LOOKBACK_DAYS=365`)
- Resultado: `macro_ok` + `score_adjustment` + `rank_adjustment`

### 4H — `evaluate_setup_confirmation()`
- Capa principal del setup: EMA stack, ADX+DI, RSI, Fibonacci, VWAP, momentum de volumen
- Calcula entry, stop_loss, tp1, tp2 (basado en ATR + estructura)
- Fuente: Bybit/OKX klines via `data_source.fetch_klines()`
- Resultado: `setup_ok` + score base

### 15m — `evaluate_timing_confirmation()`
- Solo valida calidad de ejecución: alineación EMA20/EMA50, RSI táctico, distancia a VWAP
- No genera setup; sólo aprueba o rechaza el timing
- Resultado: `timing_ok` + pequeños ajustes de score

## Tipos de alerta

- **FULL**: 3/3 confirmaciones + score ≥ MIN_SCORE + RR ≥ MIN_RR
- **TACTICAL**: 4H+15m alineados, 1D no perfecto pero `soft_ok`=true, score alto (≥ TACTICAL_MIN_SCORE)

## Thresholds clave (env vars + defaults)

```
MIN_SCORE=7.0          # Score mínimo para alerta
MIN_RR=1.8             # R:R mínimo
MIN_ADX=20.0           # ADX mínimo
COOLDOWN_HOURS=24      # Cooldown por símbolo+lado
MAX_ALERTS_PER_RUN=2   # Máximo alertas por ejecución
MAX_ALERTS_PER_GROUP=1 # Máximo por grupo de activos
TACTICAL_MIN_SCORE=8.0
RISK_PER_TRADE_USD=50.0  # USD en riesgo por trade
```

## Pipeline de una alerta

1. `get_market_prices()` → CoinGecko (daily + hourly data)
2. `build_ohlc_from_prices()` → resample a 1D, 4H, 15m
3. `evaluate_macro_confirmation()` → sesgo 1D
4. Para cada side (LONG, SHORT):
   - `evaluate_setup_confirmation()` → setup 4H
   - `evaluate_timing_confirmation()` → timing 15m
   - `build_candidate()` → combina scores, aplica policy
   - `apply_execution_quality_gate()` → descarta entradas tardías
5. Quality gates: ADX, RSI extremo, régimen MIXED
6. Ranking y deduplicación → max 2 alertas por run
7. `format_alert_message()` → HTML para Telegram
8. `send_telegram()` + persistencia en SQLite

## Sizing de posición

```python
qty = (RISK_PER_TRADE_USD × risk_multiplier) / abs(entry - stop_loss)
```
`risk_multiplier` se recorta en modo táctico (≤ 0.75) o por ejecución tardía (≤ 0.65).

## market_context.json — estructura

```json
{
  "GLOBAL": { "caution_level": "NORMAL", "allowed_sides": ["LONG","SHORT"], ... },
  "BTC":    { "macro_bias": "BEARISH", "hard_block_long": false, "tp1_rr": 0.75, ... }
}
```

Claves relevantes por asset: `caution_level`, `allowed_sides`, `hard_block_long/short`, `long_resistance_near`, `short_support_near`, `tp1_rr`, `tp2_rr`, `risk_multiplier`, `fast_exit_mode`, `require_breakout_above`, `require_breakdown_below`.

## data_source.py — comportamiento

- Primario: **Bybit v5** (`/v5/market/kline`, spot)
- Fallback: **OKX v5** (`/api/v5/market/candles`)
- Binance fue descartado porque bloquea IPs de cloud (Azure, GitHub Actions)
- `fetch_klines(symbol, timeframe, candles_needed)` → DataFrame con columnas: ts, Open, High, Low, Close, Volume, QuoteVolume, __source__
- Última fila siempre es vela cerrada (drop_unclosed=True por defecto)

## SQLite — tabla `alerts`

Columnas clave: symbol, side, timeframe, setup_key (hash deduplicador), entry_price, stop_loss, tp1, tp2, rr_ratio, score, adx, rsi, status (ACTIVE/INVALIDATED/EXPIRED/CLOSED), validation_status (PENDING/RESOLVED), sent_at.

Migración automática: `_migrate_alerts_table()` añade columnas faltantes con ALTER TABLE.

## Backtester

```bash
python backtester.py                        # 24m, todos los activos
python backtester.py --symbol BTC --months 6
python backtester.py --fees 0.001 --slippage 0.0005 --output results.json
```

- Walk-forward 70/30 (train/test) para detectar overfit
- Forward bars: 24 × 4H = 96h por default
- Métrica primaria: expectancy en R (no win rate)

## Entorno y dependencias

```
Python 3.9
pandas>=2.2, requests>=2.32
pytest>=8.0
.venv/ en el directorio raíz
```

Variables de entorno requeridas para producción:
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `COINGECKO_API_KEY` (demo key, rate limit generoso)

## Tests

```bash
pytest tests/
```

Archivos: `tests/test_alert.py`, `tests/test_data_source.py`, `tests/test_backtester.py`.

## Grupos de activos (para ranking y límite por grupo)

- Majors: BTC, ETH
- Layer1: SOL, DOT, TON
- Exchange: BNB
- Infra: LINK
- Payments: XRP, TRX, XLM
- Legacy: LTC
