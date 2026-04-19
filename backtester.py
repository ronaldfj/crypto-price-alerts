import argparse
from dataclasses import dataclass, asdict
from typing import Any, Dict, List

import pandas as pd

from alert import (
    CRYPTO_IDS,
    DAILY_LOOKBACK_DAYS,
    HOURLY_LOOKBACK_DAYS,
    INTRADAY_LOOKBACK_DAYS,
    HOURLY_INTERVAL,
    TRADING_TIMEFRAME,
    ENTRY_TIMEFRAME,
    build_ohlcv_from_frame,
    build_candidate,
    evaluate_macro_confirmation,
    evaluate_setup_confirmation,
    evaluate_timing_confirmation,
    get_market_frame,
    load_market_context,
    normalize_context,
)


@dataclass
class BacktestTrade:
    symbol: str
    candle_ts: str
    entry: float
    stop: float
    tp1: float
    tp2: float
    outcome_48h_pct: float
    hit_tp1: bool
    hit_tp2: bool
    hit_stop: bool


def resolve_symbol(user_symbol: str) -> tuple[str, str]:
    symbol = user_symbol.upper()
    for cg_id, ticker in CRYPTO_IDS.items():
        if ticker == symbol:
            return cg_id, ticker
    raise ValueError(f"Símbolo no soportado: {user_symbol}")


def build_frames(cg_id: str):
    daily_frame = get_market_frame(cg_id, DAILY_LOOKBACK_DAYS, interval=None)
    fourh_frame = get_market_frame(cg_id, HOURLY_LOOKBACK_DAYS, interval=HOURLY_INTERVAL)
    intraday_frame = get_market_frame(cg_id, INTRADAY_LOOKBACK_DAYS, interval=None)

    daily_df = build_ohlcv_from_frame(daily_frame, "1D", 220)
    fourh_df = build_ohlcv_from_frame(fourh_frame, TRADING_TIMEFRAME, 220)
    entry_df = build_ohlcv_from_frame(intraday_frame, ENTRY_TIMEFRAME, 60)
    return daily_df, fourh_df, entry_df


def run_smoke_backtest(symbol: str, horizon_bars_4h: int = 12) -> Dict[str, Any]:
    cg_id, ticker = resolve_symbol(symbol)
    market_context = load_market_context()
    ctx = normalize_context(market_context, ticker)

    daily_df, fourh_df, entry_df = build_frames(cg_id)
    if daily_df is None or fourh_df is None or entry_df is None:
        raise RuntimeError("No hay suficientes datos para backtesting.")

    trades: List[BacktestTrade] = []
    start_idx = 230
    end_idx = len(fourh_df) - horizon_bars_4h

    for i in range(start_idx, end_idx):
        ts = fourh_df.iloc[i]["ts"]
        daily_slice = daily_df[daily_df["ts"] <= ts].tail(260).reset_index(drop=True)
        fourh_slice = fourh_df.iloc[: i + 1].tail(260).reset_index(drop=True)
        entry_slice = entry_df[entry_df["ts"] <= ts].tail(160).reset_index(drop=True)

        if len(daily_slice) < 220 or len(fourh_slice) < 220 or len(entry_slice) < 60:
            continue

        macro_eval = evaluate_macro_confirmation(daily_slice, ticker, ctx)
        setup_eval = evaluate_setup_confirmation(fourh_slice, ticker, cg_id)
        timing_eval = evaluate_timing_confirmation(entry_slice, ticker)
        if not macro_eval or not setup_eval or not timing_eval:
            continue

        candidate = build_candidate(ticker, cg_id, macro_eval, setup_eval, timing_eval)
        if not candidate["alert"]:
            continue

        future = fourh_df.iloc[i + 1 : i + 1 + horizon_bars_4h].reset_index(drop=True)
        if future.empty:
            continue

        entry = float(candidate["entry_price"])
        stop = float(candidate["stop_loss"])
        tp1 = float(candidate.get("tp1", candidate["take_profit"]))
        tp2 = float(candidate.get("tp2", candidate["take_profit"]))
        future_high = float(future["High"].max())
        future_low = float(future["Low"].min())
        future_close = float(future.iloc[-1]["Close"])
        outcome_pct = (future_close - entry) / entry * 100

        trades.append(
            BacktestTrade(
                symbol=ticker,
                candle_ts=str(ts),
                entry=entry,
                stop=stop,
                tp1=tp1,
                tp2=tp2,
                outcome_48h_pct=round(outcome_pct, 2),
                hit_tp1=future_high >= tp1,
                hit_tp2=future_high >= tp2,
                hit_stop=future_low <= stop,
            )
        )

    if not trades:
        return {
            "symbol": ticker,
            "signals": 0,
            "message": "No hubo señales 3/3 en la ventana disponible.",
        }

    df = pd.DataFrame(asdict(t) for t in trades)
    wins = (df["outcome_48h_pct"] > 0).sum()
    losses = (df["outcome_48h_pct"] <= 0).sum()
    gross_profit = df.loc[df["outcome_48h_pct"] > 0, "outcome_48h_pct"].sum()
    gross_loss = abs(df.loc[df["outcome_48h_pct"] <= 0, "outcome_48h_pct"].sum())
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

    return {
        "symbol": ticker,
        "signals": int(len(df)),
        "win_rate_pct": round(wins / len(df) * 100, 2),
        "avg_outcome_48h_pct": round(df["outcome_48h_pct"].mean(), 2),
        "median_outcome_48h_pct": round(df["outcome_48h_pct"].median(), 2),
        "profit_factor": profit_factor,
        "tp1_hit_rate_pct": round(df["hit_tp1"].mean() * 100, 2),
        "tp2_hit_rate_pct": round(df["hit_tp2"].mean() * 100, 2),
        "stop_hit_rate_pct": round(df["hit_stop"].mean() * 100, 2),
        "sample": df.tail(10).to_dict(orient="records"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke backtest para Crypto Sentinel")
    parser.add_argument("--symbol", default="BTC", help="Ticker soportado, por ejemplo BTC o ETH")
    parser.add_argument("--horizon-bars", type=int, default=12, help="Horizonte en velas 4H. 12 = 48h")
    args = parser.parse_args()

    result = run_smoke_backtest(args.symbol, horizon_bars_4h=args.horizon_bars)
    print(pd.Series(result).to_string())


if __name__ == "__main__":
    main()
