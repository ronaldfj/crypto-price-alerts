"""
backtester.py — Backtesting histórico del Crypto Sentinel Bot

Reutiliza la lógica exacta de alert.py para simular señales pasadas
y calcular métricas reales: win rate, profit factor, max drawdown.

Uso:
    python backtester.py                        # todos los activos, 90 días
    python backtester.py --symbol BTC --days 60
    python backtester.py --symbol SOL --min-score 7.0

Requiere las mismas variables de entorno que alert.py.
"""

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# Importar lógica de alert.py directamente — sin duplicar código
sys.path.insert(0, os.path.dirname(__file__))
from alert import (
    CRYPTO_IDS,
    DAILY_LOOKBACK_DAYS,
    HOURLY_LOOKBACK_DAYS,
    HOURLY_INTERVAL,
    INTRADAY_LOOKBACK_DAYS,
    MARKET_CONTEXT_FILE,
    MIN_SCORE,
    MIN_RR,
    SLEEP_BETWEEN_ASSETS,
    TRADING_TIMEFRAME,
    build_candidate,
    build_ohlc_from_prices,
    evaluate_macro_confirmation,
    evaluate_setup_confirmation,
    evaluate_timing_confirmation,
    fetch_btc_dominance,
    get_market_prices,
    load_market_context,
    normalize_context,
)


# ── Configuración del backtester ──────────────────────────────────────────────
BACKTEST_STEP_CANDLES = int(os.getenv("BACKTEST_STEP_CANDLES", "1"))
BACKTEST_FORWARD_CANDLES = int(os.getenv("BACKTEST_FORWARD_CANDLES", "24"))
BACKTEST_MIN_HISTORY = int(os.getenv("BACKTEST_MIN_HISTORY", "230"))


# ── Simulación de outcome ─────────────────────────────────────────────────────
def simulate_outcome(
    future_candles: pd.DataFrame,
    entry_price: float,
    stop_loss: float,
    tp1: float,
    tp2: float,
) -> Dict[str, Any]:
    """
    Recorre las velas futuras en orden y determina qué tocó primero:
    TP1, TP2 o SL. Orden de prioridad por vela: Low primero (SL),
    luego High (TP). Conservador por diseño.
    """
    for i, row in future_candles.iterrows():
        low = float(row["Low"])
        high = float(row["High"])
        candle_num = i + 1

        # SL tocado — Low primero (pesimista)
        if low <= stop_loss:
            pnl_pct = (stop_loss - entry_price) / entry_price * 100
            return {
                "outcome": "SL_HIT",
                "exit_price": stop_loss,
                "pnl_pct": round(pnl_pct, 3),
                "candles_to_exit": candle_num,
                "tp1_hit": False,
                "tp2_hit": False,
            }

        # TP1 tocado
        if high >= tp1 and not False:
            # Si también alcanza TP2 en la misma vela, cuenta TP2
            if high >= tp2:
                pnl_pct = (tp2 - entry_price) / entry_price * 100
                return {
                    "outcome": "TP2_HIT",
                    "exit_price": tp2,
                    "pnl_pct": round(pnl_pct, 3),
                    "candles_to_exit": candle_num,
                    "tp1_hit": True,
                    "tp2_hit": True,
                }
            pnl_pct = (tp1 - entry_price) / entry_price * 100
            return {
                "outcome": "TP1_HIT",
                "exit_price": tp1,
                "pnl_pct": round(pnl_pct, 3),
                "candles_to_exit": candle_num,
                "tp1_hit": True,
                "tp2_hit": False,
            }

    # Tiempo agotado sin resolución
    last_close = float(future_candles.iloc[-1]["Close"])
    pnl_pct = (last_close - entry_price) / entry_price * 100
    return {
        "outcome": "EXPIRED",
        "exit_price": last_close,
        "pnl_pct": round(pnl_pct, 3),
        "candles_to_exit": len(future_candles),
        "tp1_hit": False,
        "tp2_hit": False,
    }


