"""Lightweight fundamental checks for SET stock candidates using Yahoo Finance.

The data is optional and informational: missing provider fields are reported
as unavailable and are never silently treated as a pass.
"""
from __future__ import annotations

import math
import re


def _normalise(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _latest_values(frame, aliases, count):
    if frame is None or getattr(frame, "empty", True):
        return []
    wanted = {_normalise(alias) for alias in aliases}
    for label in frame.index:
        if _normalise(label) not in wanted:
            continue
        values = frame.loc[label].dropna()
        try:
            keys = sorted(values.index, key=lambda item: str(item), reverse=True)
            return [float(values.loc[key]) for key in keys[:count]]
        except (TypeError, ValueError, OverflowError):
            return []
    return []


def analyze_fundamentals(symbol):
    """Return a simple evidence score and compact Thai summary."""
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        income = ticker.quarterly_income_stmt
        cashflow = ticker.quarterly_cashflow
        balance = ticker.quarterly_balance_sheet
    except Exception:
        return {"score": None, "available": 0, "summary": "ดึงข้อมูลงบไม่ได้"}

    checks = []
    details = []

    revenue = _latest_values(income, ("Total Revenue", "Operating Revenue"), 5)
    if len(revenue) >= 5:
        growth = revenue[0] / revenue[4] - 1 if revenue[4] else math.nan
        if math.isfinite(growth):
            checks.append(growth > 0)
            details.append("รายได้ YoY " + ("เพิ่ม" if growth > 0 else "ลด/ทรงตัว"))
    else:
        details.append("รายได้ YoY ไม่มีข้อมูล")

    profits = _latest_values(
        income,
        ("Net Income", "Net Income Common Stockholders", "Net Income From Continuing Operations"),
        4,
    )
    if len(profits) >= 4:
        profitable = sum(value > 0 for value in profits)
        checks.append(sum(profits) > 0 and profitable >= 3)
        details.append(f"กำไร {profitable}/4 ไตรมาส")
    else:
        details.append("กำไร: งบไม่ครบ 4 ไตรมาส")

    operating_cash = _latest_values(
        cashflow,
        ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
        4,
    )
    if len(operating_cash) >= 4:
        checks.append(sum(operating_cash) > 0)
        details.append("กระแสเงินสดดำเนินงาน " + ("บวก" if sum(operating_cash) > 0 else "ลบ"))
    else:
        details.append("กระแสเงินสด: ข้อมูลไม่ครบ")

    debt = _latest_values(balance, ("Total Debt",), 1)
    equity = _latest_values(
        balance,
        ("Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"),
        1,
    )
    if debt and equity and equity[0] > 0:
        ratio = debt[0] / equity[0]
        checks.append(math.isfinite(ratio) and ratio <= 2.0)
        details.append(f"หนี้/ทุน {ratio:.1f} เท่า")
    else:
        details.append("หนี้/ทุน: ข้อมูลไม่ครบ")

    score = sum(checks)
    available = len(checks)
    if available < 2:
        label = "ข้อมูลไม่พอ"
    elif score == available:
        label = "สัญญาณพื้นฐานบวก"
    elif score == 0:
        label = "ควรตรวจความเสี่ยง"
    else:
        label = "ผสม"
    summary = f"{label} ({score}/{available})"
    if details:
        summary += " | " + "; ".join(details)
    return {"score": score if available else None, "available": available, "summary": summary}
