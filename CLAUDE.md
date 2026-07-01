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

## Workflows de GitHub Actions

| Archivo | Qué hace | Frecuencia |
|---------|----------|------------|
| `alert_production.yml` | Escaneo completo + alertas Telegram | Cada 4h (0,4,8,12,16,20 UTC) |
| `daily_summary.yml` | Resumen diario vía `daily_summary.py` | 21:00 UTC (4pm UTC-5) |
| `backtest.yml` | Backtest manual | `workflow_dispatch` |

`alerts_state.db` se commitea automáticamente después de cada ejecución del bot con `[skip ci]`.

## Observaciones de producción (Jun 8 – Jul 1, 2026)

Estos patrones surgieron del análisis de las primeras 3 semanas en producción. Útiles para calibrar ajustes futuros.

**Patrón de invalidaciones (26 alertas, todas SHORT):**
- 92% invalidación — promedio 4.9h hasta invalidar
- "Confirmación macro perdida" (54%): el 1D oscilaba porque el mercado estaba en rango choppy, no en bajista limpio. Afecta principalmente BTC, DOT, SOL, ETH, LTC.
- "Timing de entrada perdido" (46%): la ventana 15m revertía en horas. Especialmente LINK (3/3 alertas con este patrón) y TRX (2/2).

**Por activo:**
- **BNB**: mejor desempeño — 2 de 3 alertas cerraron en TP1. Activo a priorizar cuando hay señal.
- **LINK**: 3/3 timing invalidations — 15m extremadamente inestable. Señales de LINK requieren mayor escepticismo.
- **BTC/DOT/SOL**: mayoría de invalidaciones por macro — sensibles a contexto 1D cambiante.

**Sobre los targets:**
- Los 2 únicos trades cerrados llegaron a TP1 pero no a TP2. El mercado en rango da movimientos cortos, no extensiones. Confirma que `fast_exit_mode=true` y TP1 conservador (≤1R) es la estrategia correcta en este tipo de mercado.

**Causa raíz del problema de invalidaciones:**
El contexto tenía `caution_level: HIGH` + `long_score_adjustment: -0.8` + `long_rank_adjustment: -3.0` en BTC, pensado para un bajista limpio desde 84k. Al entrar en rango (~57k–65k), el 1D empezó a oscilar y las señales SHORT se disparaban con soporte a solo 5% de distancia — sin recorrido real. La resistencia de referencia (84k) también estaba obsoleta.

**Señal de alerta para contexto desactualizado:**
Si más del 50% de las invalidaciones son "Confirmación macro perdida" en múltiples activos simultáneamente, el `market_context.json` probablemente no refleja la fase actual del mercado y hay que revisarlo.

## Cuándo actualizar CLAUDE.md

Actualizar cuando cambie algo **estructural** (no thresholds temporales):
- Se agrega/quita un activo de `CRYPTO_IDS`
- Se agrega un nuevo timeframe o tipo de confirmación
- Se crean scripts/workflows nuevos
- Hay learnings de producción que cambian cómo calibrar el sistema

No actualizar por cambios en `market_context.json` — ese archivo se lee directo y cambia frecuentemente.
