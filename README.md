# Crypto Sentinel Bot

Bot de alertas de criptomonedas orientado a reducir ruido, evitar alertas duplicadas y mantener memoria operativa entre ejecuciones de GitHub Actions.

## Qué hace esta versión

- Usa **SQLite** para persistir el estado de setups enviados.
- Construye un **setup_key** con símbolo, dirección, timeframe, régimen, bucket RSI, zona Fibonacci y bucket de precio.
- Bloquea alertas similares durante **24 horas**.
- Reenvía solo si hubo **invalidación** o **mejora material**.
- Evalúa únicamente la **última vela cerrada**.
- Migra automáticamente el cooldown antiguo desde `alert_state.json` en la primera ejecución.

## Secrets requeridos

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `COINGECKO_API_KEY` (opcional, pero recomendado)

## Variables opcionales

- `ALERT_DB_FILE` (default: `alerts_state.db`)
- `LEGACY_STATE_FILE` (default: `alert_state.json`)
- `COOLDOWN_HOURS` (default: `24`)
- `MIN_SCORE` (default: `5.5`)
- `MIN_RR` (default: `2.0`)
- `OHLC_DAYS` (default: `90`)

## Flujo de persistencia

El workflow ejecuta `alert.py` y luego hace commit del archivo `alerts_state.db` de vuelta al repositorio. Eso permite que el bot conserve memoria entre corridas de GitHub Actions.

## Archivos principales

- `alert.py`: lógica principal
- `.github/workflows/crypto_alerts.yml`: ejecución programada y persistencia
- `alerts_state.db`: base SQLite generada automáticamente

## Nota sobre el archivo legacy

Puedes dejar `alert_state.json` en el repo para que el bot migre el cooldown antiguo la primera vez. Después de verificar que todo funciona bien, lo puedes eliminar si ya no lo quieres conservar.
