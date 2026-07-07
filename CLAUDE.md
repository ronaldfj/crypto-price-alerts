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
| `inspector.py` | Inspector interactivo (Streamlit), página principal: evalúa un activo elegido a mano con el motor idéntico a `alert.py` (1D+4H+15m). Solo lectura — no envía Telegram ni escribe en `alerts_state.db`. `streamlit run inspector.py` |
| `pages/1_Resumen.py` | Segunda página del mismo app Streamlit (multipage): tabla comparativa de los 11 pares (score/RR/ADX/régimen por timeframe, LONG y SHORT). Clic en una fila → `st.switch_page` al Inspector con ese par preseleccionado y evaluado. Streamlit detecta `pages/` automáticamente junto a `inspector.py`; no cambia el comando de arranque |
| `sentinel_shared.py` | Utilidades compartidas entre `inspector.py` y `pages/1_Resumen.py`: fetchers cacheados (`get_klines`, `get_context`, `get_btc_dominance`) y `evaluate_pair()` (pipeline macro/setup/timing/candidate para ambos lados). Sin UI propia |
| `market_context.json` | Contexto macro manual por símbolo (sesgos, bloqueos, ajustes de RR) |
| `alerts_state.db` | SQLite con tabla `alerts` y cooldowns |

## Activos rastreados

BTC, ETH, SOL, BNB, XRP, TRX, XLM, DOT, TON, LTC, LINK — todos vs USDT, en Bybit Spot.

**⚠️ TON sin cobertura de datos (detectado jul 2026 vía `inspector.py`):** ni Bybit ni OKX listan un par spot para TON (`TONUSDT` / `TON-USDT`) bajo ningún nombre — se confirmó contra `/v5/market/instruments-info` (Bybit, 598 símbolos) y `/api/v5/public/instruments` (OKX, 1278 símbolos), cero coincidencias. Desde la migración de `alert.py`/`diagnose_scan.py` a `data_source.fetch_klines()`, cada corrida falla para TON ("ningún proveedor devolvió datos") y nunca genera alertas para ese activo. Con el data-health check (jul 2026, ver sección "Circuit breaker y data-health check" abajo) este tipo de falla ya no requiere inspección manual: tras 3 corridas consecutivas sin datos se envía un aviso Telegram automático. Pendiente decidir: quitar TON de `CRYPTO_IDS`/`SYMBOL_TO_BASE` o buscar un exchange/par alternativo.

## Arquitectura de 3 timeframes

### 1D — `evaluate_macro_confirmation()`
- Determina si el sesgo macro permite el lado (LONG/SHORT)
- Aplica ajustes de score/rank desde `market_context.json`
- Fuente: Bybit/OKX klines via `data_source.fetch_klines()`
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
- **TACTICAL**: 4H+15m alineados, 1D no perfecto pero `soft_ok`=true, score alto (≥ TACTICAL_MIN_SCORE). **Desactivado por default** (`ENABLE_TACTICAL_ALERTS=false`) desde jul 2026: backtest walk-forward de 12m mostró que no generaliza out-of-sample (+0.289R in-sample vs -0.137R out-sample).

## Thresholds clave (env vars + defaults)

```
MIN_SCORE=7.0          # Score mínimo para alerta
MIN_RR=1.8             # R:R mínimo
MIN_ADX=20.0           # ADX mínimo
COOLDOWN_HOURS=24      # Cooldown por símbolo+lado
MAX_ALERTS_PER_RUN=2   # Máximo alertas por ejecución
MAX_ALERTS_PER_GROUP=1 # Máximo por grupo de activos
TACTICAL_MIN_SCORE=8.0
ENABLE_TACTICAL_ALERTS=false      # Ver nota arriba
REQUIRE_RSI_BAND_SHORT=true       # Solo SHORT: exige RSI en [RSI_BAND_SHORT_MIN, RSI_BAND_SHORT_MAX)
RSI_BAND_SHORT_MIN=35.0
RSI_BAND_SHORT_MAX=50.0
REQUIRE_FIB_OUTSIDE_SHORT=true    # Solo SHORT: exige fib_zone == OUTSIDE
REQUIRE_VWAP_PROXIMITY_SHORT=true # Solo SHORT: rechaza |vwap_distance_pct| > MAX_VWAP_DISTANCE_SHORT_PCT
MAX_VWAP_DISTANCE_SHORT_PCT=3.5
SEND_RUN_SUMMARY=false   # Resumen por-corrida desactivado; daily_summary.py cubre el "una vez al día"
RISK_PER_TRADE_USD=50.0  # USD en riesgo por trade
ENABLE_CIRCUIT_BREAKER=true          # Bloquea symbol+side tras invalidaciones repetidas — ver sección abajo
CIRCUIT_BREAKER_MAX_INVALIDATIONS=3  # Nº de invalidaciones (status=INVALIDATED) que gatillan el bloqueo
CIRCUIT_BREAKER_WINDOW_HOURS=24      # Ventana de conteo (y de auto-expiración del bloqueo)
DATA_HEALTH_ALERT_THRESHOLD=3        # Corridas consecutivas sin datos antes de avisar por Telegram
```

## Pipeline de una alerta

1. `data_source.fetch_klines()` → velas 1D/4H/15m reales desde Bybit (primario) / OKX (fallback). Si algún timeframe devuelve `None`, se registra en el data-health tracker (ver abajo) en vez de fallar en silencio.
2. `data_source.fetch_latest_price()` → precio actual para el execution gate
3. `evaluate_macro_confirmation()` → sesgo 1D
4. Para cada side (LONG, SHORT):
   - `evaluate_setup_confirmation()` → setup 4H
   - `evaluate_timing_confirmation()` → timing 15m
   - `build_candidate()` → combina scores, aplica policy
   - `apply_execution_quality_gate()` → descarta entradas tardías
