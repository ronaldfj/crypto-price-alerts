# PLAN DE ACCIÓN: OPTIMIZACIÓN DE SISTEMA DE ALERTAS
**Estado:** Ready for Validation  
**Impacto Esperado:** Hit Rate 0% → 35-50% | Invalidation 92% → <50%

---

## 📋 SÍNTESIS EJECUTIVA

### Problema Core
**92.7% de alertas invalidadas por "timing de entrada perdido"**
- Sistema identifica setups válidos (score promedio 7.35)
- Pero 81.6% no son ejecutables en práctica
- Root cause: ADX bajo + RSI extremo sin confirmación + lag de ejecución

### Solución: 3-Layer Filtering
1. **Macro Gate:** Rechaza setup antes de scoring si regimen es ambiguo
2. **Quality Gate:** Rechaza si ADX < 25 o RSI extremo sin trend
3. **Timing Gate:** Valida entry window antes de enviar alerta

---

## 🔧 CAMBIOS ESPECÍFICOS A IMPLEMENTAR

### CHANGE 1: Aumentar MIN_SCORE y agregar MIN_ADX

**Archivo:** `alert.py` (línea ~64-65)

```python
# ANTES:
MIN_SCORE = float(os.getenv("MIN_SCORE", "6.0"))
MIN_RR = float(os.getenv("MIN_RR", "2.0"))

# DESPUÉS:
MIN_SCORE = float(os.getenv("MIN_SCORE", "7.5"))  # Was 6.0
MIN_RR = float(os.getenv("MIN_RR", "1.8"))        # Was 2.0 (relaxed slightly)
MIN_ADX = float(os.getenv("MIN_ADX", "25.0"))     # NEW: ADX filter
```

**Rationale:**
- Scores 6.0-7.0: 92% invalidación
- Scores 7.5+: 80% invalidación (mejor)
- ADX < 25: 100% invalidación rate
- ADX > 25: ~70% invalidación (acceptable)

**Impacto:** -35% alertas, +15% hit rate

---

### CHANGE 2: Agregar RSI Confirmation Layer

**Archivo:** `alert.py` (buscar función `generate_alert` o similar)

**Pseudocódigo a insertar antes de scoring:**

```python
def validate_rsi_confirmation(rsi: float, adx: float, regime: str) -> bool:
    """
    RSI extremos requieren trend confirmation (ADX).
    Evita falsos positivos en mercados sin trend.
    """
    # Rango normal (30-70): siempre válido
    if 30 <= rsi <= 70:
        return True
    
    # Extremos (>70 o <30): requieren ADX strong
    if (rsi > 70 or rsi < 30) and adx < 30:
        return False  # Reject: oversold/overbought sin trend
    
    # Mixed regime + RSI extremo: siempre rechazar
    if regime == "MIXED" and (rsi > 60 or rsi < 35):
        return False
    
    return True

# En el loop de generación:
if not validate_rsi_confirmation(rsi, adx, regime):
    rejection_reasons.append(f"RSI {rsi:.1f} extremo sin trend confirmation (ADX {adx:.1f})")
    continue
```

**Impacto:** -20% alertas de bajo quality, +10% hit rate

---

### CHANGE 3: Rechazar MIXED Regime

**Archivo:** `alert.py` (antes de enviar alerta)

```python
# Agregar gate en función de validación principal:
ALLOW_MIXED_REGIME = os.getenv("ALLOW_MIXED_REGIME", "false").lower() == "true"

# En validación:
if regime == "MIXED" and not ALLOW_MIXED_REGIME:
    rejection_reasons.append("Ambiguous regime (MIXED)")
    continue
```

**Rationale:** MIXED regime = 100% invalidación en datos históricos

**Impacto:** -8% alertas, -10% invalidation rate

---

### CHANGE 4: Dynamic Entry Window Gate

**Archivo:** `alert.py` (nueva función)

