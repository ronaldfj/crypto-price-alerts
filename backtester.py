
"""
backtester.py — backtest simplificado del Crypto Sentinel Bot

Objetivo:
- medir si la versión simplificada genera más oportunidades
- mantener point-in-time correcto
- evaluar expectancy en R como métrica principal
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))

from alert import (
    ALERT_FORWARD_BARS,
    CRYPTO_IDS,
    ENTRY_TIMEFRAME,
    MARKET_CONTEXT_FILE,
    MIN_RR,
    MIN_SCORE,
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


DEFAULT_MONTHS = int(os.getenv("BACKTEST_MONTHS", "12"))
FORWARD_BARS = int(os.getenv("BACKTEST_FORWARD_BARS", str(ALERT_FORWARD_BARS)))
STEP_BARS = int(os.getenv("BACKTEST_STEP_BARS", "1"))
MIN_HISTORY_BARS = int(os.getenv("BACKTEST_MIN_HISTORY", "230"))

DEFAULT_FEE_PER_SIDE = float(os.getenv("BACKTEST_FEE_PER_SIDE", "0.001"))
DEFAULT_SLIPPAGE = float(os.getenv("BACKTEST_SLIPPAGE", "0.0005"))
TRAIN_FRACTION = float(os.getenv("BACKTEST_TRAIN_FRACTION", "0.7"))


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
    adx: float
    rsi: float
    fib_zone: str
    regime: str
    rr_ratio: float
    alert_profile: str
    macro_ok: bool
    timing_ok: bool

    outcome: str = "PENDING"
    exit_price: float = 0.0
    bars_to_exit: int = 0
    pnl_r_gross: float = 0.0
    pnl_r_net: float = 0.0
    pnl_pct_gross: float = 0.0
    pnl_pct_net: float = 0.0
    tp1_hit: bool = False
    tp2_hit: bool = False
    is_train: bool = True


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
    if future_candles is None or future_candles.empty:
        return {"outcome": "NO_DATA"}

    risk = (entry_price - stop_loss) if side == SIDE_LONG else (stop_loss - entry_price)
    if risk <= 0:
        return {"outcome": "INVALID_RISK"}

    for i, row in future_candles.reset_index(drop=True).iterrows():
        low = float(row["Low"])
        high = float(row["High"])
        close = float(row["Close"])
        bar = i + 1

        if side == SIDE_LONG:
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

    last_close = float(future_candles.iloc[-1]["Close"])
    fill = last_close * (1 - slippage) if side == SIDE_LONG else last_close * (1 + slippage)
    return _build_result("EXPIRED", fill, len(future_candles), entry_price, fee_per_side, side, risk)


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
    gross_move = (fill - entry) if side == SIDE_LONG else (entry - fill)
    fee_cost = entry * fee * 2
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


def fetch_symbol_history(symbol: str, months: int) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[pd.DataFrame]]:
    candles_4h = months * 30 * 6 + 300
    seconds_15m = months * 30 * 24 * 4 + 300
    candles_1d = months * 30 + 260

    daily = data_source.fetch_klines(symbol, "1d", candles_1d, drop_unclosed=True)
    fourh = data_source.fetch_klines(symbol, TRADING_TIMEFRAME, candles_4h, drop_unclosed=True)
    entry = data_source.fetch_klines(symbol, ENTRY_TIMEFRAME, seconds_15m, drop_unclosed=True)
    return daily, fourh, entry


def add_epoch_column(df: pd.DataFrame) -> pd.DataFrame:
    clone = df.copy()
    clone["ts_epoch"] = clone["ts"].astype("datetime64[ns, UTC]").astype("int64") // 10**9
    return clone


def window_until(df: pd.DataFrame, ts_epoch: int, min_rows: int) -> Optional[pd.DataFrame]:
    out = df[df["ts_epoch"] <= ts_epoch].copy()
    if len(out) < min_rows:
        return None
    return out.drop(columns=["ts_epoch"]).reset_index(drop=True)


def backtest_symbol(
    symbol: str,
    cg_id: str,
    market_context: Dict[str, Any],
    btc_dominance: Optional[float],
    months: int,
    fee_per_side: float,
    slippage: float,
    min_score_filter: Optional[float] = None,
) -> Tuple[List[TradeOutcome], Dict[str, int]]:
    counters = {
        "evaluated": 0,
        "macro_fail": 0,
        "setup_fail": 0,
        "timing_fail": 0,
        "build_no_alert": 0,
        "exec_gate_fail": 0,
        "user_min_score_fail": 0,
        "passed_full": 0,
        "passed_tactical": 0,
    }

    daily_raw, fourh_raw, entry_raw = fetch_symbol_history(symbol, months)
    if daily_raw is None or fourh_raw is None or entry_raw is None:
        return [], counters

    daily_full = add_epoch_column(daily_raw)
    fourh_full = add_epoch_column(fourh_raw)
    entry_full = add_epoch_column(entry_raw)

    trades: List[TradeOutcome] = []

    train_cutoff_index = int(len(fourh_full) * TRAIN_FRACTION)
    for i in range(max(MIN_HISTORY_BARS, 0), len(fourh_full) - FORWARD_BARS, STEP_BARS):
        cur_ts = int(fourh_full.iloc[i]["ts_epoch"])

        daily_window = window_until(daily_full, cur_ts, 220)
        fourh_window = window_until(fourh_full.iloc[: i + 1], cur_ts, 220)
        entry_window = window_until(entry_full, cur_ts, 60)

        if daily_window is None or fourh_window is None or entry_window is None:
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
            sim_current_price = float(candidate["entry_price"])
            candidate = apply_execution_quality_gate(candidate, sim_current_price)

            if not candidate["alert"]:
                if candidate.get("execution_state") in {"INVALID_NOW", "LATE"}:
                    counters["exec_gate_fail"] += 1
                else:
                    counters["build_no_alert"] += 1
                continue

            if min_score_filter is not None and float(candidate["score"]) < min_score_filter:
                counters["user_min_score_fail"] += 1
                continue

            if candidate["alert_profile"] == "FULL":
                counters["passed_full"] += 1
            else:
                counters["passed_tactical"] += 1

            future = fourh_full.iloc[i + 1 : i + 1 + FORWARD_BARS].drop(columns=["ts_epoch"]).copy()
            outcome = simulate_outcome_with_costs(
                future,
                float(candidate["entry_price"]),
                float(candidate["stop_loss"]),
                float(candidate.get("tp1", candidate["entry_price"])),
                float(candidate.get("tp2", candidate.get("take_profit", candidate["entry_price"]))),
                side,
                fee_per_side,
                slippage,
            )
            if outcome.get("outcome") in {"NO_DATA", "INVALID_RISK"}:
                continue

            trade = TradeOutcome(
                symbol=symbol,
                side=side,
                candle_ts=cur_ts,
                entry_price=float(candidate["entry_price"]),
                stop_loss=float(candidate["stop_loss"]),
                tp1=float(candidate["tp1"]),
                tp2=float(candidate["tp2"]),
                score=float(candidate["score"]),
                adx=float(candidate["adx"]),
                rsi=float(candidate["rsi"]),
                fib_zone=str(candidate["fib_zone"]),
                regime=str(candidate["regime"]),
                rr_ratio=float(candidate["rr_ratio"]),
                alert_profile=str(candidate.get("alert_profile", "FULL")),
                macro_ok=bool(candidate.get("macro_ok", False)),
                timing_ok=bool(candidate.get("timing_ok", False)),
                outcome=str(outcome["outcome"]),
                exit_price=float(outcome.get("exit_price", candidate["entry_price"])),
                bars_to_exit=int(outcome.get("bars_to_exit", 0)),
                pnl_r_gross=float(outcome.get("pnl_r_gross", 0.0)),
                pnl_r_net=float(outcome.get("pnl_r_net", 0.0)),
                pnl_pct_gross=float(outcome.get("pnl_pct_gross", 0.0)),
                pnl_pct_net=float(outcome.get("pnl_pct_net", 0.0)),
                tp1_hit=bool(outcome.get("tp1_hit", False)),
                tp2_hit=bool(outcome.get("tp2_hit", False)),
                is_train=(i < train_cutoff_index),
            )
            trades.append(trade)

    return trades, counters


def summarize_trades(trades: List[TradeOutcome]) -> Dict[str, Any]:
    if not trades:
        return {
            "trades": 0,
            "win_rate_pct": 0.0,
            "tp2_rate_pct": 0.0,
            "avg_r_net": 0.0,
            "expectancy_r": 0.0,
            "profit_factor": 0.0,
            "avg_bars": 0.0,
        }

    pnl = [t.pnl_r_net for t in trades]
    winners = [x for x in pnl if x > 0]
    losers = [x for x in pnl if x <= 0]
    tp2_hits = sum(1 for t in trades if t.outcome == "TP2_HIT")
    bars = [t.bars_to_exit for t in trades]

    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))

    return {
        "trades": len(trades),
        "win_rate_pct": round((sum(1 for x in pnl if x > 0) / len(trades)) * 100, 2),
        "tp2_rate_pct": round((tp2_hits / len(trades)) * 100, 2),
        "avg_r_net": round(sum(pnl) / len(trades), 4),
        "expectancy_r": round(sum(pnl) / len(trades), 4),
        "profit_factor": round((gross_profit / gross_loss), 3) if gross_loss > 0 else math.inf,
        "avg_bars": round(sum(bars) / len(bars), 2),
    }


def slice_train_test(trades: List[TradeOutcome]) -> Tuple[List[TradeOutcome], List[TradeOutcome]]:
    train = [t for t in trades if t.is_train]
    test = [t for t in trades if not t.is_train]
    return train, test


def group_stats(trades: List[TradeOutcome], attr: str) -> Dict[str, Any]:
    buckets: Dict[str, List[TradeOutcome]] = {}
    for trade in trades:
        key = str(getattr(trade, attr))
        buckets.setdefault(key, []).append(trade)

    out: Dict[str, Any] = {}
    for key, group in sorted(buckets.items()):
        out[key] = summarize_trades(group)
    return out


def counters_to_dict(counters: Dict[str, int]) -> Dict[str, int]:
    total = max(counters.get("evaluated", 0), 1)
    enriched = dict(counters)
    enriched["pass_rate_pct"] = round(
        ((counters.get("passed_full", 0) + counters.get("passed_tactical", 0)) / total) * 100,
        2,
    )
    return enriched


def run_backtest(
    symbols: List[str],
    months: int,
    fee_per_side: float,
    slippage: float,
    min_score_filter: Optional[float] = None,
) -> Dict[str, Any]:
    market_context = load_market_context(MARKET_CONTEXT_FILE)
    btc_dominance = fetch_btc_dominance()

    all_trades: List[TradeOutcome] = []
    by_symbol: Dict[str, Any] = {}
    filter_counters: Dict[str, Dict[str, int]] = {}

    for symbol in symbols:
        cg_id = next((k for k, v in CRYPTO_IDS.items() if v == symbol), "")
        print(f"[{symbol}] backtest...")
        trades, counters = backtest_symbol(
            symbol=symbol,
            cg_id=cg_id,
            market_context=market_context,
            btc_dominance=btc_dominance,
            months=months,
            fee_per_side=fee_per_side,
            slippage=slippage,
            min_score_filter=min_score_filter,
        )
        all_trades.extend(trades)
        filter_counters[symbol] = counters_to_dict(counters)

        train, test = slice_train_test(trades)
        by_symbol[symbol] = {
            "summary": summarize_trades(trades),
            "train": summarize_trades(train),
            "test": summarize_trades(test),
            "by_side": group_stats(trades, "side"),
            "by_profile": group_stats(trades, "alert_profile"),
            "by_regime": group_stats(trades, "regime"),
            "by_fib_zone": group_stats(trades, "fib_zone"),
        }

    train_all, test_all = slice_train_test(all_trades)

    return {
        "config": {
            "months": months,
            "forward_bars": FORWARD_BARS,
            "step_bars": STEP_BARS,
            "min_score": MIN_SCORE,
            "min_rr": MIN_RR,
            "fee_per_side": fee_per_side,
            "slippage": slippage,
            "user_min_score_filter": min_score_filter,
        },
        "portfolio": {
            "summary": summarize_trades(all_trades),
            "train": summarize_trades(train_all),
            "test": summarize_trades(test_all),
            "by_side": group_stats(all_trades, "side"),
            "by_profile": group_stats(all_trades, "alert_profile"),
            "by_regime": group_stats(all_trades, "regime"),
            "by_fib_zone": group_stats(all_trades, "fib_zone"),
        },
        "symbols": by_symbol,
        "filter_counters": filter_counters,
        "sample_trades": [asdict(t) for t in all_trades[:30]],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtester del Crypto Sentinel simplificado")
    parser.add_argument("--symbol", type=str, default="", help="Símbolo único, ej. BTC")
    parser.add_argument("--months", type=int, default=DEFAULT_MONTHS)
    parser.add_argument("--fees", type=float, default=DEFAULT_FEE_PER_SIDE, help="Fee por lado")
    parser.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE)
    parser.add_argument("--min-score", type=float, default=None, help="Filtro adicional de score en backtest")
    parser.add_argument("--output", type=str, default="", help="Ruta JSON de salida")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.symbol:
        symbols = [args.symbol.upper()]
    else:
        symbols = list(CRYPTO_IDS.values())

    results = run_backtest(
        symbols=symbols,
        months=args.months,
        fee_per_side=args.fees,
        slippage=args.slippage,
        min_score_filter=args.min_score,
    )

    portfolio = results["portfolio"]["summary"]
    print("")
    print("=== RESUMEN PORTAFOLIO ===")
    print(json.dumps(portfolio, indent=2, ensure_ascii=False))
    print("")
    print("=== CONTADORES DE FILTRO ===")
    print(json.dumps(results["filter_counters"], indent=2, ensure_ascii=False))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=2, ensure_ascii=False)
        print(f"Resultados guardados en {args.output}")


if __name__ == "__main__":
    main()