5. Quality gates: ADX, RSI extremo, régimen MIXED, banda RSI [35,50), fuera de zona Fibonacci, distancia a VWAP ≤3.5% (últimos 3 solo SHORT, validados por walk-forward)
6. `should_send_alert()`: cooldown + similitud de setup + **circuit breaker** (bloquea symbol+side con invalidaciones recientes — ver abajo)
7. Ranking y deduplicación → max 2 alertas por run
8. `format_alert_message()` → HTML para Telegram
9. `send_telegram()` + persistencia en SQLite

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
- `COINGECKO_API_KEY` (demo key; solo se usa para BTC dominance, no para OHLCV — ver nota de jul 2026 abajo)

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

## Observaciones de producción (jun-jul 2026)

De las primeras 3 semanas en producción: LINK y TRX tienen timing 15m inestable — mayor escepticismo en sus señales. BTC/DOT/SOL son sensibles a `market_context.json` desactualizado (mayoría de invalidaciones por "confirmación macro perdida"). **Heurística de contexto obsoleto:** si >50% de las invalidaciones recientes son por macro en varios activos a la vez, revisar `market_context.json`. BNB parecía el mejor activo en esta muestra corta (2/3 TP1) pero el backtest de 12m lo mostró como el peor (-0.179R, ver calibración abajo) — no rankear activos con muestras de producción de pocas semanas. Los únicos 2 trades cerrados llegaron a TP1 pero no TP2, confirmando que `fast_exit_mode=true` + TP1 conservador es la estrategia correcta en mercado en rango.

## Calibración validada por walk-forward (jul 2026)

Backtest de 12m mostró edge marginal agregado que sube a +0.110R out-of-sample con 3 gates SHORT: perfil FULL (no TACTICAL), RSI en banda [35,50), fuera de zona Fibonacci — ver `ENABLE_TACTICAL_ALERTS`/`REQUIRE_RSI_BAND_SHORT`/`REQUIRE_FIB_OUTSIDE_SHORT` arriba. Momentum de volumen 4H SHORT se podó del score (no discriminaba out-of-sample); VWAP >3.5% se agregó como gate (`REQUIRE_VWAP_PROXIMITY_SHORT`) tras rendir negativo en ambas mitades del split.

**Regla dura:** nunca fijar un threshold por el agregado global — subir `MIN_ADX` mejoraba el agregado pero empeoraba out-of-sample en cada corte (overfitting puro, no se tocó). Partir siempre in/out-sample antes de calibrar producción. BTC/BNB siguen negativos con los 3 gates aplicados — no excluidos, pendiente validar con datos frescos.

## Auditoría de infraestructura y lógica (jul 2026)

- **Fuente de datos unificada**: el escaneo en vivo usaba OHLC sintético de CoinGecko (sin volumen real); se migró a klines reales Bybit/OKX (`data_source.fetch_klines()`), igualando la fuente de `backtester.py` — la calibración walk-forward nunca se había probado contra los datos reales del bot hasta esta migración. Efecto: momentum de volumen 4H ahora contribuye al score LONG en vivo por primera vez.
- **Gate de RR roto**: `compute_required_min_rr` derivaba el techo del propio `candidate["tp2_rr"]` ya capado — tautológico, `MIN_RR` no bloqueaba nada en BTC/GLOBAL. Corregido para derivar del contexto antes del cap.
- **Entry-window gate muerto**: `candidate["current_price"]` nunca se asignaba — `ENABLE_ENTRY_WINDOW_GATE` nunca corría pese a estar activo. Corregido.
- **Workflow duplicado eliminado**: `crypto-alert.yml` (cron 2h) duplicaba `alert_production.yml` (4h) sin coordinar el commit de `alerts_state.db` — riesgo de alerta duplicada. Eliminado.

## Circuit breaker y data-health check (jul 2026)

Dos patrones adoptados de freqtrade (Protections/Pairlist), sin cambios de esquema SQL — ambos reusan estado existente (`alerts`, `meta`).

- **Circuit breaker** (`is_circuit_broken()`, hook en `should_send_alert()`): bloquea symbol+side tras `CIRCUIT_BREAKER_MAX_INVALIDATIONS` invalidaciones discrecionales en `CIRCUIT_BREAKER_WINDOW_HOURS` (no cuenta `CLOSED` por SL/TP real). Corta el ciclo de re-alertar un side que "mejora" (`is_material_improvement`) y vuelve a invalidarse en mercado choppy (ver observaciones de producción arriba). Se auto-expira solo; visible en `daily_summary.py` ("🔒 Bloqueadas"), sin Telegram ad-hoc.
- **Data-health check** (`record_data_health_failure/success()`, persistido en `meta` bajo `data_health:{symbol}`): avisa por Telegram tras `DATA_HEALTH_ALERT_THRESHOLD` corridas sin datos de ningún proveedor — responde al caso TON, que antes fallaba en silencio.

## Cuándo actualizar CLAUDE.md

Actualizar cuando cambie algo **estructural** (no thresholds temporales):
- Se agrega/quita un activo de `CRYPTO_IDS`
- Se agrega un nuevo timeframe o tipo de confirmación
- Se crean scripts/workflows nuevos
- Hay learnings de producción que cambian cómo calibrar el sistema

No actualizar por cambios en `market_context.json` — ese archivo se lee directo y cambia frecuentemente.

Este archivo se recarga completo en cada mensaje del proyecto — las entradas de learnings van como conclusión + por qué en pocas líneas, no como relato paso a paso. El detalle histórico completo ya vive en `git log`/commits; no hace falta duplicarlo acá.