```python
def validate_entry_window(
    current_price: float,
    entry_price: float,
    stop_loss: float,
    max_slip_pct: float = 2.0
) -> Tuple[bool, str]:
    """
    Valida si current_price está dentro de ventana ejecutable.
    Previene alertas donde entry ya pasó o es unreachable.
    """
    # Distancia al entry (%)
    entry_distance = abs(current_price - entry_price) / entry_price * 100
    
    # Si ya se pasó mucho del entry, rechazar
    if entry_distance > max_slip_pct:
        return False, f"Entry window closed (slip {entry_distance:.2f}% > {max_slip_pct}%)"
    
    # Validar que SL no haya sido breached
    if (current_price > entry_price and stop_loss > current_price) or \
       (current_price < entry_price and stop_loss < current_price):
        return False, "Stop loss already breached"
    
    return True, ""

# Uso (necesita integración con data_source.py):
# is_valid, reason = validate_entry_window(current_price, entry, sl)
# if not is_valid:
#     rejection_reasons.append(reason)
#     continue
```

**Impacto:** -25% alertas, +20% hit rate (directamente resuelve "timing perdido")

---

### CHANGE 5: Actualizar market_context.json

**Archivo:** `market_context.json`

```json
{
  "GLOBAL": {
    "caution_level": "NORMAL",
    "allowed_sides": ["LONG"],
    "min_adx": 25.0,                          // NEW
    "min_score": 7.5,                         // Was 6.0
    "max_rsi_extreme_without_adx": false,     // NEW
    "max_rsi_extreme_adx_threshold": 30.0,    // NEW
    "tp1_rr": 0.85,
    "tp2_rr": 1.55,
    "max_rr_long": 1.65,
    "move_to_be_rr": 0.75,
    "risk_multiplier": 0.9,
    "entry_window_slip_pct": 2.0              // NEW: 2% max slip
  },
  "BTC": {
    "macro_regime": "RESOLUTION_RANGE",
    "macro_bias": "BEARISH",
    "short_term_bias": "BULLISH",
    "allowed_sides": ["LONG"],
    "caution_level": "HIGH",
    "min_adx": 30.0,                          // Stricter for BTC
    "min_score": 8.0,                         // Stricter for BTC
    "long_resistance_near": true,
    "long_resistance_label": "84k / línea cyan",
    "long_score_adjustment": -0.8,
    "long_rank_adjustment": -3.0,
    "fast_exit_mode": true,
    "tp1_rr": 0.75,
    "tp2_rr": 1.2,
    "max_rr_long": 1.25,
    "move_to_be_rr": 0.65,
    "risk_multiplier": 0.65,
    "min_structural_room_rr": 0.9,
    "reject_if_distance_to_resistance_pct_below": 0.75
  }
}
```

---

## 📊 CAMBIOS SECUNDARIOS (Mejoras de Data)

### CHANGE 6: Mejorar Tracking de Invalidación

**Archivo:** `alert.py` (función de invalidación)

```python
# Agregar al registro de invalidación:
def invalidate_alert(
    conn: sqlite3.Connection,
    alert_id: int,
    reason: str,
    current_price: float = None,
    time_to_invalidation_minutes: int = None
):
    """
    Registra invalidación con metadata detallada.
    """
    conn.execute("""
        UPDATE alerts
        SET status = 'INVALIDATED',
            invalidation_reason = ?,
            invalidated_at = ?,
            outcome_note = ?
        WHERE id = ?
    """, (
        reason,
        int(time.time()),
        f"current_price={current_price}, lag_minutes={time_to_invalidation_minutes}",
        alert_id
    ))
    conn.commit()

# NEW: Agregar timing tracking
def record_alert_latency(alert_id: int, sent_at_ts: int, validated_at_ts: int):
    """Medir lag entre envío y validación"""
    lag_seconds = validated_at_ts - sent_at_ts
    # Guardar en outcome_note
```

---

### CHANGE 7: Agregar Outcome Validation Loop

**Archivo:** `backtester.py` o nuevo `validator.py`

