#!/usr/bin/env python3
"""Backtest the LINE bot's daily equal-weight rotation rule.

Signals use adjusted closing prices after a session closes. Rebalances execute
at the next session's adjusted open, so the test does not buy at the signal
close. Yahoo Finance's auto-adjusted OHLC series is used for split/dividend
adjustments; validate data quality before relying on any result.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import math
from typing import Iterable

import numpy as np
import pandas as pd

from set100_constituents import SET100_SYMBOLS

INDEX_SYMBOL = "SET.BK"
MOMENTUM_LOOKBACK = 63
VOLATILITY_LOOKBACK = 20
MIN_AVG_DAILY_VALUE_BAHT = 5_000_000
UNIVERSE = SET100_SYMBOLS


def select_targets(
    frames: dict[str, pd.DataFrame],
    as_of: pd.Timestamp,
    universe: Iterable[str] = UNIVERSE,
    top_n: int = 5,
    sma_days: int = 20,
    breadth_threshold: float = 50.0,
    min_coverage: float = 0.80,
    min_avg_daily_value: float = MIN_AVG_DAILY_VALUE_BAHT,
) -> tuple[list[str], float, float]:
    """Return targets, breadth percentage, and valid-universe coverage."""
    symbols = list(universe)
    if not symbols:
        return [], 0.0, 0.0

    as_of_day = pd.Timestamp(as_of).date()
    valid: dict[str, tuple[float, float]] = {}
    for symbol in symbols:
        frame = frames.get(symbol)
        if frame is None or "Close" not in frame:
            continue
        frame = frame.sort_index()
        dated = frame.loc[frame.index.date <= as_of_day]
        history = _field(dated, "Close").dropna()
        if len(history) < sma_days or history.index[-1].date() != as_of_day:
            continue
        close = float(history.iloc[-1])
        average = float(history.tail(sma_days).mean())
        if math.isfinite(close) and math.isfinite(average) and average > 0:
            valid[symbol] = (close, average)

    coverage = len(valid) / len(symbols)
    if coverage < min_coverage or not valid:
        return [], 0.0, coverage

    breadth = 100.0 * sum(close > average for close, average in valid.values()) / len(valid)
    if breadth < breadth_threshold:
        return [], breadth, coverage

    ranked = []
    for symbol, (close, average) in valid.items():
        frame = frames[symbol].sort_index()
        dated = frame.loc[frame.index.date <= as_of_day]
        closes = _field(dated, "Close").dropna()
        if len(closes) < MOMENTUM_LOOKBACK + 1 or "Volume" not in dated:
            continue
        recent = dated.loc[closes.index].tail(MOMENTUM_LOOKBACK + 1)
        recent_close = _field(recent, "Close")
        returns = recent_close.pct_change().dropna()
        if len(returns) < MOMENTUM_LOOKBACK:
            continue
        momentum = float(recent_close.iloc[-1] / recent_close.iloc[0] - 1.0)
        volatility = float(returns.tail(VOLATILITY_LOOKBACK).std(ddof=1) * math.sqrt(252))
        traded_value = (_field(recent, "Close") * _field(recent, "Volume")).tail(VOLATILITY_LOOKBACK)
        valid_traded_value = traded_value.replace([np.inf, -np.inf], np.nan).dropna()
        if len(valid_traded_value) < VOLATILITY_LOOKBACK:
            continue
        avg_value = float(valid_traded_value.median())
        if not all(math.isfinite(x) for x in (momentum, volatility, avg_value)):
            continue
        if close > average and momentum > 0 and volatility > 0 and avg_value >= min_avg_daily_value:
            ranked.append((momentum / volatility, symbol))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [symbol for _, symbol in ranked[:top_n]], breadth, coverage


def _field(frame: pd.DataFrame, name: str) -> pd.Series:
    if name not in frame.columns:
        return pd.Series(index=frame.index, dtype=float)
    return pd.to_numeric(frame[name], errors="coerce")


def backtest(
    frames: dict[str, pd.DataFrame],
    universe: Iterable[str] = UNIVERSE,
    top_n: int = 5,
    sma_days: int = 20,
    breadth_threshold: float = 50.0,
    min_coverage: float = 0.80,
    min_avg_daily_value: float = MIN_AVG_DAILY_VALUE_BAHT,
    commission_rate: float = 0.0025,
    vat_rate: float = 0.07,
    slippage_bps: float = 10.0,
    initial_cash: float = 100_000.0,
    evaluation_start: str | None = None,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """Run daily close-signal / next-open rotation with cash and trading costs."""
    if INDEX_SYMBOL not in frames:
        raise ValueError(f"Missing benchmark/index data: {INDEX_SYMBOL}")
    dates = frames[INDEX_SYMBOL].index.sort_values().unique()
    if len(dates) < sma_days + 2:
        raise ValueError("Not enough benchmark history for the selected SMA period")

    cost_rate = commission_rate * (1.0 + vat_rate) + slippage_bps / 10_000.0
    start_index = 1
    if evaluation_start:
        requested_start = pd.Timestamp(evaluation_start)
        start_index = next((i for i, day in enumerate(dates) if pd.Timestamp(day) >= requested_start), len(dates))
        start_index = max(1, start_index)
        if start_index >= len(dates):
            raise ValueError("No benchmark sessions on or after --start")

    equity = float(initial_cash)
    weights: dict[str, float] = {}
    curve: list[dict[str, float | str]] = []
    total_turnover = 0.0
    rebalance_count = 0
    previous_targets: tuple[str, ...] = ()

    for i in range(start_index, len(dates)):
        previous_day, day = pd.Timestamp(dates[i - 1]), pd.Timestamp(dates[i])
        targets, breadth, coverage = select_targets(
            frames, previous_day, universe, top_n, sma_days,
            breadth_threshold, min_coverage, min_avg_daily_value,
        )

        # Existing holdings earn the overnight close-to-open move.
        open_prices: dict[str, float] = {}
        close_prices: dict[str, float] = {}
        for symbol in set(weights) | set(targets):
            frame = frames.get(symbol)
            if frame is None or previous_day not in frame.index or day not in frame.index:
                continue
            open_price = float(_field(frame, "Open").get(day, np.nan))
            previous_close = float(_field(frame, "Close").get(previous_day, np.nan))
            close_price = float(_field(frame, "Close").get(day, np.nan))
            if all(math.isfinite(x) and x > 0 for x in (open_price, previous_close, close_price)):
                open_prices[symbol] = open_price
                close_prices[symbol] = close_price

        overnight_factor = 0.0
        opened_weights: dict[str, float] = {}
        cash_weight = max(0.0, 1.0 - sum(weights.values()))
        for symbol, weight in weights.items():
            if symbol in open_prices:
                factor = open_prices[symbol] / float(_field(frames[symbol], "Close").loc[previous_day])
                value = weight * factor
                opened_weights[symbol] = value
                overnight_factor += value
            else:
                opened_weights[symbol] = weight
                overnight_factor += weight
        overnight_factor += cash_weight
        equity_open = equity * overnight_factor
        current_open_weights = {
            symbol: value / overnight_factor
            for symbol, value in opened_weights.items()
        } if overnight_factor > 0 else {}
        actual_targets = [s for s in targets if s in open_prices]
        target_weights = {
            symbol: 1.0 / len(actual_targets) for symbol in actual_targets
        } if actual_targets else {}

        turnover = sum(
            abs(target_weights.get(symbol, 0.0) - current_open_weights.get(symbol, 0.0))
            for symbol in set(target_weights) | set(current_open_weights)
        )
        total_turnover += turnover
        if tuple(actual_targets) != previous_targets:
            rebalance_count += 1
            previous_targets = tuple(actual_targets)

        equity_after_cost = equity_open * max(0.0, 1.0 - turnover * cost_rate)
        close_factor = 1.0
        if target_weights:
            close_factor = sum(
                weight * (close_prices[symbol] / open_prices[symbol])
                for symbol, weight in target_weights.items()
            )
        equity = equity_after_cost * close_factor
        weights = target_weights
        curve.append({
            "Date": day.strftime("%Y-%m-%d"),
            "Equity": equity,
            "DailyReturn": equity / curve[-1]["Equity"] - 1.0 if curve else equity / initial_cash - 1.0,
            "Breadth": breadth,
            "Coverage": coverage,
            "Positions": len(actual_targets),
            "Turnover": turnover,
        })

    result = pd.DataFrame(curve).set_index("Date")
    benchmark_dates = pd.to_datetime(dates[start_index - 1:])
    benchmark = frames[INDEX_SYMBOL]["Close"].reindex(benchmark_dates).dropna()
    benchmark_return = float(benchmark.iloc[-1] / benchmark.iloc[0] - 1) if len(benchmark) > 1 else 0.0

    daily_returns = result["DailyReturn"].astype(float)
    years = len(daily_returns) / 252.0
    total_return = equity / initial_cash - 1.0
    cagr = (equity / initial_cash) ** (1 / years) - 1 if years > 0 and equity > 0 else -1.0
    equity_values = result["Equity"].astype(float).to_numpy()
    peaks = np.maximum.accumulate(np.concatenate(([initial_cash], equity_values)))[1:]
    max_drawdown = float((equity_values / peaks - 1.0).min()) if len(equity_values) else 0.0
    volatility = float(daily_returns.std(ddof=1) * math.sqrt(252)) if len(daily_returns) > 1 else 0.0
    sharpe = float(daily_returns.mean() / daily_returns.std(ddof=1) * math.sqrt(252)) if len(daily_returns) > 1 and daily_returns.std(ddof=1) > 0 else 0.0
    metrics = {
        "initial_cash": initial_cash,
        "final_equity": equity,
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_drawdown,
        "annualized_volatility": volatility,
        "sharpe_zero_rf": sharpe,
        "benchmark_return": benchmark_return,
        "average_daily_turnover": total_turnover / max(len(daily_returns), 1),
        "rebalance_count": float(rebalance_count),
    }
    return result, metrics


def _download(start: str, end: str, symbols: list[str]) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    raw = yf.download(
        tickers=symbols,
        start=start,
        end=end,
        auto_adjust=True,
        group_by="ticker",
        progress=False,
        threads=True,
    )
    if raw.empty:
        raise ValueError("No price data returned. Check the symbols and date range.")

    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        try:
            frame = raw[symbol].copy() if isinstance(raw.columns, pd.MultiIndex) else raw.copy()
        except (KeyError, TypeError):
            continue
        frame.columns = [str(col).title() for col in frame.columns]
        frame.index = pd.to_datetime(frame.index).tz_localize(None)
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        frame = frame.dropna(subset=["Close"])
        if not frame.empty:
            frames[symbol] = frame
    return frames


def _self_test() -> None:
    days = pd.bdate_range("2024-01-01", periods=72)
    signal_days = days[:70]
    universe = ["AAA.BK", "BBB.BK", "CCC.BK", "DDD.BK"]

    def prices(length: int) -> dict[str, list[float]]:
        return {
            "AAA.BK": [100 + 0.30 * i + 0.10 * math.sin(i) for i in range(length)],
            "BBB.BK": [100 + 0.20 * i + 0.20 * math.sin(i) for i in range(length)],
            "CCC.BK": [100 + 0.10 * i + 0.30 * math.sin(i) for i in range(length)],
            "DDD.BK": [100 - 0.10 * i + 0.10 * math.sin(i) for i in range(length)],
        }

    signal_prices = prices(len(signal_days))
    frames = {
        symbol: pd.DataFrame(
            {"Close": values, "Volume": [100_000] * len(values)},
            index=signal_days,
        )
        for symbol, values in signal_prices.items()
    }
    targets, breadth, coverage = select_targets(
        frames, signal_days[-1], universe, top_n=2, min_coverage=0.75,
    )
    assert targets == ["AAA.BK", "BBB.BK"], targets
    assert breadth == 75.0 and coverage == 1.0, (breadth, coverage)

    stale = dict(frames)
    stale["DDD.BK"] = stale["DDD.BK"].iloc[:-1]
    targets, breadth, coverage = select_targets(
        stale, signal_days[-1], universe, top_n=2, min_coverage=0.75,
    )
    assert targets == ["AAA.BK", "BBB.BK"], targets
    assert coverage == 0.75, coverage

    targets, _, coverage = select_targets(
        stale, signal_days[-1], universe, top_n=2, min_coverage=0.80,
    )
    assert targets == [] and coverage == 0.75, (targets, coverage)

    trading_prices = prices(len(days))
    trading_prices[INDEX_SYMBOL] = [100.0] * len(days)
    trading_frames = {}
    for symbol, values in trading_prices.items():
        close = pd.Series(values, index=days)
        trading_frames[symbol] = pd.DataFrame({
            "Open": close.shift(1).fillna(close),
            "Close": close,
            "Volume": [100_000] * len(close),
        })
    _, metrics = backtest(
        trading_frames, universe=universe, top_n=2, min_coverage=0.75,
        commission_rate=0, vat_rate=0, slippage_bps=0, initial_cash=10_000,
    )
    assert metrics["final_equity"] > 10_000, metrics
    assert metrics["average_daily_turnover"] > 0, metrics
    print("Self-test passed: risk-adjusted ranking, coverage, liquidity, and accounting.")

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=(date.today() - timedelta(days=5 * 365)).isoformat())
    parser.add_argument("--end", default=(date.today() + timedelta(days=1)).isoformat(), help="Exclusive end date")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--sma", type=int, default=20)
    parser.add_argument("--breadth", type=float, default=50.0)
    parser.add_argument("--min-coverage", type=float, default=0.80)
    parser.add_argument("--min-avg-daily-value", type=float, default=MIN_AVG_DAILY_VALUE_BAHT, help="Minimum median traded value in baht/day")
    parser.add_argument("--commission", type=float, default=0.0025, help="Commission rate per side (default 0.25%; set to your broker's rate)")
    parser.add_argument("--vat", type=float, default=0.07, help="VAT charged on commission")
    parser.add_argument("--slippage-bps", type=float, default=10.0, help="Estimated slippage per side in basis points")
    parser.add_argument("--cash", type=float, default=100_000.0)
    parser.add_argument("--output", default="backtest_equity.csv")
    parser.add_argument("--self-test", action="store_true", help="Run offline rule checks without downloading prices")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
        return
    if args.top < 1 or args.sma < 2 or not 0 <= args.min_coverage <= 1:
        parser.error("--top must be positive, --sma at least 2, and --min-coverage between 0 and 1")

    symbols = sorted(set(UNIVERSE + [INDEX_SYMBOL]))
    warmup_days = max(90, args.sma * 3)
    download_start = (pd.Timestamp(args.start) - pd.Timedelta(days=warmup_days)).date().isoformat()
    frames = _download(download_start, args.end, symbols)
    if INDEX_SYMBOL not in frames:
        raise SystemExit(f"Could not download benchmark {INDEX_SYMBOL}")

    curve, metrics = backtest(
        frames,
        top_n=args.top,
        sma_days=args.sma,
        breadth_threshold=args.breadth,
        min_coverage=args.min_coverage,
        min_avg_daily_value=args.min_avg_daily_value,
        commission_rate=args.commission,
        vat_rate=args.vat,
        slippage_bps=args.slippage_bps,
        initial_cash=args.cash,
        evaluation_start=args.start,
    )
    curve.to_csv(args.output)
    print(f"Period: {curve.index[0]} to {curve.index[-1]}")
    print(f"Equity curve: {args.output}")
    for key, value in metrics.items():
        if key in {"initial_cash", "final_equity"}:
            print(f"{key}: {value:,.2f}")
        elif key == "rebalance_count":
            print(f"{key}: {value:.0f}")
        else:
            print(f"{key}: {value:.2%}" if "sharpe" not in key else f"{key}: {value:.2f}")


if __name__ == "__main__":
    main()
