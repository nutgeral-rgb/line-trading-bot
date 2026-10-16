"""Quarterly fundamental checks for SET candidates using Yahoo Finance.

This is an informational overlay, not a buy/sell filter. Incomplete data is
reported as unavailable. Yahoo Finance data can be missing or delayed.
"""
from __future__ import annotations

from functools import lru_cache
from datetime import datetime
import logging
import math
import re
import time
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Debt/equity is not a useful comparable screen for banks and lenders.
_FINANCIALS = {
    "AEONTS.BK", "BAM.BK", "BBL.BK", "KBANK.BK", "KKP.BK", "KTB.BK",
    "KTC.BK", "MTC.BK", "SAWAD.BK", "SCB.BK", "TCAP.BK", "TIDLOR.BK",
    "TISCO.BK", "TTB.BK", "JMT.BK",
}


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
    """Refresh the informational overlay once per Bangkok calendar day."""
    day = datetime.now(ZoneInfo("Asia/Bangkok")).date().isoformat()
    return _analyze_fundamentals_for_day(symbol, day)


@lru_cache(maxsize=200)
def _analyze_fundamentals_for_day(symbol, day):
    """Return a simple evidence score and compact Thai summary."""
    try:
        import yfinance as yf

        for attempt in range(3):
            try:
                ticker = yf.Ticker(symbol)
                income = ticker.quarterly_income_stmt
                cashflow = ticker.quarterly_cashflow
                balance = ticker.quarterly_balance_sheet
                break
            except Exception as exc:
                is_rate_limited = (
                    "ratelimit" in type(exc).__name__.lower()
                    or "too many requests" in str(exc).lower()
                    or "rate limited" in str(exc).lower()
                )
                if not is_rate_limited or attempt == 2:
                    logger.warning(
                        "ดึงข้อมูลงบไม่สำเร็จ: symbol=%s "
                        "error_type=%s",
                        symbol,
                        type(exc).__name__,
                    )
                    return {
                        "score": None,
                        "available": 0,
                        "summary": "ดึงข้อมูลงบไม่ได้",
                    }
                delay = 2 ** (attempt + 1)
                logger.info(
                    "Yahoo Finance จำกัดคำขอข้อมูลงบ: "
                    "symbol=%s retry=%d delay=%ds",
                    symbol,
                    attempt + 1,
                    delay,
                )
                time.sleep(delay)
    except Exception as exc:
        logger.warning(
            "เตรียมดึงข้อมูลงบไม่สำเร็จ: symbol=%s "
            "error_type=%s",
            symbol,
            type(exc).__name__,
        )
        return {
            "score": None,
            "available": 0,
            "summary": "ดึงข้อมูลงบไม่ได้",
        }

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

    if symbol in _FINANCIALS:
        details.append("ไม่ใช้หนี้/ทุนกับธุรกิจการเงิน")
    else:
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

