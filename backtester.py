"""
backtester.py — v3.0 — Backtesting histórico del Crypto Sentinel Bot

Cambios vs v2:
- Datos point-in-time correctos: en cada paso, daily/4H/15m se truncan al
  timestamp del 4H simulado (no usa "última vela cerrada al momento de
  ejecutar el script").
- Fees y slippage parametrizables (default Bybit Spot 0.1% maker/taker, 0.05% slippage).
- Reportes desglosados: por activo, side, régimen, score bucket, fib zone.
- Walk-forward 70/30 train/test para detectar overfit.
- Expectancy en R como métrica primaria, no win rate.
- Métrica de "señales descartadas por filtro" para diagnosticar si filtros matan winners.

Uso:
    python backtester.py                              # 12m, todos los activos
    python backtester.py --symbol BTC --months 6
    python backtester.py --fees 0.001 --slippage 0.0005
    python backtester.py --output results.json --report-only winners

Requiere acceso a Bybit/OKX vía data_source.py.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from alert import (
    CRYPTO_IDS,
    ENTRY_TIMEFRAME,
    MARKET_CONTEXT_FILE,
    MIN_SCORE,
    MIN_RR,
    SIDE_LONG,
    SIDE_SHORT,
    TRADING_TIMEFRAME,
    apply_execution_quality_gate,
    build_candidate,
    evaluate_macro_confirmation,
    evaluate_setup_confirmation,
    evaluate_timing_confirmation,
    fetch_btc_dominance,
    load_market_context,
    normalize_context,
    parse_allowed_sides,
)
import data_source


# ── Configuración del backtester ──────────────────────────────────────────────
DEFAULT_MONTHS = int(os.getenv("BACKTEST_MONTHS", "24"))
FORWARD_BARS = int(os.getenv("BACKTEST_FORWARD_BARS", "24"))   # 24 × 4H = 96h
STEP_BARS = int(os.getenv("BACKTEST_STEP_BARS", "1"))
MIN_HISTORY_BARS = int(os.getenv("BACKTEST_MIN_HISTORY", "230"))

# Costos: defaults Bybit Spot.
DEFAULT_FEE_PER_SIDE = float(os.getenv("BACKTEST_FEE_PER_SIDE", "0.001"))   # 0.1%
DEFAULT_SLIPPAGE = float(os.getenv("BACKTEST_SLIPPAGE", "0.0005"))          # 0.05%

# Walk-forward: fracción del rango asignada a "train" (in-sample).
TRAIN_FRACTION = float(os.getenv("BACKTEST_TRAIN_FRACTION", "0.7"))


# ── Estructuras ───────────────────────────────────────────────────────────────
@dataclass
class TradeOutcome:
    symbol: str
    side: str
    candle_ts: int
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    score: float
    score_bucket: str
    adx: float
    rsi: float
    fib_zone: str
    regime: str
    rr_ratio: float
    alert_profile: str
    macro_ok: bool
    timing_ok: bool
    vwap_distance_pct: float = 0.0
    above_vwap: bool = False
    volume_strong: bool = False
    volume_divergence: bool = False

    outcome: str = "PENDING"           # TP1_HIT | TP2_HIT | SL_HIT | EXPIRED
    exit_price: float = 0.0
    bars_to_exit: int = 0
    pnl_r_gross: float = 0.0           # En R, sin fees ni slippage
    pnl_r_net: float = 0.0             # Tras fees y slippage
    pnl_pct_gross: float = 0.0
    pnl_pct_net: float = 0.0
    is_train: bool = True              # Para walk-forward


# ── Simulación de outcome con costos ──────────────────────────────────────────
def simulate_outcome_with_costs(
    future_candles: pd.DataFrame,
    entry_price: float,
    stop_loss: float,
    tp1: float,
    tp2: float,
    side: str,
    fee_per_side: float,
    slippage: float,
) -> Dict[str, Any]:
    """
    Recorre velas futuras y determina qué tocó primero. Aplica:
    - Slippage en el fill: SL fill = stop * (1 + slippage) para LONG (peor),
      TP fill = tp * (1 - slippage) para LONG (peor).
    - Fees por lado en entry y exit.

    Convención conservadora: en una vela que cubre tanto SL como TP, asume SL
    primero (peor caso para el trader, estándar en backtesting de OHLC).
    """
    if future_candles is None or future_candles.empty:
        return {"outcome": "NO_DATA"}

    # Para LONG: risk = entry - stop (positivo). Para SHORT: stop - entry.
    if side == SIDE_LONG:
        risk = entry_price - stop_loss
    else:
        risk = stop_loss - entry_price

    if risk <= 0:
        return {"outcome": "INVALID_RISK"}

    for i, row in future_candles.reset_index(drop=True).iterrows():
        low = float(row["Low"])
        high = float(row["High"])
        close = float(row["Close"])
        bar = i + 1

        if side == SIDE_LONG:
            # SL primero (conservador) — slippage empeora el fill.
            if low <= stop_loss:
                fill = stop_loss * (1 - slippage)
                return _build_result("SL_HIT", fill, bar, entry_price, fee_per_side, side, risk)
            if high >= tp2:
                fill = tp2 * (1 - slippage)
                return _build_result("TP2_HIT", fill, bar, entry_price, fee_per_side, side, risk, tp1_hit=True, tp2_hit=True)
            if high >= tp1:
                fill = tp1 * (1 - slippage)
                return _build_result("TP1_HIT", fill, bar, entry_price, fee_per_side, side, risk, tp1_hit=True)
        else:
            if high >= stop_loss:
                fill = stop_loss * (1 + slippage)
                return _build_result("SL_HIT", fill, bar, entry_price, fee_per_side, side, risk)
            if low <= tp2:
                fill = tp2 * (1 + slippage)
                return _build_result("TP2_HIT", fill, bar, entry_price, fee_per_side, side, risk, tp1_hit=True, tp2_hit=True)
            if low <= tp1:
                fill = tp1 * (1 + slippage)
                return _build_result("TP1_HIT", fill, bar, entry_price, fee_per_side, side, risk, tp1_hit=True)

    # Tiempo agotado: cierre al close de la última vela.
    last_close = float(future_candles.iloc[-1]["Close"])
    fill = last_close * (1 - slippage) if side == SIDE_LONG else last_close * (1 + slippage)
    return _build_result(
        "EXPIRED",
        fill,
        len(future_candles),
        entry_price,
        fee_per_side,
        side,
        risk,
    )


def _build_result(
    outcome: str,
    fill: float,
    bars: int,
    entry: float,
    fee: float,
    side: str,
    risk: float,
    tp1_hit: bool = False,
    tp2_hit: bool = False,
) -> Dict[str, Any]:
    if side == SIDE_LONG:
        gross_move = fill - entry
    else:
        gross_move = entry - fill

    # Fees: 2 lados, sobre el notional (~entry+fill, aproximamos con entry).
    fee_cost = entry * fee * 2  # round-trip
    net_move = gross_move - fee_cost

    pnl_r_gross = gross_move / risk
    pnl_r_net = net_move / risk
    pnl_pct_gross = (gross_move / entry) * 100
    pnl_pct_net = (net_move / entry) * 100

    return {
        "outcome": outcome,
        "exit_price": fill,
        "bars_to_exit": bars,
        "pnl_r_gross": round(pnl_r_gross, 4),
        "pnl_r_net": round(pnl_r_net, 4),
        "pnl_pct_gross": round(pnl_pct_gross, 4),
        "pnl_pct_net": round(pnl_pct_net, 4),
        "tp1_hit": tp1_hit,
        "tp2_hit": tp2_hit,
        "fee_cost_pct": round((fee_cost / entry) * 100, 4),
    }


def score_bucket(score: float) -> str:
    if score < 5.0:
        return "<5.0"
    if score < 6.0:
        return "5.0-6.0"
    if score < 7.0:
        return "6.0-7.0"
    if score < 8.0:
        return "7.0-8.0"
    if score < 9.0:
        return "8.0-9.0"
    return ">=9.0"


# ── Backtest por activo ───────────────────────────────────────────────────────
def backtest_symbol(
    symbol: str,
    cg_id: str,
    market_context: Dict[str, Any],
    btc_dominance: Optional[float],
    months: int,
    fee_per_side: float,
    slippage: float,
    min_score_filter: Optional[float] = None,  # DEPRECATED: usar MIN_SCORE env var
) -> Tuple[List[TradeOutcome], Dict[str, int]]:
    """
    Devuelve (lista_de_trades, contadores_de_filtros).
    Los contadores explican cuántas señales se descartaron en cada filtro,
    útil para diagnosticar si los filtros están matando winners.
    """
    print(f"  [{symbol}] Descargando {months}m de datos...")
    counters: Dict[str, int] = {
        "evaluated": 0, "macro_fail": 0, "setup_fail": 0, "timing_fail": 0,
        "build_no_alert": 0, "exec_gate_fail": 0, "min_score_user_filter": 0,
        "passed_full": 0, "passed_tactical": 0,
    }

    # Cuántas velas históricas necesitamos: months × 30 días × 6 velas 4H + buffer.
    candles_4h = months * 30 * 6 + MIN_HISTORY_BARS
    candles_4h = min(candles_4h, 1000)  # Bybit cap por call. Suficiente para 6 meses; >6m requiere paginación.
    # Para >6m usamos fetch_klines_range.
    now_ts = int(time.time())
    range_start = now_ts - months * 30 * 86400

    daily_full = data_source.fetch_klines_range(symbol, "1d", range_start - 365 * 86400, now_ts)
    fourh_full = data_source.fetch_klines_range(symbol, TRADING_TIMEFRAME, range_start - 60 * 86400, now_ts)
    # 15m: descargamos todo lo disponible. Bybit devuelve máx ~10k velas (≈104 días).
    # El backtester usa ventanas de 96 velas 15m, así que solo las evaluaciones
    # dentro del rango cubierto tendrán timing válido — el resto se salta limpiamente.
    entry_full = data_source.fetch_klines_range(symbol, ENTRY_TIMEFRAME, range_start - 30 * 86400, now_ts)

    if daily_full is None or len(daily_full) < MIN_HISTORY_BARS:
        print(f"  [{symbol}] Daily insuficiente ({len(daily_full) if daily_full is not None else 0}).")
        return [], counters
    if fourh_full is None or len(fourh_full) < MIN_HISTORY_BARS:
        print(f"  [{symbol}] 4H insuficiente ({len(fourh_full) if fourh_full is not None else 0}).")
        return [], counters
    if entry_full is None or len(entry_full) < 60:
        print(f"  [{symbol}] 15m insuficiente ({len(entry_full) if entry_full is not None else 0}).")
        return [], counters

    print(f"  [{symbol}] daily={len(daily_full)} | 4H={len(fourh_full)} | 15m={len(entry_full)}")

    # Pre-cómputo de epochs para slicing rápido.
    fourh_full = fourh_full.copy().reset_index(drop=True)
    fourh_full["ts_epoch"] = fourh_full["ts"].astype("datetime64[ns, UTC]").astype("int64") // 10**9
    daily_full = daily_full.copy()
    daily_full["ts_epoch"] = daily_full["ts"].astype("datetime64[ns, UTC]").astype("int64") // 10**9
    entry_full = entry_full.copy()
    entry_full["ts_epoch"] = entry_full["ts"].astype("datetime64[ns, UTC]").astype("int64") // 10**9

    # Punto de inicio: la primera vela 4H que tiene MIN_HISTORY_BARS atrás.
    start_idx = MIN_HISTORY_BARS
    end_idx = len(fourh_full) - FORWARD_BARS  # Necesitamos velas futuras para outcome
    if end_idx <= start_idx:
        print(f"  [{symbol}] Ventana insuficiente para slide.")
        return [], counters

    # Walk-forward: separar por timestamp, no por índice.
    # 70% del rango temporal = in-sample, 30% = out-of-sample.
    first_ts = int(fourh_full.iloc[start_idx]["ts_epoch"])
    last_ts = int(fourh_full.iloc[end_idx - 1]["ts_epoch"])
    train_cutoff_ts = first_ts + int((last_ts - first_ts) * TRAIN_FRACTION)

    print(f"  [{symbol}] slide: {start_idx}-{end_idx} ({end_idx - start_idx} pasos) | "
          f"train hasta ts={train_cutoff_ts}")

    trades: List[TradeOutcome] = []
    bar_seconds = 14400  # 4H
    daily_seconds = 86400
    intraday_seconds = 900  # 15m

    for i in range(start_idx, end_idx, STEP_BARS):
        # Vela 4H actual (cerrada): la que cierra en t = ts[i] + bar_seconds.
        # Para simular "vela cerrada en este momento" pedimos hasta iloc[:i+1] inclusivo,
        # porque ts[i] es el OPEN time, y la vela cerró ts[i] + bar_seconds.
        cur_ts = int(fourh_full.iloc[i]["ts_epoch"])  # open time de la vela actual
        cur_close_ts = cur_ts + bar_seconds  # close time

        # Slicing point-in-time:
        # - 4H: hasta i incluido (la vela i ya cerró en cur_close_ts)
        fourh_window = fourh_full.iloc[: i + 1].drop(columns=["ts_epoch"]).copy()
        # - Daily: solo velas cuyo close_time <= cur_close_ts
        daily_window = daily_full[daily_full["ts_epoch"] + daily_seconds <= cur_close_ts]
        daily_window = daily_window.drop(columns=["ts_epoch"]).copy()
        # - 15m: ventana de 96 velas terminando en cur_close_ts
        entry_window = entry_full[entry_full["ts_epoch"] + intraday_seconds <= cur_close_ts]
        entry_window = entry_window.tail(96).drop(columns=["ts_epoch"]).copy()

        if len(daily_window) < 220 or len(fourh_window) < 220 or len(entry_window) < 30:
            continue

        normalized_context = normalize_context(market_context, symbol)
        if btc_dominance is not None:
            normalized_context["btc_dominance"] = btc_dominance

        allowed_sides = parse_allowed_sides(normalized_context)

        for side in allowed_sides:
            counters["evaluated"] += 1

            macro_eval = evaluate_macro_confirmation(daily_window, symbol, normalized_context, side=side)
            if macro_eval is None:
                counters["macro_fail"] += 1
                continue

            setup_eval = evaluate_setup_confirmation(fourh_window, symbol, cg_id, side=side)
            if setup_eval is None:
                counters["setup_fail"] += 1
                continue

            timing_eval = evaluate_timing_confirmation(entry_window, symbol, side=side)
            if timing_eval is None:
                counters["timing_fail"] += 1
                continue

            candidate = build_candidate(symbol, cg_id, macro_eval, setup_eval, timing_eval)
            # Para backtest no aplicamos execution gate sobre precio actual:
            # el "precio actual" simulado es el close de la vela 4H (entry).

            if not candidate["alert"]:
                counters["build_no_alert"] += 1
                continue

            if min_score_filter is not None and candidate["score"] < min_score_filter:
                counters["min_score_user_filter"] += 1
                continue

            if candidate["alert_profile"] == "FULL":
                counters["passed_full"] += 1
            elif candidate["alert_profile"] == "TACTICAL":
                counters["passed_tactical"] += 1

            # Simulación
            entry_price = float(candidate["entry_price"])
            stop_loss = float(candidate["stop_loss"])
            tp1 = float(candidate.get("tp1", entry_price))
            tp2 = float(candidate.get("tp2", candidate.get("take_profit", tp1)))

            future = fourh_full.iloc[i + 1: i + 1 + FORWARD_BARS].drop(columns=["ts_epoch"]).copy()
            outcome_dict = simulate_outcome_with_costs(
                future, entry_price, stop_loss, tp1, tp2, side, fee_per_side, slippage
            )

            if outcome_dict.get("outcome") in {"NO_DATA", "INVALID_RISK"}:
                continue

            trade = TradeOutcome(
                symbol=symbol,
                side=side,
                candle_ts=cur_ts,
                entry_price=entry_price,
                stop_loss=stop_loss,
                tp1=tp1,
                tp2=tp2,
                score=float(candidate["score"]),
                score_bucket=score_bucket(float(candidate["score"])),
                adx=float(candidate["adx"]),
                rsi=float(candidate["rsi"]),
                fib_zone=str(candidate["fib_zone"]),
                regime=str(candidate["regime"]),
                rr_ratio=float(candidate["rr_ratio"]),
                alert_profile=str(candidate.get("alert_profile", "FULL")),
                macro_ok=bool(candidate.get("macro_ok", False)),
                timing_ok=bool(candidate.get("timing_ok", False)),
                vwap_distance_pct=float(candidate.get("vwap_distance_pct", 0.0)),
                above_vwap=bool(candidate.get("above_vwap", False)),
                volume_strong=bool(candidate.get("volume_strong", False)),
                volume_divergence=bool(candidate.get("volume_divergence", False)),
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

    print(f"  [{symbol}] Evaluadas={counters['evaluated']} | señales={len(trades)}")
    return trades, counters


# ── Métricas ──────────────────────────────────────────────────────────────────
def compute_metrics(trades: List[TradeOutcome], use_net: bool = True) -> Dict[str, Any]:
    if not trades:
        return {"total": 0}

    pnl_attr = "pnl_r_net" if use_net else "pnl_r_gross"

    total = len(trades)
    winners = [t for t in trades if t.outcome in {"TP1_HIT", "TP2_HIT"}]
    losers = [t for t in trades if t.outcome == "SL_HIT"]
    expired = [t for t in trades if t.outcome == "EXPIRED"]

    win_rate = len(winners) / total * 100
    expectancy_r = sum(getattr(t, pnl_attr) for t in trades) / total

    gross_profit_r = sum(getattr(t, pnl_attr) for t in trades if getattr(t, pnl_attr) > 0)
    gross_loss_r = abs(sum(getattr(t, pnl_attr) for t in trades if getattr(t, pnl_attr) < 0))
    profit_factor = gross_profit_r / gross_loss_r if gross_loss_r > 0 else float("inf")

    avg_win = sum(getattr(t, pnl_attr) for t in winners) / len(winners) if winners else 0.0
    avg_loss = sum(getattr(t, pnl_attr) for t in losers) / len(losers) if losers else 0.0

    # Drawdown sobre PnL acumulado en R
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cumulative += getattr(t, pnl_attr)
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    avg_bars = sum(t.bars_to_exit for t in trades) / total

    # Sharpe simplificado: expectancy / desviación estándar de PnL en R
    pnl_values = [getattr(t, pnl_attr) for t in trades]
    if len(pnl_values) > 1:
        mean = sum(pnl_values) / len(pnl_values)
        var = sum((x - mean) ** 2 for x in pnl_values) / (len(pnl_values) - 1)
        std = math.sqrt(var)
        sharpe_simple = expectancy_r / std if std > 0 else 0.0
    else:
        sharpe_simple = 0.0

    return {
        "total": total,
        "winners": len(winners),
        "losers": len(losers),
        "expired": len(expired),
        "win_rate_pct": round(win_rate, 1),
        "expectancy_r": round(expectancy_r, 3),
        "profit_factor": round(profit_factor, 2),
        "avg_win_r": round(avg_win, 2),
        "avg_loss_r": round(avg_loss, 2),
        "max_drawdown_r": round(max_dd, 2),
        "total_pnl_r": round(sum(pnl_values), 2),
        "avg_bars_to_exit": round(avg_bars, 1),
        "sharpe_simple": round(sharpe_simple, 2),
    }


def breakdown_by(trades: List[TradeOutcome], key: str, use_net: bool = True) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[TradeOutcome]] = {}
    for t in trades:
        k = str(getattr(t, key, "?"))
        groups.setdefault(k, []).append(t)
    return {k: compute_metrics(v, use_net=use_net) for k, v in groups.items()}


# ── Reporte ───────────────────────────────────────────────────────────────────
def print_report(
    all_trades: List[TradeOutcome],
    counters_total: Dict[str, int],
    fee: float,
    slippage: float,
) -> None:
    sep = "─" * 68

    print(f"\n{sep}")
    print("  REPORTE DE BACKTESTING — CRYPTO SENTINEL v3.0")
    print(f"  Fees: {fee*100:.3f}% por lado | Slippage: {slippage*100:.3f}% | Walk-fwd: {int(TRAIN_FRACTION*100)}/{100-int(TRAIN_FRACTION*100)}")
    print(sep)

    if not all_trades:
        print("\n  Sin señales generadas en el período evaluado.")
        print("  Diagnóstico de filtros (acumulado todos los activos):")
        for k, v in counters_total.items():
            print(f"    {k}: {v}")
        print()
        return

    train_trades = [t for t in all_trades if t.is_train]
    test_trades = [t for t in all_trades if not t.is_train]

    # ── Globales ──
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

    # ── Walk-forward in-sample vs out-of-sample ──
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
        # Ratio de degradación: out/in. Si <0.5 hay sobreajuste claro.
        if train_metrics["expectancy_r"] != 0:
            degradation = test_metrics["expectancy_r"] / train_metrics["expectancy_r"]
            verdict = "OK" if degradation >= 0.6 else ("DEGRADACIÓN" if degradation >= 0.2 else "OVERFIT")
            print(f"  {'Degradación out/in':<25} {degradation:>14.2f} ({verdict})")

    # ── Por activo ──
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

    # ── Por side ──
    by_side = breakdown_by(all_trades, "side")
    print(f"\n{sep}")
    print("  POR SIDE")
    print(f"  {'SIDE':<6} {'N':>4} {'WR%':>6} {'E[R]':>8} {'PF':>6}")
    for side_, m in by_side.items():
        if m["total"] == 0:
            continue
        print(f"  {side_:<6} {m['total']:>4} {m['win_rate_pct']:>5.1f}% "
              f"{m['expectancy_r']:>+7.3f}R {m['profit_factor']:>6.2f}")

    # ── Por régimen ──
    by_regime = breakdown_by(all_trades, "regime")
    print(f"\n{sep}")
    print("  POR RÉGIMEN 4H")
    print(f"  {'REGIMEN':<14} {'N':>4} {'WR%':>6} {'E[R]':>8} {'PF':>6}")
    for reg, m in sorted(by_regime.items(), key=lambda kv: kv[1].get("expectancy_r", 0), reverse=True):
        if m["total"] == 0:
            continue
        print(f"  {reg:<14} {m['total']:>4} {m['win_rate_pct']:>5.1f}% "
              f"{m['expectancy_r']:>+7.3f}R {m['profit_factor']:>6.2f}")

    # ── Por score bucket — el más diagnóstico ──
    by_score = breakdown_by(all_trades, "score_bucket")
    print(f"\n{sep}")
    print("  POR SCORE BUCKET (diagnóstico de si el score discrimina)")
    print(f"  {'BUCKET':<10} {'N':>4} {'WR%':>6} {'E[R]':>8} {'PF':>6}")
    bucket_order = ["<5.0", "5.0-6.0", "6.0-7.0", "7.0-8.0", "8.0-9.0", ">=9.0"]
    for bucket in bucket_order:
        m = by_score.get(bucket)
        if not m or m["total"] == 0:
            continue
        print(f"  {bucket:<10} {m['total']:>4} {m['win_rate_pct']:>5.1f}% "
              f"{m['expectancy_r']:>+7.3f}R {m['profit_factor']:>6.2f}")
    print("  ↑ Si E[R] no crece con bucket, el score no aporta edge.")

    # ── Por fib zone ──
    by_fib = breakdown_by(all_trades, "fib_zone")
    print(f"\n{sep}")
    print("  POR ZONA FIBONACCI")
    print(f"  {'ZONA':<14} {'N':>4} {'WR%':>6} {'E[R]':>8} {'PF':>6}")
    for zone, m in sorted(by_fib.items(), key=lambda kv: kv[1].get("expectancy_r", 0), reverse=True):
        if m["total"] == 0:
            continue
        print(f"  {zone:<14} {m['total']:>4} {m['win_rate_pct']:>5.1f}% "
              f"{m['expectancy_r']:>+7.3f}R {m['profit_factor']:>6.2f}")

    # ── Por perfil de alerta ──
    by_profile = breakdown_by(all_trades, "alert_profile")
    print(f"\n{sep}")
    print("  POR PERFIL DE ALERTA")
    print(f"  {'PROFILE':<10} {'N':>4} {'WR%':>6} {'E[R]':>8} {'PF':>6}")
    for prof, m in by_profile.items():
        if m["total"] == 0:
            continue
        print(f"  {prof:<10} {m['total']:>4} {m['win_rate_pct']:>5.1f}% "
              f"{m['expectancy_r']:>+7.3f}R {m['profit_factor']:>6.2f}")

    # ── Diagnóstico de filtros ──
    print(f"\n{sep}")
    print("  DIAGNÓSTICO DE FILTROS (cuántas señales descartó cada capa)")
    for k, v in counters_total.items():
        print(f"  {k:<28} {v}")
    if counters_total.get("evaluated", 0) > 0:
        survival = (counters_total.get("passed_full", 0) + counters_total.get("passed_tactical", 0)) / counters_total["evaluated"] * 100
        print(f"  → tasa de supervivencia: {survival:.2f}% de evaluaciones devienen señal")

    # ── Veredicto ──
    print(f"\n{sep}")
    expectancy_net = full_metrics["expectancy_r"]
    pf_net = full_metrics["profit_factor"]
    if expectancy_net >= 0.15 and pf_net >= 1.4:
        verdict = "✅ EDGE POSITIVO NETO — sigue validando out-of-sample"
    elif expectancy_net >= 0.05:
        verdict = "🟡 EDGE MARGINAL — revisar componentes que no aportan"
    elif expectancy_net >= -0.05:
        verdict = "⚠️  BREAKEVEN — el sistema no tiene edge demostrable"
    else:
        verdict = "❌ EDGE NEGATIVO — no operar; replantear estrategia"
    print(f"  VEREDICTO: {verdict}")
    print(sep)
    print()


# ── Exportar a JSON ───────────────────────────────────────────────────────────
def trades_to_dicts(trades: List[TradeOutcome]) -> List[Dict[str, Any]]:
    return [
        {
            **t.__dict__,
        }
        for t in trades
    ]


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Backtester v3.0 — Crypto Sentinel Bot")
    parser.add_argument("--symbol", type=str, default=None, help="Símbolo específico (ej: BTC)")
    parser.add_argument("--months", type=int, default=DEFAULT_MONTHS, help="Meses de histórico (default 12)")
    parser.add_argument("--fees", type=float, default=DEFAULT_FEE_PER_SIDE, help="Fees por lado (0.001 = 0.1%%)")
    parser.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE, help="Slippage (0.0005 = 0.05%%)")
    parser.add_argument("--output", type=str, default=None, help="Guardar trades a JSON")
    parser.add_argument("--gross-only", action="store_true", help="Reportar sólo gross (sin fees)")
    args = parser.parse_args()

    market_context = load_market_context(MARKET_CONTEXT_FILE)
    btc_dominance = fetch_btc_dominance()
    if btc_dominance is not None:
        print(f"BTC Dominance: {btc_dominance:.1f}%")

    if args.symbol:
        symbols = {k: v for k, v in CRYPTO_IDS.items() if v.upper() == args.symbol.upper()}
        if not symbols:
            print(f"Símbolo '{args.symbol}' no encontrado. Disponibles: {list(CRYPTO_IDS.values())}")
            return
    else:
        symbols = CRYPTO_IDS

    print(f"\nBacktest {len(symbols)} activo(s) | {args.months}m | "
          f"fees={args.fees*100:.3f}% | slippage={args.slippage*100:.3f}%\n")

    all_trades: List[TradeOutcome] = []
    counters_total: Dict[str, int] = {}

    for cg_id, symbol in symbols.items():
        trades, counters = backtest_symbol(
            symbol=symbol,
            cg_id=cg_id,
            market_context=market_context,
            btc_dominance=btc_dominance,
            months=args.months,
            fee_per_side=args.fees,
            slippage=args.slippage,
        )
        all_trades.extend(trades)
        for k, v in counters.items():
            counters_total[k] = counters_total.get(k, 0) + v

    print_report(all_trades, counters_total, args.fees, args.slippage)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(trades_to_dicts(all_trades), f, ensure_ascii=False, indent=2, default=str)
        print(f"Trades guardados en: {args.output}")


if __name__ == "__main__":
    main()