# ── Backtesting por activo ────────────────────────────────────────────────────
def backtest_symbol(
    symbol: str,
    cg_id: str,
    market_context: Dict[str, Any],
    btc_dominance: Optional[float],
    min_score: float,
) -> List[Dict[str, Any]]:
    """
    Descarga datos históricos y simula señales pasadas para un activo.
    Desliza una ventana de BACKTEST_MIN_HISTORY velas y en cada paso
    evalúa si se generaría señal, luego simula el outcome con velas futuras.
    """
    print(f"  [{symbol}] Descargando datos...")

    daily_prices = get_market_prices(cg_id, DAILY_LOOKBACK_DAYS, interval=None)
    fourh_prices = get_market_prices(cg_id, HOURLY_LOOKBACK_DAYS, interval=HOURLY_INTERVAL)
    intraday_prices = get_market_prices(cg_id, INTRADAY_LOOKBACK_DAYS, interval=None)

    if daily_prices is None or fourh_prices is None or intraday_prices is None:
        print(f"  [{symbol}] Datos insuficientes — saltando.")
        return []

    daily_full = build_ohlc_from_prices(daily_prices, "1D", 220)
    fourh_full = build_ohlc_from_prices(fourh_prices, TRADING_TIMEFRAME, 220)
    entry_full = build_ohlc_from_prices(intraday_prices, INTRADAY_LOOKBACK_DAYS * 96, 30)

    if daily_full is None or fourh_full is None or entry_full is None:
        print(f"  [{symbol}] OHLC insuficiente — saltando.")
        return []

    total_4h = len(fourh_full)
    results = []
    signals_found = 0

    # Deslizar ventana sobre velas 4H históricas
    start_idx = BACKTEST_MIN_HISTORY
    for i in range(start_idx, total_4h - BACKTEST_FORWARD_CANDLES, BACKTEST_STEP_CANDLES):
        # Ventana histórica hasta la vela i (exclusivo — simula "vela cerrada")
        fourh_window = fourh_full.iloc[:i].copy()
        daily_window = daily_full[daily_full["ts"] <= fourh_window.iloc[-1]["ts"]].copy()
        entry_window = entry_full[entry_full["ts"] <= fourh_window.iloc[-1]["ts"]].tail(96).copy()

        if len(daily_window) < 220 or len(fourh_window) < 220 or len(entry_window) < 30:
            continue

        normalized_context = normalize_context(market_context, symbol)
        if btc_dominance is not None:
            normalized_context["btc_dominance"] = btc_dominance

        macro_eval = evaluate_macro_confirmation(daily_window, symbol, normalized_context)
        setup_eval = evaluate_setup_confirmation(fourh_window, symbol, cg_id)
        timing_eval = evaluate_timing_confirmation(entry_window, symbol)

        if not macro_eval or not setup_eval or not timing_eval:
            continue

        candidate = build_candidate(symbol, cg_id, macro_eval, setup_eval, timing_eval)

        if not candidate["alert"]:
            continue
        if candidate["score"] < min_score:
            continue

        signals_found += 1
        entry_price = float(candidate["entry_price"])
        stop_loss = float(candidate["stop_loss"])
        tp1 = float(candidate.get("tp1", entry_price * 1.02))
        tp2 = float(candidate.get("tp2", candidate["take_profit"]))

        # Velas futuras para simular el outcome
        future = fourh_full.iloc[i:i + BACKTEST_FORWARD_CANDLES].reset_index(drop=True)
        outcome = simulate_outcome(future, entry_price, stop_loss, tp1, tp2)

        result = {
            "symbol": symbol,
            "candle_ts": str(fourh_window.iloc[-1]["ts"]),
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "tp1": tp1,
            "tp2": tp2,
            "score": candidate["score"],
            "adx": candidate["adx"],
            "rsi": candidate["rsi"],
            "fib_zone": candidate["fib_zone"],
            "vwap_distance_pct": candidate.get("vwap_distance_pct", 0.0),
            "volume_divergence": candidate.get("volume_divergence", False),
            "regime": candidate["regime"],
            "rr_ratio": candidate["rr_ratio"],
            **outcome,
        }
        results.append(result)

    print(f"  [{symbol}] Señales simuladas: {signals_found} | Con outcome: {len(results)}")
    return results


