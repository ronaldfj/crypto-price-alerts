"""
ALERT TEMPLATE: Versión optimizada (minimal noise, máxima claridad)
"""

ALERT_TEMPLATE_MINIMAL = """
🔴 LTC SHORT | R:R 2.81 | ADX 26 | Score 7.40

📍 ENTRADA: $52.06
🛑 STOP: $52.52 (0.5%)
🎯 TP1: $51.12 (2.06R) | TP2: $50.78 (2.81R)

⚠️ Timing: EJECUTABLE ahora
📊 Confirmaciones: 3/3 ✅
🧠 Motivo: Bajista, EMA20<50<200, RSI 34, ADX 26+
"""

ALERT_TEMPLATE_SHORT = """
🔴 SHORT LÁSER: LTC
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💰 Entrada: $52.06
🛑 Stop: $52.52 (0.5%)
🎯 TP: $51.12 (2.06R) / $50.78 (2.81R)

✅ Status: 3/3 confirmaciones | Ejecutable
📈 Score: 7.40 | ADX: 26 | RSI: 34
🧭 Setup: Bajista (EMA20<50<200)

📌 Riesgo: Swing low reciente en $52.5
⏱️ Timeframe: 4H
"""

ALERT_TEMPLATE_MEDIUM = """
🔴 LTC SHORT | LÁSER | 4H
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📍 ENTRADA: $52.06 (ejecutable ahora)
🛑 STOP: $52.52 | 📊 Riesgo: 0.5%
🎯 TP1: $51.12 (2.06R) | TP2: $50.78 (2.81R)

✅ Confirmaciones: 3/3
📈 Score: 7.40 | ADX: 26 | RSI: 34
🧭 Régimen: BEAR_STACK (EMA20<50<200)

⚠️ Principal: Swing low $52.5 cercano
💡 Recomendación: Ejecutar, respetar SL
🛟 Breakeven: Mover SL a $51.72 (0.75R)
"""

SUMMARY_TEMPLATE_MINIMAL = """
📋 RESUMEN EJECUCIÓN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✅ ENVIADAS (1):
  • LTC SHORT | R:R 2.81 | Prioridad 88

👀 VIGILANCIA (5):
  • XLM, ETH, XRP, DOT, SOL SHORT: Esperar mejor setup

⏸️ BLOQUEADAS:
  • BTC, ETH, DOT: R:R insuficiente
  • TRX, XRP: Confirmaciones incompletas
"""

SUMMARY_TEMPLATE_DETAILED = """
📋 RESUMEN EJECUCIÓN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✅ ENVIADAS (1):
  LTC SHORT | Score 7.40 | R:R 2.81 | Prioridad 88.2

👀 EN VIGILANCIA (5):
  XLM, ETH, XRP, DOT, SOL (SHORT) → Esperar setup mejor estructurado

⏸️ BLOQUEADAS (11):
  BTC LONG/SHORT | ETH LONG/SHORT | DOT LONG/SHORT | TON LONG/SHORT
  LTC LONG | XRP LONG | TRX LONG
  
  Razones: R:R bajo, confirmaciones incompletas, timing invalid

📊 STATS:
  Evaluadas: 21 combinaciones
  Válidas: 1/21 (4.8%)
  Ruido eliminado: 95.2%
"""

if __name__ == '__main__':
    print("MINIMAL:")
    print(ALERT_TEMPLATE_MINIMAL)
    print("\n" + "="*60)
    print("SHORT:")
    print(ALERT_TEMPLATE_SHORT)
    print("\n" + "="*60)
    print("MEDIUM:")
    print(ALERT_TEMPLATE_MEDIUM)
    print("\n" + "="*60)
    print("SUMMARY MINIMAL:")
    print(SUMMARY_TEMPLATE_MINIMAL)
    print("\n" + "="*60)
    print("SUMMARY DETAILED:")
    print(SUMMARY_TEMPLATE_DETAILED)
