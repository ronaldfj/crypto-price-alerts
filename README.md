# Crypto Sentinel Bot

Bot de alertas de criptomonedas orientado a continuidad alcista en velas de 4 horas, con persistencia en SQLite, deduplicación semántica por setup, ranking de oportunidades y envío por Telegram.

## Qué hace

- descarga datos desde CoinGecko
- reconstruye velas OHLC de 4h a partir de precios horarios
- evalúa únicamente velas cerradas
- calcula EMA20, EMA50, EMA200, RSI, ATR, ADX y contexto Fibonacci
- construye una identidad de setup para distinguir entre “misma idea” y “nueva idea”
- evita alertas repetidas durante 24 horas si el setup sigue siendo esencialmente el mismo
- reenvía solo si hubo invalidación o mejora material
- rankea los setups válidos y envía solo los mejores por corrida
- persiste el estado en `alerts_state.db`

## Arquitectura resumida

El flujo es este:

1. obtener datos horarios
2. reconstruir velas 4h cerradas
3. calcular indicadores
4. validar setup alcista
5. deduplicar por `setup_key` y similitud
6. rankear setups válidos
7. aplicar límite por corrida y por grupo
8. enviar a Telegram
9. guardar solo las alertas realmente enviadas en SQLite

## Persistencia

El archivo activo del estado es:

- `alerts_state.db`

Ese archivo guarda el histórico de alertas enviadas, setups activos, invalidaciones y metadatos internos.

`alert_state.json` solo era del modelo anterior. Si aún existe, el script intenta migrarlo una sola vez. Después ya no debería ser necesario.

## Lógica principal del setup

La alerta long exige, como base:

- régimen `BULL_STACK` (`EMA20 > EMA50 > EMA200`)
- precio sobre EMA200
- `+DI > -DI`
- `ADX >= 18`
- `R:R >= MIN_RR`
- `score >= MIN_SCORE`

Fibonacci sigue participando en el análisis, en el score y en la identidad del setup, pero no bloquea por sí solo una oportunidad fuerte.

## Deduplicación

La app ya no deduplica solo por símbolo. Ahora deduplica por setup.

El `setup_key` considera:

- símbolo
- dirección
- timeframe
- régimen
- bucket de RSI
- zona Fibonacci
- bucket de precio

Si aparece un setup suficientemente similar dentro de `COOLDOWN_HOURS`, no se vuelve a enviar. Solo se permite reenviar si la nueva señal mejora de forma material.

## Ranking y diversificación

Después de pasar los filtros de señal y deduplicación, los setups válidos se ordenan por prioridad interna.

La prioridad favorece:

- score más alto
- ADX más fuerte
- RSI más cercano a zona sana
- R:R sólido
- mejor contexto Fibonacci
- cruce reciente EMA20/EMA50
- majors como BTC y ETH

Además, el bot aplica un límite por corrida y un límite por grupo, para no mandar varias alertas casi idénticas en el mismo bloque horario.

Grupos actuales:

- `Majors`: BTC, ETH
- `Layer1`: TON, SOL, DOT
- `Exchange`: BNB
- `Infra`: LINK
- `Payments`: TRX, XRP, XLM
- `Legacy`: LTC

## Variables de entorno

### Integración

- `TELEGRAM_BOT_TOKEN`: token del bot de Telegram
- `TELEGRAM_CHAT_ID`: chat ID de destino
- `COINGECKO_API_KEY`: opcional; si la tienes, se usa en las llamadas a CoinGecko

### Persistencia y compatibilidad

- `ALERT_DB_FILE`: nombre del archivo SQLite. Default: `alerts_state.db`
- `LEGACY_STATE_FILE`: archivo JSON antiguo para migración. Default: `alert_state.json`

### Mercado y timeframe

