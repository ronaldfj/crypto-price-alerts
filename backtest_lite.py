"""
backtest_lite.py — "Sentinel Lite": backtest de un sistema mínimo de 2 indicadores
(EMA50 como filtro de tendencia + RSI14 como gatillo de entrada), un solo timeframe
(4H), sin score compuesto ni confirmaciones multi-timeframe.

Objetivo: comparar, con la misma metodología walk-forward que backtester.py (mismos
fees/slippage, mismo horizonte de salida, mismo umbral de veredicto), si un sistema
mucho más simple iguala o supera al sistema completo (1D+4H+15m, ~8 señales).

No toca alert.py ni ningún archivo de producción — es un script de investigación
independiente. Reutiliza de backtester.py la simulación de fills y el cálculo de
métricas/veredicto, y de alert.py el cálculo de indicadores (add_indicators), para
no reinventar fórmulas ya probadas.

Uso:
    python backtest_lite.py                    # 12m, todos los activos
    python backtest_lite.py --symbol BTC --months 6
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(__file__))

from alert import (
    CRYPTO_IDS,
    SIDE_LONG,
    SIDE_SHORT,
    add_indicators,
)
from backtester import (
    DEFAULT_FEE_PER_SIDE,
    DEFAULT_SLIPPAGE,
    FORWARD_BARS,
    MIN_HISTORY_BARS,
    TRAIN_FRACTION,
    TradeOutcome,
    breakdown_by,
    compute_metrics,
    compute_verdict,
    simulate_outcome_with_costs,
)
import data_source


# ── Parámetros de la estrategia mínima ──────────────────────────────────────
LITE_EMA_PERIOD = int(os.getenv("LITE_EMA_PERIOD", "50"))  # ya viene precalculado en add_indicators como ema50
LITE_ATR_STOP_MULT = float(os.getenv("LITE_ATR_STOP_MULT", "1.5"))
LITE_TARGET_R = float(os.getenv("LITE_TARGET_R", "1.5"))

# Banda de RSI para SHORT: la misma validada por walk-forward en jul 2026
# (REQUIRE_RSI_BAND_SHORT en alert.py). Para LONG es un espejo, sin validar aparte.
LITE_RSI_SHORT_MIN = float(os.getenv("LITE_RSI_SHORT_MIN", "35.0"))
LITE_RSI_SHORT_MAX = float(os.getenv("LITE_RSI_SHORT_MAX", "50.0"))
LITE_RSI_LONG_MIN = float(os.getenv("LITE_RSI_LONG_MIN", "50.0"))
LITE_RSI_LONG_MAX = float(os.getenv("LITE_RSI_LONG_MAX", "65.0"))


def backtest_lite_symbol(
    symbol: str,
    months: int,
    fee_per_side: float,
    slippage: float,
) -> List[TradeOutcome]:
    print(f"  [{symbol}] Descargando {months}m de 4H...")
    now_ts = int(time.time())
    range_start = now_ts - months * 30 * 86400

    fourh_full = data_source.fetch_klines_range(symbol, "4h", range_start - 60 * 86400, now_ts)
    if fourh_full is None or len(fourh_full) < MIN_HISTORY_BARS:
        print(f"  [{symbol}] 4H insuficiente ({len(fourh_full) if fourh_full is not None else 0}).")
        return []

    work = add_indicators(fourh_full)
    if len(work) < MIN_HISTORY_BARS:
        print(f"  [{symbol}] Histórico útil insuficiente tras indicadores ({len(work)}).")
        return []

    work = work.reset_index(drop=True)
    work["ts_epoch"] = work["ts"].astype("datetime64[ns, UTC]").astype("int64") // 10**9

    start_idx = MIN_HISTORY_BARS
    end_idx = len(work) - FORWARD_BARS
    if end_idx <= start_idx:
        print(f"  [{symbol}] Ventana insuficiente para slide.")
        return []

    first_ts = int(work.iloc[start_idx]["ts_epoch"])
    last_ts = int(work.iloc[end_idx - 1]["ts_epoch"])
    train_cutoff_ts = first_ts + int((last_ts - first_ts) * TRAIN_FRACTION)

    print(f"  [{symbol}] slide: {start_idx}-{end_idx} ({end_idx - start_idx} pasos) | "
          f"train hasta ts={train_cutoff_ts}")

    trades: List[TradeOutcome] = []

    for i in range(start_idx, end_idx):
        row = work.iloc[i]
        close = float(row["Close"])
        ema = float(row["ema50"])
        rsi = float(row["rsi"])
        atr = float(row["atr"])
        cur_ts = int(row["ts_epoch"])

        if atr <= 0:
            continue

        side: Optional[str] = None
        if close < ema and LITE_RSI_SHORT_MIN <= rsi < LITE_RSI_SHORT_MAX:
            side = SIDE_SHORT
        elif close > ema and LITE_RSI_LONG_MIN < rsi <= LITE_RSI_LONG_MAX:
            side = SIDE_LONG

        if side is None:
            continue

        entry_price = close
        risk = LITE_ATR_STOP_MULT * atr
        if side == SIDE_SHORT:
            stop_loss = entry_price + risk
            target = entry_price - LITE_TARGET_R * risk
        else:
            stop_loss = entry_price - risk
            target = entry_price + LITE_TARGET_R * risk

        future = work.iloc[i + 1: i + 1 + FORWARD_BARS].drop(columns=["ts_epoch"])
        outcome_dict = simulate_outcome_with_costs(
            future, entry_price, stop_loss, target, target, side, fee_per_side, slippage
        )
        if outcome_dict.get("outcome") in {"NO_DATA", "INVALID_RISK"}:
            continue

        trade = TradeOutcome(
            symbol=symbol,
            side=side,
            candle_ts=cur_ts,
            entry_price=entry_price,
            stop_loss=stop_loss,
            tp1=target,
            tp2=target,
            score=0.0,
            score_bucket="N/A",
            adx=0.0,
            rsi=rsi,
            fib_zone="N/A",
            regime="EMA_BELOW" if side == SIDE_SHORT else "EMA_ABOVE",
            rr_ratio=LITE_TARGET_R,
            alert_profile="LITE",
            macro_ok=True,
            timing_ok=True,
            outcome=str(outcome_dict["outcome"]),
            exit_price=float(outcome_dict.get("exit_price", entry_price)),
            bars_to_exit=int(outcome_dict.get("bars_to_exit", 0)),
            pnl_r_gross=float(outcome_dict.get("pnl_r_gross", 0.0)),
            pnl_r_net=float(outcome_dict.get("pnl_r_net", 0.0)),
            pnl_pct_gross=float(outcome_dict.get("pnl_pct_gross", 0.0)),
            pnl_pct_net=float(outcome_dict.get("pnl_pct_net", 0.0)),
            is_train=(cur_ts <= train_cutoff_ts),
        )
        trades.append(trade)

    print(f"  [{symbol}] señales={len(trades)}")
    return trades


def print_lite_report(all_trades: List[TradeOutcome], fee: float, slippage: float) -> None:
    sep = "─" * 68
    print(f"\n{sep}")
    print("  REPORTE — SENTINEL LITE (EMA50 + RSI14, 1 timeframe, sin score)")
    print(
        f"  EMA={LITE_EMA_PERIOD} | RSI SHORT=[{LITE_RSI_SHORT_MIN},{LITE_RSI_SHORT_MAX}) "
        f"| RSI LONG=({LITE_RSI_LONG_MIN},{LITE_RSI_LONG_MAX}] (espejo, sin validar) "
        f"| stop={LITE_ATR_STOP_MULT}xATR | target={LITE_TARGET_R}R"
    )
    print(f"  Fees: {fee*100:.3f}% por lado | Slippage: {slippage*100:.3f}% | Walk-fwd: {int(TRAIN_FRACTION*100)}/{100-int(TRAIN_FRACTION*100)}")
    print(sep)

    if not all_trades:
        print("\n  Sin señales generadas en el período evaluado.\n")
        return

    train_trades = [t for t in all_trades if t.is_train]
    test_trades = [t for t in all_trades if not t.is_train]

    full_metrics = compute_metrics(all_trades, use_net=True)
    full_gross = compute_metrics(all_trades, use_net=False)

    print(f"\n  GLOBAL — {full_metrics['total']} señales (in+out)")
    print(f"  {'Métrica':<25} {'Net':>10} {'Gross':>10}")
    print(f"  {'-'*25} {'-'*10} {'-'*10}")
    print(f"  {'Win rate':<25} {full_metrics['win_rate_pct']:>9.1f}% {full_gross['win_rate_pct']:>9.1f}%")
    print(f"  {'Expectancy / signal':<25} {full_metrics['expectancy_r']:>+10.3f}R {full_gross['expectancy_r']:>+9.3f}R")
    print(f"  {'Profit factor':<25} {full_metrics['profit_factor']:>10.2f} {full_gross['profit_factor']:>10.2f}")
    print(f"  {'Avg win':<25} {full_metrics['avg_win_r']:>+10.2f}R {full_gross['avg_win_r']:>+9.2f}R")
    print(f"  {'Avg loss':<25} {full_metrics['avg_loss_r']:>+10.2f}R {full_gross['avg_loss_r']:>+9.2f}R")
    print(f"  {'Max drawdown':<25} {full_metrics['max_drawdown_r']:>10.2f}R {full_gross['max_drawdown_r']:>10.2f}R")
    print(f"  {'Total PnL':<25} {full_metrics['total_pnl_r']:>+10.2f}R {full_gross['total_pnl_r']:>+9.2f}R")
    print(f"  {'Sharpe (simple)':<25} {full_metrics['sharpe_simple']:>10.2f} {full_gross['sharpe_simple']:>10.2f}")
    print(f"  {'Avg bars to exit':<25} {full_metrics['avg_bars_to_exit']:>10.1f} {full_gross['avg_bars_to_exit']:>10.1f}")
    print(f"  Distribución: {full_metrics['winners']}W / {full_metrics['losers']}L / {full_metrics['expired']}E")

    train_metrics = compute_metrics(train_trades, use_net=True) if train_trades else {"total": 0}
    test_metrics = compute_metrics(test_trades, use_net=True) if test_trades else {"total": 0}

    print(f"\n{sep}")
    print(f"  WALK-FORWARD ({int(TRAIN_FRACTION*100)}/{100-int(TRAIN_FRACTION*100)})")
    print(f"  {'Métrica':<25} {'In-sample':>14} {'Out-sample':>14}")
    print(f"  {'-'*25} {'-'*14} {'-'*14}")
    print(f"  {'N':<25} {train_metrics.get('total', 0):>14} {test_metrics.get('total', 0):>14}")
    if train_metrics.get("total", 0) > 0 and test_metrics.get("total", 0) > 0:
        print(f"  {'Win rate':<25} {train_metrics['win_rate_pct']:>13.1f}% {test_metrics['win_rate_pct']:>13.1f}%")
        print(f"  {'Expectancy':<25} {train_metrics['expectancy_r']:>+13.3f}R {test_metrics['expectancy_r']:>+13.3f}R")
        print(f"  {'Profit factor':<25} {train_metrics['profit_factor']:>14.2f} {test_metrics['profit_factor']:>14.2f}")
        if train_metrics["expectancy_r"] != 0:
            degradation = test_metrics["expectancy_r"] / train_metrics["expectancy_r"]
            verdict = "OK" if degradation >= 0.6 else ("DEGRADACIÓN" if degradation >= 0.2 else "OVERFIT")
            print(f"  {'Degradación out/in':<25} {degradation:>14.2f} ({verdict})")

    by_symbol = breakdown_by(all_trades, "symbol")
    print(f"\n{sep}")
    print("  POR ACTIVO")
    print(f"  {'SYM':<6} {'N':>4} {'WR%':>6} {'E[R]':>8} {'PF':>6} {'MaxDD':>8}")
    print(f"  {'-'*5} {'-'*4} {'-'*6} {'-'*8} {'-'*6} {'-'*8}")
    for sym, m in sorted(by_symbol.items(), key=lambda kv: kv[1].get("expectancy_r", 0), reverse=True):
        if m["total"] == 0:
            continue
        print(f"  {sym:<6} {m['total']:>4} {m['win_rate_pct']:>5.1f}% "
              f"{m['expectancy_r']:>+7.3f}R {m['profit_factor']:>6.2f} "
              f"{m['max_drawdown_r']:>7.2f}R")

    by_side = breakdown_by(all_trades, "side")
    print(f"\n{sep}")
    print("  POR SIDE")
    print(f"  {'SIDE':<6} {'N':>4} {'WR%':>6} {'E[R]':>8} {'PF':>6}")
    for side_, m in by_side.items():
        if m["total"] == 0:
            continue
        print(f"  {side_:<6} {m['total']:>4} {m['win_rate_pct']:>5.1f}% "
              f"{m['expectancy_r']:>+7.3f}R {m['profit_factor']:>6.2f}")

    print(f"\n{sep}")
    print(f"  VEREDICTO: {compute_verdict(test_metrics)}")
    print(sep)
    print()


def trades_to_dicts(trades: List[TradeOutcome]) -> List[Dict[str, Any]]:
    return [{**t.__dict__} for t in trades]


def main() -> None:
    parser = argparse.ArgumentParser(description="Sentinel Lite — backtest de 2 indicadores")
    parser.add_argument("--symbol", type=str, default=None, help="Símbolo específico (ej: BTC)")
    parser.add_argument("--months", type=int, default=12, help="Meses de histórico (default 12)")
    parser.add_argument("--fees", type=float, default=DEFAULT_FEE_PER_SIDE, help="Fees por lado")
    parser.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE, help="Slippage")
    parser.add_argument("--output", type=str, default=None, help="Guardar trades a JSON")
    args = parser.parse_args()

    if args.symbol:
        symbols = {k: v for k, v in CRYPTO_IDS.items() if v.upper() == args.symbol.upper()}
        if not symbols:
            print(f"Símbolo '{args.symbol}' no encontrado. Disponibles: {list(CRYPTO_IDS.values())}")
            return
    else:
        symbols = CRYPTO_IDS

    print(f"\nSentinel Lite backtest — {len(symbols)} activo(s) | {args.months}m | "
          f"fees={args.fees*100:.3f}% | slippage={args.slippage*100:.3f}%\n")

    all_trades: List[TradeOutcome] = []
    for cg_id, symbol in symbols.items():
        trades = backtest_lite_symbol(symbol, args.months, args.fees, args.slippage)
        all_trades.extend(trades)

    print_lite_report(all_trades, args.fees, args.slippage)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(trades_to_dicts(all_trades), f, ensure_ascii=False, indent=2, default=str)
        print(f"Trades guardados en: {args.output}")


if __name__ == "__main__":
    main()
