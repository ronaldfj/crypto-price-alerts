"""
SUMMARY TEMPLATE: Ultra-minimal version
"""

SUMMARY_ULTRA_MINIMAL = """
📋 RESUMEN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Enviadas: 0
👀 En watch: 5 (esperando setup mejor)
⏸️ Bloqueadas: 11 (R:R bajo, confirmaciones incompletas)
"""

SUMMARY_COMPACT = """
📋 RESUMEN EJECUCIÓN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Enviadas: 0
👀 Watch: XLM, XRP, ETH, LTC, TON
⏸️ Bloqueadas: 11 (score/rr/confirmaciones)
"""

SUMMARY_MINIMAL = """
📋 RESUMEN EJECUCIÓN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Enviadas: 0
👀 En vigilancia: 5 assets
  (Esperar mejor estructura 4H)
⏸️ Rechazadas: 11
  (R:R bajo o confirmaciones <3/3)
"""

if __name__ == '__main__':
    print("ULTRA MINIMAL:")
    print(SUMMARY_ULTRA_MINIMAL)
    print("\nCOMPACT:")
    print(SUMMARY_COMPACT)
    print("\nMINIMAL:")
    print(SUMMARY_MINIMAL)
