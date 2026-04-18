# Crypto Sentinel Bot

Bot de alertas de criptomonedas con memoria persistente, deduplicación de setups, ranking de oportunidades y una capa opcional de contexto macro manual.

## Qué hace esta versión

- Usa **SQLite** para conservar memoria entre ejecuciones.
- Evalúa únicamente la **última vela 4h cerrada**.
- Construye un **setup_key** con símbolo, dirección, timeframe, régimen, bucket RSI, zona Fibonacci y bucket de precio.
- Bloquea alertas similares durante **24 horas**.
- Reenvía solo si hubo **invalidación** o **mejora material**.
- Rankea los setups válidos y envía solo los mejores por corrida.
- Permite incorporar **contexto macro manual** desde `market_context.json`.
- Enriquece la alerta con **VWAP sintético**, **momentum de volumen** y contexto macro.

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
- `MARKET_CHART_DAYS` default `90`
- `BASE_INTERVAL` default `hourly`
- `TRADING_TIMEFRAME` default `4h`
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

## Cómo funciona la lógica

1. Descarga precios horarios desde CoinGecko.
2. Reconstruye velas **4h** y descarta la vela aún abierta.
3. Calcula EMA20, EMA50, EMA200, RSI, ATR, ADX, Fibonacci, VWAP sintético y momentum de volumen.
4. Aplica el **contexto macro manual** si existe.
5. Valida el setup técnico.
6. Revisa si existe una alerta similar activa en las últimas 24h.
7. Rankea las alertas válidas y envía solo las mejores.
8. Guarda en SQLite únicamente las alertas realmente confirmadas por Telegram.

## Contexto macro manual

`market_context.json` te deja meter información cualitativa estructurada para que el bot no dependa solo del timeframe 4h.

### Ejemplo

```json
{
  "GLOBAL": {
    "caution_level": "NORMAL"
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
- `allowed_sides`: lados permitidos. Hoy el bot opera `LONG`, pero queda listo para ampliar.
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

- `Timeframe`: temporalidad usada en la decisión. En esta versión, `4h`.
- `Precio`: precio de entrada evaluado sobre la última vela cerrada.
- `Score`: puntuación total del setup después de sumar o restar factores técnicos y macro.
- `ADX`: fuerza de tendencia. Más alto suele implicar más direccionalidad.
- `RSI`: momento relativo. El bot favorece zonas sanas, no extremos.
- `Régimen`: estructura de EMAs. `BULL_STACK` significa EMA20 > EMA50 > EMA200.
- `Fib`: zona de retroceso en el lookback configurado.
- `R:R`: relación riesgo/beneficio entre entrada, stop y target.
- `TARGET (TP)`: objetivo técnico calculado.
- `STOP (SL)`: stop técnico calculado.
- `VWAP`: distancia del precio frente al VWAP sintético. Ayuda a detectar extensión o valor.
- `Volumen`: lectura de momentum basada en rango de velas como proxy de actividad.
- `Macro`: contexto manual aplicado al activo.
- `Prioridad`: puntuación final del ranking para decidir cuáles alertas salen primero.
- `Análisis`: razones técnicas y macro que explican el score.
- `Motivo de envío`: por qué el bot decidió enviarla a pesar del cooldown y la deduplicación.

## Nota sobre el estado legacy

Si todavía existe `alert_state.json`, el bot puede migrarlo una vez. Después de validar que todo está bien, puedes eliminarlo.

## Recomendación operativa

El bot es un filtro técnico y táctico. La decisión final sigue siendo humana, sobre todo cuando:

- el activo está en zona de resistencia macro,
- el mercado está en rango de resolución,
- o el contexto diario contradice el 4h.
