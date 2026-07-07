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

## Observaciones de producción (Jun 8 – Jul 1, 2026)

Estos patrones surgieron del análisis de las primeras 3 semanas en producción. Útiles para calibrar ajustes futuros.

**Patrón de invalidaciones (26 alertas, todas SHORT):**
- 92% invalidación — promedio 4.9h hasta invalidar
- "Confirmación macro perdida" (54%): el 1D oscilaba porque el mercado estaba en rango choppy, no en bajista limpio. Afecta principalmente BTC, DOT, SOL, ETH, LTC.
- "Timing de entrada perdido" (46%): la ventana 15m revertía en horas. Especialmente LINK (3/3 alertas con este patrón) y TRX (2/2).

**Por activo:**
- **BNB**: en esta muestra de 3 semanas parecía el mejor (2/3 en TP1), pero el backtest de 12 meses (96 señales) lo muestra como el **peor** activo del set (43.8% WR, -0.179R) — la muestra corta de producción era ruido, no señal. No priorizar BNB por esta observación temprana.
- **LINK**: 3/3 timing invalidations — 15m extremadamente inestable. Señales de LINK requieren mayor escepticismo.
- **BTC/DOT/SOL**: mayoría de invalidaciones por macro — sensibles a contexto 1D cambiante. En el backtest de 12m, BTC también rinde negativo (-0.182R); DOT y SOL sí generalizan positivo.

**Sobre los targets:**
- Los 2 únicos trades cerrados llegaron a TP1 pero no a TP2. El mercado en rango da movimientos cortos, no extensiones. Confirma que `fast_exit_mode=true` y TP1 conservador (≤1R) es la estrategia correcta en este tipo de mercado.

**Causa raíz del problema de invalidaciones:**
El contexto tenía `caution_level: HIGH` + `long_score_adjustment: -0.8` + `long_rank_adjustment: -3.0` en BTC, pensado para un bajista limpio desde 84k. Al entrar en rango (~57k–65k), el 1D empezó a oscilar y las señales SHORT se disparaban con soporte a solo 5% de distancia — sin recorrido real. La resistencia de referencia (84k) también estaba obsoleta.

**Señal de alerta para contexto desactualizado:**
Si más del 50% de las invalidaciones son "Confirmación macro perdida" en múltiples activos simultáneamente, el `market_context.json` probablemente no refleja la fase actual del mercado y hay que revisarlo.

## Calibración validada por walk-forward (jul 2026)

Backtest de 12 meses (994 señales, todos los activos) mostró que el sistema completo apenas generalizaba fuera de muestra (walk-forward out-sample E[R]=+0.023R, degradación 0.08 = overfit — veredicto del backtester: EDGE MARGINAL). Se probó cada componente por separado, comparando expectancy in-sample vs out-sample, y solo 3 condiciones sostuvieron out-of-sample positivo para SHORT: perfil FULL (no TACTICAL), RSI en [35,50), y entrada fuera de zona Fibonacci 0.382-0.786. Con esos 3 gates aplicados, el out-of-sample sube a +0.110R y el veredicto pasa a EDGE POSITIVO NETO (ver `ENABLE_TACTICAL_ALERTS`, `REQUIRE_RSI_BAND_SHORT`, `REQUIRE_FIB_OUTSIDE_SHORT` arriba).

**Importante:** subir `MIN_ADX` parecía mejorar el agregado global (hasta +0.56R en ADX≥45), pero al separar in/out-sample cada corte de ADX más alto **empeoraba** el out-of-sample (llegaba a -0.115R en ADX≥38) — era overfitting puro. No se tocó `MIN_ADX`. Lección para futuras calibraciones: nunca decidir un threshold solo por el agregado global; siempre partir in-sample vs out-sample antes de tocar producción.

**Segunda ronda — poda de componentes del score (jul 2026):** a pedido del usuario, se repitió el mismo ejercicio in/out-sample pero por componente del score (EMA stack, VWAP, momentum de volumen, Fibonacci), buscando simplificar en vez de solo agregar filtros. Resultados: EMA stack ya no tiene variación que podar (el gate de régimen MIXED lo satura); momentum de volumen 4H para SHORT (`volume_strong`/`divergence`) no discrimina out-of-sample (+0.110R con momentum fuerte vs +0.133R sin él) y se quitó del score; distancia a VWAP >3.5% rinde negativo en ambas mitades del split (no solo in-sample) y se agregó como gate nuevo (`REQUIRE_VWAP_PROXIMITY_SHORT`, `MAX_VWAP_DISTANCE_SHORT_PCT`). Resultado neto: out-of-sample sube de +0.110R a +0.122R — mejora modesta pero consistente, como anticipaba la evidencia por componente.

BTC y BNB siguen negativos incluso con los 3 gates aplicados, pero no se excluyeron para evitar seleccionar símbolos ganadores sobre el mismo dataset donde se descubrieron — pendiente de validar con datos frescos.

## Auditoría de infraestructura y lógica (jul 2026)

