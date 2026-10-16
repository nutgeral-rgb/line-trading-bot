"""Quarterly fundamentals and valuation context for SET candidates.

This is a screening aid, not a valuation model or buy/sell instruction.
Yahoo Finance data can be missing, stale, or inconsistent; unavailable data
must never be treated as a positive result.
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


def _number(info, *keys):
    for key in keys:
        try:
            value = float(info.get(key))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return None


def analyze_fundamentals(symbol):
    """Refresh fundamentals/valuation context once per Bangkok calendar day."""
    day = datetime.now(ZoneInfo("Asia/Bangkok")).date().isoformat()
    return _analyze_fundamentals_for_day(symbol, day)


@lru_cache(maxsize=200)
def _analyze_fundamentals_for_day(symbol, day):
    """Return explicit pass/fail/unavailable evidence and valuation context."""
    is_financial = symbol in _FINANCIALS
    required = 3 if is_financial else 4
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        income = cashflow = balance = None
        for attempt in range(3):
            try:
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
                        "ดึงข้อมูลงบไม่สำเร็จ: symbol=%s error_type=%s",
                        symbol, type(exc).__name__,
                    )
                    income = cashflow = balance = None
                    break
                delay = 2 ** (attempt + 1)
                logger.info(
                    "Yahoo Finance จำกัดคำขอข้อมูลงบ: symbol=%s retry=%d delay=%ds",
                    symbol, attempt + 1, delay,
                )
                time.sleep(delay)
        try:
            info = ticker.info or {}
        except Exception as exc:
            logger.info(
                "ดึง valuation metadata ไม่สำเร็จ: symbol=%s error_type=%s",
                symbol, type(exc).__name__,
            )
            info = {}
    except Exception as exc:
        logger.warning(
            "เตรียมดึงข้อมูลงบไม่สำเร็จ: symbol=%s error_type=%s",
            symbol, type(exc).__name__,
        )
        return {
            "score": None, "available": 0, "required": required,
            "checks": {}, "sector": None, "valuation": {},
            "valuation_concern": None, "summary": "ดึงข้อมูลงบไม่ได้",
        }

    checks = {}
    details = []

    revenue = _latest_values(income, ("Total Revenue", "Operating Revenue"), 5)
    if len(revenue) >= 5 and revenue[4] != 0:
        growth = revenue[0] / revenue[4] - 1
        if math.isfinite(growth):
            checks["revenue_growth"] = growth > 0
            details.append(f"รายได้ YoY {growth * 100:+.1f}%")
    else:
        details.append("รายได้ YoY: ไม่มีข้อมูล")

    profits = _latest_values(
        income,
        ("Net Income", "Net Income Common Stockholders", "Net Income From Continuing Operations"),
        4,
    )
    if len(profits) >= 4:
        profitable = sum(value > 0 for value in profits)
        checks["earnings"] = sum(profits) > 0 and profitable >= 3
        details.append(f"กำไรบวก {profitable}/4 ไตรมาส")
    else:
        details.append("กำไร: งบไม่ครบ 4 ไตรมาส")

    operating_cash = _latest_values(
        cashflow,
        ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
        4,
    )
    if len(operating_cash) >= 4:
        cashflow_positive = sum(operating_cash) > 0
        checks["operating_cashflow"] = cashflow_positive
        details.append("กระแสเงินสดดำเนินงาน " + ("บวก" if cashflow_positive else "ลบ"))
    else:
        details.append("กระแสเงินสด: ข้อมูลไม่ครบ")

    if is_financial:
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
            checks["leverage"] = math.isfinite(ratio) and ratio <= 2.0
            details.append(f"หนี้/ทุน {ratio:.1f} เท่า")
        else:
            details.append("หนี้/ทุน: ข้อมูลไม่ครบ")

    # Valuation multiples are a review flag only: sector/accounting differences
    # make these coarse thresholds unsuitable as a fair-value estimate.
    try:
        sector = info.get("sector") or info.get("industry")
    except Exception:
        sector = None
    pe = _number(info, "trailingPE")
    pb = _number(info, "priceToBook")
    roe = None
    try:
        raw_roe = float(info.get("returnOnEquity"))
        if math.isfinite(raw_roe):
            roe = raw_roe
    except (TypeError, ValueError, OverflowError):
        pass
    valuation = {"trailing_pe": pe, "price_to_book": pb, "return_on_equity": roe}
    elevated = []
    if pe is not None and pe > 35:
        elevated.append("P/E สูงกว่า 35")
    pb_limit = 2.5 if is_financial else 5.0
    if pb is not None and pb > pb_limit:
        elevated.append(f"P/B สูงกว่า {pb_limit:g}")
    valuation_concern = len(elevated)
    if pe is None and pb is None:
        details.append("มูลค่า P/E, P/B: ไม่มีข้อมูล (ต้องตรวจเพิ่ม)")
    else:
        multiples = []
        multiples.append(f"P/E {pe:.1f}" if pe is not None else "P/E ไม่มีข้อมูล")
        multiples.append(f"P/B {pb:.2f}" if pb is not None else "P/B ไม่มีข้อมูล")
        details.append("มูลค่าเบื้องต้น " + ", ".join(multiples))
    if roe is not None:
        details.append(f"ROE {roe * 100:.1f}%")
    if elevated:
        details.append("ควรทบทวนมูลค่า: " + ", ".join(elevated))

    score = sum(checks.values())
    available = len(checks)
    if available < required:
        label = f"ข้อมูลพื้นฐานไม่ครบ ({available}/{required})"
    elif score == required:
        label = "ผ่านเกณฑ์พื้นฐานครบ"
    else:
        label = f"พื้นฐานผ่าน {score}/{required}"
    summary = label + (" | " + "; ".join(details) if details else "")
    return {
        "score": score if available else None,
        "available": available,
        "required": required,
        "checks": checks,
        "sector": sector,
        "valuation": valuation,
        "valuation_concern": valuation_concern if pe is not None or pb is not None else None,
        "summary": summary,
    }
