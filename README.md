# Crypto Sentinel Bot

Bot de alertas de criptomonedas con memoria persistente, deduplicación semántica, ranking de oportunidades y confirmación en tres capas.

## Qué hace esta versión

- Usa **SQLite** para conservar memoria entre ejecuciones.
- Evalúa únicamente **velas cerradas**.
- Exige **3 de 3 confirmaciones** antes de disparar una alerta:
  - **1D** para contexto macro y sesgo.
  - **4H** para el setup operativo.
  - **15m** para el timing de entrada.
- Construye un **setup_key** con símbolo, dirección, timeframe, régimen, bucket RSI, zona Fibonacci y bucket de precio.
- Bloquea alertas similares durante **24 horas**.
- Reenvía solo si hubo **invalidación** o **mejora material**.
- Rankea los setups válidos y envía solo los mejores por corrida.
- Permite incorporar **contexto macro manual** desde `market_context.json`.
- Usa **total_volumes** de CoinGecko para medir momentum de volumen. Cuando no hay datos de volumen, hace fallback al rango de vela.
- Enriquece la alerta con lectura humana, VWAP, volumen, confirmaciones por timeframe y contexto macro.
- Incluye un **esqueleto de backtesting** en `backtester.py` para validar la estrategia fuera de producción.

## Archivos principales

- `alert.py`: motor principal.
- `backtester.py`: esqueleto para backtesting histórico.
- `alerts_state.db`: base SQLite generada automáticamente.
- `market_context.json`: contexto macro manual opcional.
- `.github/workflows/crypto-alert.yml`: ejecución programada y persistencia del estado.

## Secrets requeridos

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `COINGECKO_API_KEY` opcional, pero recomendado.

## Variables de entorno opcionales

- `ALERT_DB_FILE` default `alerts_state.db`
- `LEGACY_STATE_FILE` default `alert_state.json`
- `MARKET_CONTEXT_FILE` default `market_context.json`
- `VS_CURRENCY` default `usd`
- `DAILY_LOOKBACK_DAYS` default `365`
- `HOURLY_LOOKBACK_DAYS` default `90`
- `INTRADAY_LOOKBACK_DAYS` default `3`
- `HOURLY_INTERVAL` default `hourly`
- `MACRO_TIMEFRAME` default `1D`
- `TRADING_TIMEFRAME` default `4h`
- `ENTRY_TIMEFRAME` default `15min`
- `COOLDOWN_HOURS` default `24`
- `MIN_SCORE` default `6.0`
- `MIN_RR` default `2.0`
- `REQUEST_TIMEOUT` default `20`
- `REQUEST_RETRIES` default `4`
- `REQUEST_BACKOFF_SECONDS` default `2.0`
- `RATE_LIMIT_SLEEP_SECONDS` default `8.0`
- `SLEEP_BETWEEN_ASSETS` default `1.0`
- `FIB_LOOKBACK` default `55`
- `TIMING_MIN_POINTS` default `3.0`
- `WATCHLIST_MIN_SETUP_SCORE` default `6.0`
- `ENABLE_RANKING` default `true`
- `MAX_ALERTS_PER_RUN` default `2`
- `MAX_ALERTS_PER_GROUP` default `1`
- `SEND_RUN_SUMMARY` default `true`

## Arquitectura de decisión

### 1) Confirmación macro — 1D

Valida si el activo permite longs desde un contexto más amplio.

Qué revisa:
- relación entre precio y EMA20/EMA50 diaria
- RSI diario en zona razonable
- dirección diaria no claramente en contra
- contexto manual desde `market_context.json`

### 2) Confirmación de setup — 4H

Aquí vive la lógica operativa principal.

Qué revisa:
- régimen EMA20/EMA50/EMA200
- ADX y dirección
- RSI
- R:R
- Fibonacci
- VWAP ponderado por volumen 24h si existe
- momentum de volumen
- cercanía al swing high
- stop ATR + swing low
- TP1 / TP2 por múltiplos de riesgo

### 3) Confirmación de timing — 15m

No define la idea. Solo decide si el momento de ejecución es aceptable.

Qué revisa:
- estructura EMA20/EMA50 en 15m
- RSI 15m
- distancia al VWAP 15m
- divergencia de momentum
- cercanía al máximo local reciente
- sistema de puntos con bloqueo duro solo en combinaciones realmente malas

## Regla principal

El bot solo manda una alerta si se cumplen las **3 confirmaciones**:

- `macro_ok = True`
- `setup_ok = True`
- `timing_ok = True`

Si alguna falla, no dispara alerta. Si el activo queda cerca, entra a **vigilancia táctica**.

## Qué significan las variables de la alerta

- `Score final`: score después de ajustes macro y timing.
- `Score 4H`: score puro del setup 4H antes de castigos o premios de otras capas.
- `ADX`: fuerza de tendencia en 4H.
- `RSI`: momento relativo en 4H.
- `Régimen`: estructura de EMAs en 4H. `BULL_STACK` significa EMA20 > EMA50 > EMA200.
- `Fib`: zona de retroceso en el lookback configurado.
- `R:R`: relación riesgo/beneficio entre entrada y target principal.
- `TP1`: objetivo parcial de 1:2.
- `TP2`: objetivo extendido de 1:4.
- `STOP (SL)`: stop técnico por ATR y swing low.
- `VWAP`: distancia del precio frente al VWAP del timeframe operativo.
- `Volumen`: lectura del momentum usando `total_volumes` de CoinGecko cuando existe.
- `Macro 1D`: indica si la capa diaria confirmó o no el long.
- `Setup 4H`: indica si la lógica operativa validó el setup base.
- `Timing 15m`: indica si el momento de ejecución fue aceptable.
- `Prioridad`: puntuación final del ranking para decidir cuáles alertas salen primero.
- `Análisis`: razones técnicas y macro que explican el resultado.
- `Motivo de envío`: por qué el bot decidió enviarla a pesar del cooldown y la deduplicación.

## Contexto macro manual

`market_context.json` te deja meter información cualitativa estructurada para que el bot no dependa solo del timeframe 4H.

### Ejemplo

```json
{
  "GLOBAL": {
    "caution_level": "NORMAL",
    "allowed_sides": ["LONG"]
  },
  "BTC": {
    "macro_regime": "RESOLUTION_RANGE",
    "macro_bias": "BEARISH",
    "short_term_bias": "BULLISH",
    "allowed_sides": ["LONG", "SHORT"],
    "caution_level": "HIGH",
    "long_resistance_near": true,
    "long_resistance_label": "84k / línea cyan",
    "short_support_label": "línea magenta",
    "long_score_adjustment": -1.5,
    "long_rank_adjustment": -8.0,
    "fast_exit_mode": true,
    "note": "Rango de resolución; revisar diario y cerrar posiciones rápido."
  }
}
```

## Backtesting

`backtester.py` es un punto de partida para validar histórico sin tocar producción.

Uso básico:

```bash
python backtester.py --symbol BTC
```

Sirve para:
- probar una versión simplificada del flujo histórico
- medir cuántas señales 3/3 aparecen
- estimar retorno a 48h, win rate y profit factor básico

## Nota operativa

El bot es un filtro técnico y táctico. La decisión final sigue siendo humana, sobre todo cuando:
- el activo está en zona de resistencia macro
- el mercado está en rango de resolución
- o el contexto diario contradice el 4H
