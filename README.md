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
- Enriquece la alerta con **VWAP sintético**, **momentum de volumen**, confirmaciones por timeframe y contexto macro.

## Archivos principales

- `alert.py`: motor principal.
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
- `SLEEP_BETWEEN_ASSETS` default `1.0`
- `FIB_LOOKBACK` default `55`
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
- VWAP sintético
- momentum de volumen
- cercanía al swing high

### 3) Confirmación de timing — 15m

No define la idea. Solo decide si el momento de ejecución es aceptable.

Qué revisa:
- estructura EMA20/EMA50 en 15m
- RSI 15m
- distancia al VWAP 15m
- divergencia de momentum
- cercanía al máximo local reciente

## Regla principal

El bot solo manda una alerta si se cumplen las **3 confirmaciones**:

- `macro_ok = True`
- `setup_ok = True`
- `timing_ok = True`

Si alguna falla, no dispara alerta.

## Cómo funciona el flujo

1. Descarga datos diarios, horarios y de 1 día intradía desde CoinGecko.
2. Reconstruye velas **1D**, **4H** y **15m**.
3. Descarta la última vela abierta de cada timeframe.
4. Calcula indicadores por capa.
5. Aplica el contexto macro manual si existe.
6. Exige `3/3` confirmaciones.
7. Revisa si existe una alerta similar activa en las últimas 24h.
8. Rankea las alertas válidas y envía solo las mejores.
9. Guarda en SQLite únicamente las alertas realmente confirmadas por Telegram.

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

### Variables soportadas

- `macro_regime`: estado macro, por ejemplo `BULLISH`, `BEARISH`, `RANGE`, `RESOLUTION_RANGE`.
- `macro_bias`: sesgo macro principal.
- `short_term_bias`: sesgo de corto plazo.
- `allowed_sides`: lados permitidos.
- `caution_level`: `LOW`, `NORMAL`, `MEDIUM`, `HIGH`, `EXTREME`.
- `long_score_adjustment`: ajuste directo al score del setup long.
- `long_rank_adjustment`: ajuste a la prioridad final del ranking.
- `hard_block_long`: bloqueo duro del lado long.
- `fast_exit_mode`: añade cautela y gestión táctica rápida al mensaje.
- `long_resistance_near`: indica que hay resistencia macro cerca.
- `long_resistance_label`: texto corto de esa resistencia.
- `short_support_label`: texto corto del soporte relevante.
- `note`: nota libre corta que saldrá en la alerta.

## Qué significan las variables de la alerta

- `Timeframe`: temporalidad operativa principal. En esta versión, `4h`.
- `Precio`: precio de entrada evaluado sobre la última vela 4H cerrada.
- `Score`: puntuación final después de ajustes técnicos, macro y timing.
- `ADX`: fuerza de tendencia en 4H.
- `RSI`: momento relativo en 4H.
- `Régimen`: estructura de EMAs en 4H. `BULL_STACK` significa EMA20 > EMA50 > EMA200.
- `Fib`: zona de retroceso en el lookback configurado.
- `R:R`: relación riesgo/beneficio entre entrada, stop y target.
- `TARGET (TP)`: objetivo técnico calculado.
- `STOP (SL)`: stop técnico calculado.
- `VWAP`: distancia del precio frente al VWAP sintético de 4H.
- `Volumen`: lectura de momentum basada en rango de velas como proxy de actividad.
- `Macro 1D`: indica si la capa diaria confirmó o no el long.
- `Setup 4H`: indica si la lógica operativa validó el setup.
- `Timing 15m`: indica si el momento de ejecución fue aceptable.
- `Prioridad`: puntuación final del ranking para decidir cuáles alertas salen primero.
- `Análisis`: razones técnicas y macro que explican el resultado.
- `Motivo de envío`: por qué el bot decidió enviarla a pesar del cooldown y la deduplicación.

## Cómo mantener `market_context.json`

No lo cambies por rutina. Cámbialo solo cuando cambie el escenario.

Ejemplos:
- ruptura de una línea o nivel mayor
- cambio de sesgo macro
- nueva resistencia o soporte relevante
- cambio del nivel de cautela
- cambio de lados permitidos

Regla práctica:
- revisión rápida diaria
- actualización real solo si cambió la estructura
- revisión más completa al cierre semanal

## Nota sobre el estado legacy

Si todavía existe `alert_state.json`, el bot puede migrarlo una vez. Después de validar que todo está bien, puedes eliminarlo.

## Recomendación operativa

El bot es un filtro técnico y táctico. La decisión final sigue siendo humana, sobre todo cuando:

- el activo está en zona de resistencia macro,
- el mercado está en rango de resolución,
- o el contexto diario contradice el 4H.