- `VS_CURRENCY`: moneda contra la que se consulta el precio. Default: `usd`
- `MARKET_CHART_DAYS`: ventana de precios horarios para reconstruir OHLC. Default: `90`
- `BASE_INTERVAL`: intervalo base de CoinGecko. Default: `hourly`
- `TRADING_TIMEFRAME`: timeframe operativo reconstruido localmente. Default: `4h`
- `FIB_LOOKBACK`: cantidad de velas usadas para contexto Fibonacci. Default: `55`

### Calidad y control de alertas

- `COOLDOWN_HOURS`: horas de bloqueo para setups similares. Default: `24`
- `MIN_SCORE`: score mínimo para validar una señal. Default: `6.0`
- `MIN_RR`: R:R mínimo para considerar una alerta. Default: `2.0`

### Operación

- `REQUEST_TIMEOUT`: timeout HTTP. Default: `20`
- `SLEEP_BETWEEN_ASSETS`: pausa entre activos. Default: `1.0`

### Ranking

- `ENABLE_RANKING`: activa ranking y selección de top setups. Default: `true`
- `MAX_ALERTS_PER_RUN`: máximo de alertas a enviar por corrida. Default: `2`
- `MAX_ALERTS_PER_GROUP`: máximo por grupo en una misma corrida. Default: `1`
- `SEND_RUN_SUMMARY`: envía resumen al final de la corrida. Default: `true`

## Qué significan las variables de la alerta

Ejemplo visual:

- `ALERTA COMPRA: BTC`: activo detectado
- `Timeframe: 4h`: vela operativa usada para la señal
- `Precio`: precio de entrada usado por el modelo al cierre de la última vela válida
- `Score`: puntuación agregada del setup. Resume tendencia, posición del precio, RSI, ADX y contexto Fib
- `ADX`: fuerza de tendencia. Más alto normalmente significa tendencia más definida
- `RSI`: momentum del activo. En este bot, valores intermedios suelen ser mejores que valores extremos
- `Régimen`: estructura de medias. `BULL_STACK` significa `EMA20 > EMA50 > EMA200`
- `Fib`: zona de retroceso de Fibonacci en la que cae el precio actual respecto al swing reciente. `OUTSIDE` significa fuera de las bandas objetivo
- `R:R`: relación riesgo/beneficio estimada entre entrada, stop y target
- `TARGET (TP)`: precio objetivo calculado
- `STOP (SL)`: nivel de invalidación o salida defensiva calculado
- `Prioridad`: score interno de ranking para comparar setups válidos entre sí
- `Grupo`: categoría del activo usada para diversificar alertas por corrida
- `Análisis`: razones concretas que explican por qué la señal calificó
- `Motivo de envío`: por qué el sistema decidió enviarla en esta corrida, por ejemplo “sin alerta similar activa” o “mejora material”

## Instalación local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python alert.py
```

## Workflow de GitHub Actions

Puntos importantes:

- el workflow debe hacer commit de `alerts_state.db`
- el repo debe tener permisos `contents: write`
- si cambias la frecuencia del cron, recuerda que GitHub Actions interpreta el cron en UTC

## Recomendaciones de operación

- para una estrategia basada en velas 4h, una corrida cada 4 horas suele ser la opción más coherente
- si prefieres detectar invalidaciones o mejoras más rápido, puedes correrlo cada hora
- cuando hagas cambios grandes en la lógica, prueba una vez con `alerts_state.db` limpio

## Limitaciones actuales

- Fibonacci usa un swing simple por ventana de lookback; no es todavía un motor avanzado de swing detection
- CoinGecko puede tener pequeños huecos o cambios en granularidad si altera su API
- el ranking es conservador y heurístico; está diseñado para priorizar sin romper el motor base

## Próximas mejoras naturales

- ranking por régimen global de mercado
- filtro adicional para evitar entradas demasiado extendidas
- resumen más ejecutivo cuando varias monedas muestran la misma condición de mercado
- exportación de métricas de desempeño del bot