# ── Métricas ──────────────────────────────────────────────────────────────────
def compute_metrics(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not results:
        return {"total": 0}

    total = len(results)
    winners = [r for r in results if r["outcome"] in {"TP1_HIT", "TP2_HIT"}]
    losers = [r for r in results if r["outcome"] == "SL_HIT"]
    expired = [r for r in results if r["outcome"] == "EXPIRED"]

    win_rate = len(winners) / total * 100
    avg_win = sum(r["pnl_pct"] for r in winners) / len(winners) if winners else 0.0
    avg_loss = sum(r["pnl_pct"] for r in losers) / len(losers) if losers else 0.0
    total_pnl = sum(r["pnl_pct"] for r in results)

    # Profit factor: suma de ganancias / suma de pérdidas absolutas
    gross_profit = sum(r["pnl_pct"] for r in winners if r["pnl_pct"] > 0)
    gross_loss = abs(sum(r["pnl_pct"] for r in losers if r["pnl_pct"] < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Max drawdown secuencial (pnl acumulado)
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in results:
        cumulative += r["pnl_pct"]
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Desglose por zona Fib
    fib_breakdown: Dict[str, Dict[str, Any]] = {}
    for r in results:
        zone = r["fib_zone"]
        if zone not in fib_breakdown:
            fib_breakdown[zone] = {"total": 0, "wins": 0, "pnl": 0.0}
        fib_breakdown[zone]["total"] += 1
        fib_breakdown[zone]["pnl"] += r["pnl_pct"]
        if r["outcome"] in {"TP1_HIT", "TP2_HIT"}:
            fib_breakdown[zone]["wins"] += 1

    avg_candles = sum(r["candles_to_exit"] for r in results) / total

    return {
        "total": total,
        "winners": len(winners),
        "losers": len(losers),
        "expired": len(expired),
        "win_rate_pct": round(win_rate, 1),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "total_pnl_pct": round(total_pnl, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "avg_candles_to_exit": round(avg_candles, 1),
        "fib_breakdown": fib_breakdown,
    }


# ── Reporte ───────────────────────────────────────────────────────────────────
def print_report(all_results: List[Dict[str, Any]], per_symbol: Dict[str, List[Dict[str, Any]]]) -> None:
    sep = "─" * 55

    print(f"\n{sep}")
    print("  REPORTE DE BACKTESTING — CRYPTO SENTINEL")
    print(sep)

    # Global
    global_metrics = compute_metrics(all_results)
    if global_metrics["total"] == 0:
        print("\n  Sin señales generadas en el período evaluado.")
        print("  Considera bajar MIN_SCORE o ampliar el período.\n")
        return

    print(f"\n  GLOBAL ({global_metrics['total']} señales)")
    print(f"  Win rate    : {global_metrics['win_rate_pct']:.1f}%  "
          f"({global_metrics['winners']}W / {global_metrics['losers']}L / {global_metrics['expired']}E)")
    print(f"  Avg win     : +{global_metrics['avg_win_pct']:.2f}%")
    print(f"  Avg loss    : {global_metrics['avg_loss_pct']:.2f}%")
    print(f"  PnL total   : {global_metrics['total_pnl_pct']:+.2f}%")
    print(f"  Profit factor: {global_metrics['profit_factor']:.2f}")
    print(f"  Max drawdown: -{global_metrics['max_drawdown_pct']:.2f}%")
    print(f"  Velas media : {global_metrics['avg_candles_to_exit']:.1f} (4H)")

    # Por activo
    print(f"\n{sep}")
    print("  POR ACTIVO")
    print(f"  {'SYM':<6} {'N':>4} {'WR%':>6} {'PnL%':>7} {'PF':>5} {'MaxDD':>7}")
    print(f"  {'-'*5} {'-'*4} {'-'*6} {'-'*7} {'-'*5} {'-'*7}")

    for symbol, res in sorted(per_symbol.items(), key=lambda x: compute_metrics(x[1]).get("total_pnl_pct", 0), reverse=True):
        m = compute_metrics(res)
        if m["total"] == 0:
            continue
        print(f"  {symbol:<6} {m['total']:>4} {m['win_rate_pct']:>5.1f}% "
              f"{m['total_pnl_pct']:>+7.2f}% {m['profit_factor']:>5.2f} "
              f"-{m['max_drawdown_pct']:>6.2f}%")

    # Por zona Fib
    fib = global_metrics.get("fib_breakdown", {})
    if fib:
        print(f"\n{sep}")
        print("  POR ZONA FIBONACCI")
        print(f"  {'ZONA':<16} {'N':>4} {'WR%':>6} {'PnL%':>7}")
        for zone, data in sorted(fib.items(), key=lambda x: x[1]["pnl"], reverse=True):
            wr = data["wins"] / data["total"] * 100 if data["total"] > 0 else 0
            print(f"  {zone:<16} {data['total']:>4} {wr:>5.1f}% {data['pnl']:>+7.2f}%")

    print(f"\n{sep}\n")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Backtester del Crypto Sentinel Bot")
    parser.add_argument("--symbol", type=str, default=None, help="Símbolo específico (ej: BTC)")
    parser.add_argument("--days", type=int, default=HOURLY_LOOKBACK_DAYS, help="Días de historia")
    parser.add_argument("--min-score", type=float, default=MIN_SCORE, help="Score mínimo")
    parser.add_argument("--output", type=str, default=None, help="Guardar resultados en JSON")
    args = parser.parse_args()

    market_context = load_market_context(MARKET_CONTEXT_FILE)
    btc_dominance = fetch_btc_dominance()
    if btc_dominance is not None:
        print(f"BTC Dominance: {btc_dominance:.1f}%")

    symbols_to_test = (
        {k: v for k, v in CRYPTO_IDS.items() if v == args.symbol}
        if args.symbol
        else CRYPTO_IDS
    )

    if not symbols_to_test:
        print(f"Símbolo '{args.symbol}' no encontrado. Disponibles: {list(CRYPTO_IDS.values())}")
        return

    print(f"\nBacktesting {len(symbols_to_test)} activo(s) | "
          f"min_score={args.min_score} | forward={BACKTEST_FORWARD_CANDLES} velas\n")

    all_results: List[Dict[str, Any]] = []
    per_symbol: Dict[str, List[Dict[str, Any]]] = {}

    for cg_id, symbol in symbols_to_test.items():
        results = backtest_symbol(symbol, cg_id, market_context, btc_dominance, args.min_score)
        per_symbol[symbol] = results
        all_results.extend(results)
        time.sleep(SLEEP_BETWEEN_ASSETS)

    print_report(all_results, per_symbol)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
        print(f"Resultados guardados en: {args.output}")


if __name__ == "__main__":
    main()
