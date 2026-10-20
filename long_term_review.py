"""Prospective checks for recorded long-term recommendations and holdings.

Only recommendations actually saved at signal time are scored. Historical
fundamentals are not reconstructed from today's statements.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import math

import pandas as pd


HORIZONS = (20, 60, 120, 252)


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _date_indexed(frame):
    if frame is None or getattr(frame, "empty", True):
        return None
    result = frame.copy()
    result.index = pd.to_datetime(result.index).tz_localize(None)
    result = result[~result.index.duplicated(keep="last")].sort_index()
    return result if not result.empty else None


def _adjusted_open(row):
    opening = _number(row.get("Open"))
    close = _number(row.get("Close"))
    adjusted = _number(row.get("Adj Close")) or close
    if opening is None or close is None or adjusted is None:
        return None
    return opening * adjusted / close


def _adjusted_close(row):
    return _number(row.get("Adj Close")) or _number(row.get("Close"))


def evaluate_recommendations(recommendations, frames, benchmark=None,
                             horizons=HORIZONS, cost_rate=0.004):
    """Score saved signals from the next open to each completed horizon.

    Returns observations and per-horizon summaries. Rows without the next
    open or a completed horizon are excluded rather than filled or guessed.
    """
    if not math.isfinite(cost_rate) or not 0 <= cost_rate <= 0.05:
        raise ValueError("cost_rate must be between 0 and 0.05")
    unique = {}
    for record in recommendations:
        if not isinstance(record, dict) or record.get("delivery_status") != "SENT":
            continue
        symbol, signal_date = record.get("symbol"), record.get("signal_date")
        try:
            date.fromisoformat(signal_date)
        except (TypeError, ValueError):
            continue
        if isinstance(symbol, str):
            unique[(signal_date, symbol)] = record

    benchmark_frame = _date_indexed(benchmark)
    observations = []
    for (signal_date, symbol), record in sorted(unique.items()):
        frame = _date_indexed(frames.get(symbol))
        if frame is None:
            continue
        later = frame.loc[frame.index.strftime("%Y-%m-%d") > signal_date]
        if later.empty:
            continue
        entry_date = later.index[0].strftime("%Y-%m-%d")
        entry_open = _adjusted_open(later.iloc[0])
        if entry_open is None:
            continue
        for horizon in horizons:
            if horizon < 1 or len(later) < horizon:
                continue
            exit_row = later.iloc[horizon - 1]
            exit_price = _adjusted_close(exit_row)
            if exit_price is None:
                continue
            exit_date = later.index[horizon - 1].strftime("%Y-%m-%d")
            net_return = exit_price * (1 - cost_rate) / (
                entry_open * (1 + cost_rate)
            ) - 1
            benchmark_return = None
            if benchmark_frame is not None:
                entry_benchmark = benchmark_frame.loc[
                    benchmark_frame.index.strftime("%Y-%m-%d") == entry_date
                ]
                exit_benchmark = benchmark_frame.loc[
                    benchmark_frame.index.strftime("%Y-%m-%d") == exit_date
                ]
                if not entry_benchmark.empty and not exit_benchmark.empty:
                    benchmark_open = _adjusted_open(entry_benchmark.iloc[0])
                    benchmark_close = _adjusted_close(exit_benchmark.iloc[0])
                    if benchmark_open is not None and benchmark_close is not None:
                        benchmark_return = benchmark_close / benchmark_open - 1
            observations.append({
                "symbol": symbol,
                "signal_date": signal_date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "horizon": horizon,
                "net_return": net_return,
                "benchmark_return": benchmark_return,
            })

    grouped = defaultdict(list)
    # A daily screen can repeat the same stock for many consecutive mornings.
    # Keep every observation for the audit trail, but count only non-overlapping
    # holding windows for that stock in aggregate accuracy statistics.
    last_exit_by_horizon_symbol = {}
    for item in sorted(observations, key=lambda row: (
        row["entry_date"], row["signal_date"], row["symbol"], row["horizon"]
    )):
        key = (item["horizon"], item["symbol"])
        if item["entry_date"] <= last_exit_by_horizon_symbol.get(key, ""):
            continue
        grouped[item["horizon"]].append(item)
        last_exit_by_horizon_symbol[key] = item["exit_date"]
    summaries = {}
    for horizon in horizons:
        rows = grouped[horizon]
        if not rows:
            continue
        comparable = [row for row in rows if row["benchmark_return"] is not None]
        summaries[horizon] = {
            "count": len(rows),
            "positive_count": sum(row["net_return"] > 0 for row in rows),
            "mean_net_return": sum(row["net_return"] for row in rows) / len(rows),
            "comparable_count": len(comparable),
            "beat_benchmark_count": sum(
                row["net_return"] > row["benchmark_return"] for row in comparable
            ),
            "mean_excess_return": (
                sum(row["net_return"] - row["benchmark_return"] for row in comparable)
                / len(comparable) if comparable else None
            ),
        }
    return observations, summaries


def assess_holding(frame, fundamental, average_cost):
    """Return a review flag with explicit evidence, never an automatic order."""
    frame = _date_indexed(frame)
    if frame is None or "Close" not in frame or len(frame) < 200:
        return {"status": "ข้อมูลไม่พอ", "reasons": ["ราคาย้อนหลังไม่ครบ 200 วัน"]}
    prices = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    if len(prices) < 200:
        return {"status": "ข้อมูลไม่พอ", "reasons": ["ราคาย้อนหลังไม่ครบ 200 วัน"]}
    close = _number(prices.iloc[-1])
    if close is None:
        return {"status": "ข้อมูลไม่พอ", "reasons": ["ไม่มีราคาปิดที่ใช้ได้"]}
    sma50, sma200 = float(prices.tail(50).mean()), float(prices.tail(200).mean())
    checks = fundamental.get("checks") or {}
    available = int(fundamental.get("available", 0) or 0)
    required = int(fundamental.get("required", 5) or 5)
    cost = _number(average_cost)
    pnl_pct = close / cost - 1 if cost is not None else None
    result = {
        "date": prices.index[-1].strftime("%Y-%m-%d"),
        "close": close,
        "pnl_pct": pnl_pct,
        "status": "ถือต่อ",
        "reasons": [],
    }
    if available != required:
        result["status"] = "ข้อมูลไม่พอ"
        result["reasons"] = ["งบล่าสุดตรวจได้ไม่ครบ จึงยังสรุปเรื่องขายไม่ได้"]
        return result
    if checks.get("financial_freshness") is not True:
        result["status"] = "ข้อมูลไม่พอ"
        result["reasons"] = ["งบเก่าหรือคนละงวด ต้องตรวจงบใหม่ก่อนตัดสินใจขาย"]
        return result
    severe = [
        label for check, label in (
            ("earnings", "กำไร 4 ไตรมาสไม่ผ่านเกณฑ์"),
            ("operating_cashflow", "กระแสเงินสดดำเนินงานไม่ผ่านเกณฑ์"),
        ) if checks.get(check) is False
    ]
    other_fails = [
        label for check, label in (
            ("revenue_growth", "รายได้ไม่เติบโต YoY"),
            ("leverage", "หนี้ต่อทุนสูงกว่าเกณฑ์"),
        ) if checks.get(check) is False
    ]
    broken_trend = close < sma50 and sma50 < sma200
    if severe and broken_trend:
        result["status"] = "พิจารณาขาย/ลด"
        result["reasons"] = severe[:2] + ["ราคาต่ำกว่าเส้น 50 และ 200 วัน"]
    elif severe or len(other_fails) >= 2 or broken_trend:
        result["status"] = "ทบทวนใกล้ชิด"
        result["reasons"] = (severe + other_fails)[:2]
        if broken_trend:
            result["reasons"].append("แนวโน้มระยะกลางและยาวอ่อนตัว")
    else:
        result["reasons"] = ["งบหลักยังผ่านและไม่พบสัญญาณขายร่วมกัน"]
    return result