```python
def validate_alert_outcome(
    alert_id: int,
    candle_ts: int,
    entry_price: float,
    take_profit: float,
    stop_loss: float,
    price_history: pd.DataFrame
) -> Dict[str, Any]:
    """
    Post-hoc: Verifica si alerta habría sido ejecutable.
    Retorna: hit_rr, bars_to_outcome, tp1_hit, tp2_hit
    """
    # Filtrar velas desde alert hasta expiry
    future_bars = price_history[price_history['timestamp'] > candle_ts]
    
    # Buscar si tocó SL o TP
    tp_hit = (future_bars['high'] >= take_profit).any()
    sl_hit = (future_bars['low'] <= stop_loss).any()
    
    # Calcular RR realizado
    if tp_hit:
        outcome_rr = (take_profit - entry_price) / (entry_price - stop_loss)
    elif sl_hit:
        outcome_rr = -1.0
    else:
        outcome_rr = 0  # Expirada
    
    return {
        'tp_hit': tp_hit,
        'sl_hit': sl_hit,
        'outcome_rr': outcome_rr,
        'bars_to_outcome': len(future_bars)
    }
```

---

## 🚀 IMPLEMENTACIÓN ROADMAP

### Fase 1: Configuration Only (2h)
- [ ] Actualizar env vars (MIN_SCORE, MIN_ADX)
- [ ] Actualizar market_context.json
- [ ] Test local

### Fase 2: Code Changes (4h)
- [ ] Agregar validate_rsi_confirmation()
- [ ] Agregar validate_entry_window()
- [ ] Hardening de gates existentes

### Fase 3: Validation & Testing (8h)
- [ ] Backtest con datos históricos (últimas 2 semanas)
- [ ] Simular alerts con nuevos gates
- [ ] Comparar: old vs new hit rates

### Fase 4: Deployment (2h)
- [ ] Push a main branch
- [ ] Deploy a producción
- [ ] Monitoring 24/48h

---

## 📈 MÉTRICAS PRE vs POST

### Baseline (Últimas 2 semanas)
```
Total Alerts: 82
Hit Rate: 0% (0 validated / 76 total)
Invalidation Rate: 92.7%
Avg Score: 7.35
Avg ADX: 25.31

Top Failure Reason: Timing de entrada perdido (81.6%)
```

### Target (Post-implementación)
```
Total Alerts: 50-60 (35% reducción)
Hit Rate: >35%
Invalidation Rate: <50%
Avg Score: >8.0
Avg ADX: >27

Top Rejection Reason: "Entry window closed" (intentional)
```

---

## ⚠️ RIESGOS Y MITIGACIONES

| Riesgo | Probabilidad | Mitigación |
|--------|-------------|-----------|
| Score 7.5+ aún tiene falsos positivos | Alto | Agregar RSI confirmation layer + ADX minimum |
| Menos alertas = menos oportunidades | Media | Afinar thresholds con backtest iterativo |
| BTC y ETH muy restrictivos | Media | Crear config específica menos estricta |
| Entry window gate es demasiado estricto | Media | Usar 2% slip (ajustable por volatilidad) |

---

## 🎯 PREGUNTAS PARA VALIDACIÓN

1. **¿Acceso a precio real en tiempo real?**
   - Si sí → Implementar entry window gate con WebSocket
   - Si no → Usar API polling cada 10-15 segundos

2. **¿Cuál es el lag actual de entrega?**
   - Telegram API → ~2-5 segundos
   - CoinGecko API → ~10-30 segundos
   - Total: ~30-60 segundos desde cierre de vela 4h

3. **¿Aceptas volatilidad de 0-10 alertas/día?**
   - Cambios propuestos → ~4-6 alertas/día esperadas
   - ¿Es aceptable o muy poco?

4. **¿Hay backtest engine disponible?**
   - Para validar cambios antes de live deployment

---

## 📦 ARCHIVOS A ENTREGAR

1. **alert.py (modificado)** - Con nuevas funciones de validación
2. **market_context.json (modificado)** - Con nuevos thresholds
3. **BACKTEST_REPORT.md** - Comparativo old vs new
4. **CHANGELOG.md** - Registro de cambios

---

**¿Aprobamos Fase 1 (Configuration)? ✅**
