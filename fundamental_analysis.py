"""Quarterly fundamentals and valuation context for SET candidates.

This is a screening aid, not a valuation model or buy/sell instruction.
Yahoo Finance data can be missing, stale, or inconsistent; unavailable data
must never be treated as a positive result. Auditor opinions are not exposed
by the quote source and are deliberately reported as unavailable.
"""
from __future__ import annotations

from functools import lru_cache
from datetime import date, datetime
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
_MAX_STATEMENT_AGE_DAYS = 180
_MAX_STATEMENT_DATE_SPREAD_DAYS = 100


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
            result = []
            for key in keys[:count]:
                value = float(values.loc[key])
                if math.isfinite(value):
                    result.append(value)
            return result
        except (TypeError, ValueError, OverflowError):
            return []
    return []


def _column_dates(frame):
    if frame is None or getattr(frame, "empty", True):
        return []
    dates = []
    for value in frame.columns:
        try:
            if isinstance(value, datetime):
                parsed = value.date()
            elif isinstance(value, date):
                parsed = value
            else:
                parsed = date.fromisoformat(str(value)[:10])
            dates.append(parsed)
        except (TypeError, ValueError, OverflowError):
            continue
    return dates


def _number(info, *keys):
    for key in keys:
        try:
            value = float(info.get(key))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return None


