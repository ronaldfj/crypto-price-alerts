"""
Diagnóstico del escaneo: corre el mismo pipeline que alert.py
pero sin enviar Telegram y con output tabular detallado.
"""

import os
import sys
import time
import json

# Apuntar a directorio del proyecto
sys.path.insert(0, os.path.dirname(__file__))

from alert import (
    CRYPTO_IDS,
    DAILY_LOOKBACK_DAYS, HOURLY_LOOKBACK_DAYS, INTRADAY_LOOKBACK_DAYS,
    HOURLY_INTERVAL, TRADING_TIMEFRAME, ENTRY_TIMEFRAME,
    MIN_SCORE, MIN_RR, MIN_ADX,
    SLEEP_BETWEEN_ASSETS, MARKET_CONTEXT_FILE,
    get_market_prices, build_ohlc_from_prices,
    evaluate_macro_confirmation, evaluate_setup_confirmation, evaluate_timing_confirmation,
    build_candidate, apply_execution_quality_gate,
    load_market_context, normalize_context, fetch_btc_dominance,
    parse_allowed_sides, latest_price_from_df,
    get_db_connection, init_db, import_legacy_state_if_needed, invalidate_old_alerts,
    should_send_alert, DB_FILE,
    ENABLE_RSI_CONFIRMATION, ENABLE_EXECUTION_QUALITY_GATE, ALLOW_MIXED_REGIME,
    validate_rsi_confirmation, validate_adx_minimum,
)

SEP = "─" * 100


def icon(val: bool) -> str:
    return "✅" if val else "❌"


