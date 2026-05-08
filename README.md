# Crypto Sentinel Bot — Laser v1

Versión simplificada para producir más alertas operables sin convertir el bot en una metralleta de ruido.

## Qué cambió

- **4H manda**: la idea nace en 4H.
- **1D y 15m acompañan**: dejan de ser una muralla de 3/3 rígido.
- **Execution gate relajado**: solo bloquea cuando la entrada ya es claramente tarde o inválida.
- **Menos castigos por contexto**: `market_context.json` ahora ajusta y no asfixia.
- **Cooldown más corto**: pensado para capturar más oportunidades reales.
- **Resumen opcional**: por defecto no manda el mensaje de “no hay nada”.

## Arquitectura

### 1D
Da sesgo y pequeños ajustes de score/ranking. Solo bloquea si el contexto está realmente del lado contrario o manualmente bloqueado.

### 4H
Es la capa principal. Evalúa:
- estructura EMA20/EMA50/EMA200
- ADX + DI
- RSI
- zona Fibonacci
- VWAP
- momentum de volumen
- espacio estructural

### 15m
Solo valida calidad de ejecución:
- alineación EMA20/EMA50
- RSI táctico
- reclaim / breakdown local
- distancia al VWAP

## Parámetros por defecto

- `MIN_SCORE=5.2`
- `MIN_RR=1.35`
- `COOLDOWN_HOURS=12`
- `MAX_ALERTS_PER_RUN=3`
- `MAX_ALERTS_PER_GROUP=2`
- `SEND_RUN_SUMMARY=false`

## Uso

```bash
python alert.py
python backtester.py --symbol BTC --months 6
python backtester.py --months 12 --output results.json
```

## Archivos

- `alert.py`: motor principal
- `backtester.py`: backtest histórico
- `data_source.py`: Bybit/OKX para OHLCV
- `market_context.json`: contexto macro manual
- `crypto-alert.yml`: workflow GitHub Actions