def _finite_info_value(info, *keys):
    for key in keys:
        try:
            value = float(info.get(key))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value):
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
    required = 4 if is_financial else 5
    empty_result = {
        "score": None, "available": 0, "required": required,
        "checks": {}, "sector": None, "valuation": {},
        "valuation_concern": None, "quality_warnings": [],
        "quality_penalty": 0, "statement_period": None,
        "statement_age_days": None, "source": "Yahoo Finance",
        "audit_opinion": None, "audit_opinion_available": False,
        "summary": "ดึงข้อมูลงบไม่ได้",
    }
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
        return empty_result

    checks = {}
    details = []
    quality_warnings = []

    period_by_statement = {
        "income": max(_column_dates(income), default=None),
        "cashflow": max(_column_dates(cashflow), default=None),
        "balance": max(_column_dates(balance), default=None),
    }
    statement_dates = [value for value in period_by_statement.values() if value]
    statement_period = min(statement_dates).isoformat() if statement_dates else None
    statement_age_days = None
    if len(statement_dates) == 3:
        oldest, newest = min(statement_dates), max(statement_dates)
        try:
            reference_day = date.fromisoformat(day)
            statement_age_days = (reference_day - oldest).days
            dates_aligned = (newest - oldest).days <= _MAX_STATEMENT_DATE_SPREAD_DAYS
            fresh = (
                0 <= statement_age_days <= _MAX_STATEMENT_AGE_DAYS
                and dates_aligned
            )
            checks["financial_freshness"] = fresh
            details.append(
                f"งบถึง {oldest.isoformat()} อายุ {statement_age_days} วัน"
            )
            if not dates_aligned:
                quality_warnings.append("งบแต่ละชุดอาจเป็นคนละงวด")
            if statement_age_days > _MAX_STATEMENT_AGE_DAYS:
                quality_warnings.append("งบเก่ากว่า 180 วัน")
            elif statement_age_days < 0:
                quality_warnings.append("วันที่งบใหม่กว่าวันปัจจุบัน")
        except (TypeError, ValueError):
            details.append("วันงบ: อ่านวันที่ไม่ได้")
    else:
        details.append("วันงบ: ข้อมูลวันสิ้นงวดไม่ครบ")
    if "financial_freshness" not in checks:
        quality_warnings.append("ตรวจความใหม่ของงบไม่ได้")

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
        5,
    )
    if len(profits) >= 4:
        profitable = sum(value > 0 for value in profits[:4])
        ttm_profit = sum(profits[:4])
        checks["earnings"] = ttm_profit > 0 and profitable >= 3
        details.append(f"กำไรบวก {profitable}/4 ไตรมาส")
    else:
        ttm_profit = None
        details.append("กำไร: งบไม่ครบ 4 ไตรมาส")

    operating_cash = _latest_values(
        cashflow,
        ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
        4,
    )
    if len(operating_cash) >= 4:
        ttm_operating_cash = sum(operating_cash[:4])
        cashflow_positive = ttm_operating_cash > 0
        checks["operating_cashflow"] = cashflow_positive
        details.append("กระแสเงินสดดำเนินงาน " + ("บวก" if cashflow_positive else "ลบ"))
    else:
        ttm_operating_cash = None
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

    # Supplemental quality measures are warnings/ranking evidence, not hidden
    # hard gates: sector-specific capital needs make universal cutoffs unsafe.
    ttm_revenue = sum(revenue[:4]) if len(revenue) >= 4 else None
    net_margin = (
        ttm_profit / ttm_revenue
        if ttm_profit is not None and ttm_revenue and ttm_revenue > 0
        else None
    )
    if net_margin is not None:
        details.append(f"อัตรากำไรสุทธิ {net_margin * 100:.1f}%")
        if net_margin <= 0:
            quality_warnings.append("อัตรากำไรสุทธิไม่เป็นบวก")

    capex = _latest_values(
        cashflow,
        ("Capital Expenditure", "Capital Expenditures", "Purchase Of PPE"),
        4,
    )
    free_cash_flow = None
    if ttm_operating_cash is not None and len(capex) >= 4:
        free_cash_flow = ttm_operating_cash - sum(abs(value) for value in capex[:4])
        details.append(
            "FCF 4 ไตรมาส " + ("บวก" if free_cash_flow >= 0 else "ลบ")
        )
        if free_cash_flow < 0:
            quality_warnings.append("กระแสเงินสดอิสระ 4 ไตรมาสติดลบ")
    else:
        details.append("FCF: ข้อมูลไม่ครบ")

    ebit = _latest_values(income, ("EBIT", "Operating Income"), 4)
    interest = _latest_values(
        income,
        (
            "Interest Expense Non Operating",
            "Interest Expense Non Operating Net Non Operating Interest Income Expense",
            "Interest Expense",
        ),
        4,
    )
    interest_coverage = None
    if len(ebit) >= 4 and len(interest) >= 4:
        interest_total = sum(abs(value) for value in interest[:4])
        if interest_total > 0:
            interest_coverage = sum(ebit[:4]) / interest_total
            details.append(f"ความสามารถจ่ายดอกเบี้ย {interest_coverage:.1f} เท่า")
            if interest_coverage < 2:
                quality_warnings.append("ความสามารถจ่ายดอกเบี้ยต่ำกว่า 2 เท่า")

    shares = _latest_values(
        income,
        ("Diluted Average Shares", "Basic Average Shares", "Ordinary Shares Number"),
        5,
    )
    share_change = None
    if len(shares) >= 5 and shares[4] > 0:
        share_change = shares[0] / shares[4] - 1
        details.append(f"จำนวนหุ้นเฉลี่ย YoY {share_change * 100:+.1f}%")
        if share_change > 0.05:
            quality_warnings.append("จำนวนหุ้นเฉลี่ยเพิ่มเกิน 5% YoY")

    try:
        sector = info.get("sector") or info.get("industry")
    except Exception:
        sector = None
    pe = _number(info, "trailingPE")
    pb = _number(info, "priceToBook")
    roe = _finite_info_value(info, "returnOnEquity")
    roa = _finite_info_value(info, "returnOnAssets")
    valuation = {
        "trailing_pe": pe,
        "price_to_book": pb,
        "return_on_equity": roe,
        "return_on_assets": roa,
        "net_margin_ttm": net_margin,
        "free_cash_flow_ttm": free_cash_flow,
        "interest_coverage_ttm": interest_coverage,
        "diluted_shares_yoy_change": share_change,
    }
    if roe is not None:
        details.append(f"ROE {roe * 100:.1f}%")
        if roe < 0:
            quality_warnings.append("ROE ติดลบ")
    if roa is not None:
        details.append(f"ROA {roa * 100:.1f}%")
        if roa < 0:
            quality_warnings.append("ROA ติดลบ")

    # Absolute valuation limits are review flags only. Sector medians are
    # compared by the portfolio planner where enough peers are available.
    elevated = []
    if pe is not None and pe > 35:
        elevated.append("P/E สูงกว่า 35")
    pb_limit = 2.5 if is_financial else 5.0
    if pb is not None and pb > pb_limit:
        elevated.append(f"P/B สูงกว่า {pb_limit:g}")
    valuation_concern = len(elevated)
    if pe is None and pb is None:
        details.append("P/E/P/B: ไม่มีข้อมูล")
    else:
        details.append(
            ("P/E " + (f"{pe:.1f}" if pe is not None else "ไม่มีข้อมูล"))
            + ", "
            + ("P/B " + (f"{pb:.2f}" if pb is not None else "ไม่มีข้อมูล"))
        )
    if elevated:
        details.append("ควรทบทวนมูลค่า: " + ", ".join(elevated))

    if quality_warnings:
        details.append("ธงคุณภาพ: " + ", ".join(dict.fromkeys(quality_warnings[:3])))
    else:
        details.append("คุณภาพกำไรเบื้องต้น: ไม่พบธงเตือนจากข้อมูลที่มี")
    details.append("ความเห็นผู้สอบบัญชี: แหล่งข้อมูลนี้ไม่มีข้อมูล ต้องตรวจ SET/ก.ล.ต.")

    score = sum(bool(value) for value in checks.values())
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
        "quality_warnings": list(dict.fromkeys(quality_warnings)),
        "quality_penalty": len(set(quality_warnings)),
        "statement_period": statement_period,
        "statement_age_days": statement_age_days,
        "source": "Yahoo Finance",
        "audit_opinion": None,
        "audit_opinion_available": False,
        "summary": summary,
    }