def main():
    print(SEP)
    print("DIAGNÓSTICO DE ESCANEO – sin enviar alertas")
    print(SEP)

    conn = get_db_connection(DB_FILE)
    init_db(conn)
    import_legacy_state_if_needed(conn)

    market_context = load_market_context(MARKET_CONTEXT_FILE)
    btc_dominance = fetch_btc_dominance()
    print(f"BTC Dominance: {btc_dominance:.1f}%" if btc_dominance else "BTC Dominance: N/A")
    print()

    results = []

    for cg_id, symbol in CRYPTO_IDS.items():
        print(f"  Descargando {symbol} ({cg_id})...", end="", flush=True)

        daily_prices = get_market_prices(cg_id, DAILY_LOOKBACK_DAYS, interval=None)
        fourh_prices  = get_market_prices(cg_id, HOURLY_LOOKBACK_DAYS, interval=HOURLY_INTERVAL)
        intraday_prices = get_market_prices(cg_id, INTRADAY_LOOKBACK_DAYS, interval=None)
        current_price = latest_price_from_df(intraday_prices)

        daily_df  = build_ohlc_from_prices(daily_prices,  "1D",             220) if daily_prices is not None else None
        fourh_df  = build_ohlc_from_prices(fourh_prices,  TRADING_TIMEFRAME, 220) if fourh_prices is not None else None
        entry_df  = build_ohlc_from_prices(intraday_prices, ENTRY_TIMEFRAME,  60) if intraday_prices is not None else None

        if daily_df is None or fourh_df is None or entry_df is None:
            print(" datos insuficientes")
            results.append({"symbol": symbol, "side": "?", "status": "NO_DATA",
                            "blocker": "datos insuficientes (1D/4H/15m)", "score": 0, "adx": 0, "rr": 0})
            time.sleep(SLEEP_BETWEEN_ASSETS)
            continue

        normalized_context = normalize_context(market_context, symbol)
        if btc_dominance is not None:
            normalized_context["btc_dominance"] = btc_dominance

        allowed_sides = parse_allowed_sides(normalized_context)
        print(f" ok | precio={current_price:.4g} | sides={allowed_sides}")

        for side in allowed_sides:
            macro_eval  = evaluate_macro_confirmation(daily_df, symbol, normalized_context, side=side)
            setup_eval  = evaluate_setup_confirmation(fourh_df, symbol, cg_id, side=side)
            timing_eval = evaluate_timing_confirmation(entry_df, symbol, side=side)

            if not macro_eval or not setup_eval or not timing_eval:
                results.append({"symbol": symbol, "side": side, "status": "EVAL_FAIL",
                                "blocker": "evaluate_* devolvió None", "score": 0, "adx": 0, "rr": 0})
                continue

            candidate = build_candidate(symbol, cg_id, macro_eval, setup_eval, timing_eval)
            candidate = apply_execution_quality_gate(candidate, current_price)
            invalidate_old_alerts(conn, candidate)

            blockers = []
            if not candidate["alert"]:
                if not candidate.get("macro_ok"):
                    blockers.append("1D_macro")
                if not candidate.get("setup_ok"):
                    blockers.append("4H_setup")
                if not candidate.get("timing_ok"):
                    blockers.append("15m_timing")
                if candidate["score"] < MIN_SCORE:
                    blockers.append(f"score={candidate['score']:.2f}<{MIN_SCORE}")
                if candidate["adx"] < MIN_ADX:
                    blockers.append(f"adx={candidate['adx']:.1f}<{MIN_ADX}")
                if candidate["rr_ratio"] < candidate.get("required_min_rr", MIN_RR):
                    blockers.append(f"rr={candidate['rr_ratio']:.2f}<{candidate.get('required_min_rr', MIN_RR):.2f}")
                # quality gate reasons
                for r in candidate.get("reasons", []):
                    if any(kw in r for kw in ["gate", "Gate", "ADX mínimo", "RSI extremo", "Régimen mixto", "Execution gate"]):
                        blockers.append(r[:60])
                exec_state = candidate.get("execution_state", "")
                if exec_state in {"INVALID_NOW", "LATE"}:
                    blockers.append(f"exec={exec_state}:{candidate.get('execution_decision','')[:40]}")

                results.append({
                    "symbol": symbol, "side": side, "status": "BLOCKED",
                    "blocker": " | ".join(blockers) if blockers else "alert=False (razón desconocida)",
                    "score": candidate["score"],
                    "adx": candidate["adx"],
                    "rr": candidate["rr_ratio"],
                    "regime": candidate.get("regime", "?"),
                    "macro_ok": candidate.get("macro_ok"),
                    "setup_ok": candidate.get("setup_ok"),
                    "timing_ok": candidate.get("timing_ok"),
                })
            else:
                should_send, improved_id, decision = should_send_alert(conn, candidate)
                if should_send:
                    results.append({
                        "symbol": symbol, "side": side, "status": "READY_TO_SEND",
                        "blocker": f"→ {decision}",
                        "score": candidate["score"],
                        "adx": candidate["adx"],
                        "rr": candidate["rr_ratio"],
                        "regime": candidate.get("regime", "?"),
                        "macro_ok": True, "setup_ok": True, "timing_ok": True,
                    })
                else:
                    results.append({
                        "symbol": symbol, "side": side, "status": "COOLDOWN",
                        "blocker": decision,
                        "score": candidate["score"],
                        "adx": candidate["adx"],
                        "rr": candidate["rr_ratio"],
                        "regime": candidate.get("regime", "?"),
                        "macro_ok": True, "setup_ok": True, "timing_ok": True,
                    })

        time.sleep(SLEEP_BETWEEN_ASSETS)

    conn.close()

    # ── Tabla de resultados ─────────────────────────────────────────────────────
    print()
    print(SEP)
    print(f"{'SYMBOL':<6} {'SIDE':<6} {'STATUS':<15} {'1D':>3} {'4H':>3} {'15m':>3} {'SCORE':>6} {'ADX':>6} {'RR':>5}  RÉGIMEN    BLOQUEADO / RAZÓN")
    print(SEP)

    status_order = {"READY_TO_SEND": 0, "COOLDOWN": 1, "BLOCKED": 2, "EVAL_FAIL": 3, "NO_DATA": 4}
    results.sort(key=lambda x: (status_order.get(x["status"], 9), -x.get("score", 0)))

    for r in results:
        macro_icon  = icon(r.get("macro_ok",  False)) if r.get("status") not in {"NO_DATA","EVAL_FAIL"} else "─"
        setup_icon  = icon(r.get("setup_ok",  False)) if r.get("status") not in {"NO_DATA","EVAL_FAIL"} else "─"
        timing_icon = icon(r.get("timing_ok", False)) if r.get("status") not in {"NO_DATA","EVAL_FAIL"} else "─"
        score_str   = f"{r.get('score', 0):.2f}" if r.get("score") else "  ─"
        adx_str     = f"{r.get('adx', 0):.1f}"  if r.get("adx")   else "  ─"
        rr_str      = f"{r.get('rr', 0):.2f}"   if r.get("rr")    else " ─"
        regime      = r.get("regime", "─")
        status      = r["status"]
        blocker     = r.get("blocker", "")[:80]

        print(f"{r['symbol']:<6} {r['side']:<6} {status:<15} {macro_icon:>3} {setup_icon:>3} {timing_icon:>3} "
              f"{score_str:>6} {adx_str:>6} {rr_str:>5}  {regime:<10} {blocker}")

    print(SEP)
    ready = [r for r in results if r["status"] == "READY_TO_SEND"]
    blocked = [r for r in results if r["status"] == "BLOCKED"]
    cooldown = [r for r in results if r["status"] == "COOLDOWN"]
    print(f"Listos para enviar: {len(ready)} | Bloqueados: {len(blocked)} | En cooldown: {len(cooldown)}")
    print()

    if ready:
        print("⚡ CANDIDATOS LISTOS:")
        for r in ready:
            print(f"   {r['symbol']} {r['side']} | score={r.get('score',0):.2f} | adx={r.get('adx',0):.1f} | rr={r.get('rr',0):.2f} | {r['blocker']}")


if __name__ == "__main__":
    main()