Una auditoría completa encontró y corrigió lo siguiente:

- **Fuente de datos unificada**: hasta esta fecha, el escaneo en vivo (`alert.py`) armaba OHLC sintético desde CoinGecko (precio-only, sin volumen real) en las 3 capas, mientras `backtester.py` siempre usó velas reales de Bybit/OKX vía `data_source.fetch_klines_range()`. Es decir, la calibración walk-forward de este documento nunca se había probado contra los datos que el bot realmente operaba. Se migró `alert.py` (escaneo principal, invalidación de alertas, validación de outcomes) y `diagnose_scan.py` a `data_source.fetch_klines()`/`fetch_latest_price()`, alineando ambos procesos sobre la misma fuente. Efecto secundario esperado: con volumen real disponible, el componente de momentum de volumen 4H para LONG (ya validado en backtest, que siempre tuvo volumen real) empieza a contribuir al score en vivo por primera vez; el componente SHORT sigue podado del score (ver sección anterior), independientemente de la fuente de datos.
- **Gate de RR roto**: `compute_required_min_rr` derivaba el techo de RR requerido del propio `candidate["tp2_rr"]`, que ya había sido capado por `apply_context_execution_policy` — el gate era tautológico y `MIN_RR=1.8` nunca bloqueaba nada para símbolos cuyo contexto cap por debajo de ese valor (BTC, GLOBAL). Corregido para derivar el techo de `macro_eval` (el contexto, antes del cap).
- **Entry-window gate muerto**: `candidate["current_price"]` nunca se asignaba, por lo que `ENABLE_ENTRY_WINDOW_GATE` (activo por default en producción) nunca corría pese a estar "activado". Corregido.
- **Workflow duplicado eliminado**: `crypto-alert.yml` (legado, cron cada 2h) ejecutaba el mismo `alert.py` que `alert_production.yml` (cada 4h) con configuración efectivamente idéntica, sin coordinación en el commit+push de `alerts_state.db` — riesgo de alerta Telegram duplicada y de pérdida de estado por push no-fast-forward. Se eliminó `crypto-alert.yml`; `alert_production.yml` es ahora el único workflow de escaneo.

## Circuit breaker y data-health check (jul 2026)

Dos patrones adoptados tras comparar este proyecto con freqtrade (bot de ejecución, no de alertas — pero con dos ideas de gating rescatables). Ninguno requirió cambios de esquema SQLite: ambos se apoyan en estado ya existente (tabla `alerts` y key-value `meta`).

- **Circuit breaker** (`is_circuit_broken()`, hook en `should_send_alert()`): cuenta invalidaciones discrecionales (`status=INVALIDATED`, no `CLOSED` por SL/TP real) del mismo symbol+side dentro de `CIRCUIT_BREAKER_WINDOW_HOURS`; si supera `CIRCUIT_BREAKER_MAX_INVALIDATIONS`, bloquea nuevas alertas para ese symbol+side (se auto-expira solo cuando las invalidaciones salen de la ventana — no hay estado de "lock" separado que mantener). Apunta directo al patrón de producción de jun-jul 2026 (26 alertas SHORT, 92% invalidación, BTC/DOT/SOL repitiendo "confirmación macro perdida" en mercado choppy): `is_material_improvement()` permite reabrir un side dentro del cooldown cuando el setup "mejora", y sin este freno esas mejoras seguían generando alertas que volvían a invalidarse. Granularidad symbol+side (no todo el activo) porque el patrón observado fue específico de un lado. Se ve reflejado en `daily_summary.py` bajo "🔒 Bloqueadas por circuit breaker", no genera Telegram ad-hoc (consistente con `SEND_RUN_SUMMARY=false`).
- **Data-health check** (`record_data_health_failure()`/`record_data_health_success()`, persistido en `meta` bajo `data_health:{symbol}`): cuenta corridas consecutivas donde `fetch_klines` devuelve `None` para algún timeframe; al cruzar `DATA_HEALTH_ALERT_THRESHOLD` envía un aviso Telegram inmediato (no se repite hasta 7 días después o hasta que el símbolo recupere datos). Es la respuesta directa al caso TON: antes de esto, un símbolo sin cobertura de exchange fallaba en silencio indefinidamente porque el único canal (`blocked_messages` vía `format_run_summary`) depende de `SEND_RUN_SUMMARY=true`, que está apagado. `daily_summary.py` también lista símbolos con salud de datos degradada como respaldo, por si el Telegram inmediato no llegó a enviarse.

## Cuándo actualizar CLAUDE.md

Actualizar cuando cambie algo **estructural** (no thresholds temporales):
- Se agrega/quita un activo de `CRYPTO_IDS`
- Se agrega un nuevo timeframe o tipo de confirmación
- Se crean scripts/workflows nuevos
- Hay learnings de producción que cambian cómo calibrar el sistema

No actualizar por cambios en `market_context.json` — ese archivo se lee directo y cambia frecuentemente.
