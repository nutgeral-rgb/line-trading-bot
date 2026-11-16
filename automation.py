"""Reminders and summaries built only from data the user already recorded.

Pure functions: callers own storage, locking and delivery. Nothing here moves
money or places orders; every cash change still follows a user confirmation.
"""
from datetime import date
import calendar
import math

DEFAULT_REMINDER_DAY = 25
DIVIDEND_TAX_RATE = 0.10
EX_DATE_WINDOW_DAYS = 14


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) and result > 0 else 0.0


def month_key(day):
    return day.strftime("%Y-%m")


def reminder_day(portfolio):
    """Day of month for the deposit reminder, clamped to 1-28."""
    strategy = portfolio.get("investment_strategy")
    value = strategy.get("reminder_day") if isinstance(strategy, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 28:
        return DEFAULT_REMINDER_DAY
    return value


def contribution_status(portfolio, today):
    """Planned, already-recorded and remaining deposit for the current month."""
    strategy = portfolio.get("investment_strategy")
    plan = _number(strategy.get("monthly_contribution_baht")) if isinstance(strategy, dict) else 0.0
    key = month_key(today)
    recorded = sum(
        _number(row.get("amount")) for row in portfolio.get("history", [])
        if isinstance(row, dict) and row.get("type") == "MONTHLY_CONTRIBUTION"
        and row.get("month") == key
    )
    return {"month": key, "plan": plan, "recorded": recorded,
            "remaining": max(0.0, round(plan - recorded, 2))}


def contribution_reminder_due(portfolio, today):
    """Return the status to remind about, or None. At most once per month."""
    strategy = portfolio.get("investment_strategy")
    if not isinstance(strategy, dict) or strategy.get("style") != "LONG_TERM":
        return None
    status = contribution_status(portfolio, today)
    if status["plan"] <= 0 or status["remaining"] <= 0 or today.day < reminder_day(portfolio):
        return None
    previous = portfolio.get("contribution_reminder") or {}
    if previous.get("month") == status["month"] and previous.get("status") in {"SENT", "PENDING"}:
        return None
    return status


def format_contribution_reminder(status):
    return (
        f"📥 ถึงเวลาเติมเงินเดือน {status['month']}\n"
        f"แผน {status['plan']:,.0f} บาท | บันทึกแล้ว {status['recorded']:,.0f} บาท | "
        f"คงเหลือ {status['remaining']:,.0f} บาท\n"
        "โอนเงินเข้าบัญชีโบรกเกอร์จริงก่อน แล้วกดปุ่ม เติมเงินตามแผน เพื่อบันทึกยอดคงเหลือ "
        "หากโอนไม่เท่าแผน พิมพ์ บันทึกเงินเติม ตามยอดจริง\n"
        "บอตไม่ได้โอนเงินให้ ปุ่มจะบันทึกให้ครั้งเดียวต่อเดือนเท่านั้น"
    )


def confirm_planned_contribution(portfolio, today, recorded_at):
    """Record the remaining planned deposit once; returns a status dict."""
    status = contribution_status(portfolio, today)
    if status["plan"] <= 0:
        return {"ok": False, "reason": "NO_PLAN", **status}
    if status["remaining"] <= 0:
        return {"ok": False, "reason": "ALREADY_RECORDED", **status}
    amount = status["remaining"]
    portfolio["cash"] = round(_number(portfolio.get("cash")) + amount, 2)
    portfolio.setdefault("history", []).append({
        "type": "MONTHLY_CONTRIBUTION", "amount": amount, "month": status["month"],
        "source": "PLAN_CONFIRM", "recorded_at": recorded_at,
    })
    portfolio["overnight_plan"] = None
    return {"ok": True, "amount": amount, **status}


def _plus_year(day):
    try:
        return day.replace(year=day.year + 1)
    except ValueError:  # 29 February
        return day.replace(year=day.year + 1, day=28)


def estimate_next_event(history, today, window=EX_DATE_WINDOW_DAYS, tolerance=30):
    """Project last year's ex-dividend dates forward. Returns an event or None.

    `history` is a list of (date, per_share_amount). A date counts only when
    the same payment happened one year earlier and no payment has already
    occurred near the projected date this year. This is an estimate: the
    company may skip, move or change the dividend.
    """
    candidates = []
    for day, amount in history:
        projected = _plus_year(day)
        if not 0 <= (projected - today).days <= window:
            continue
        if any(abs((d - projected).days) <= tolerance and d <= today for d, _ in history):
            continue
        candidates.append({"ex_date": projected, "amount": _number(amount) or None,
                           "estimated": True})
    return min(candidates, key=lambda e: e["ex_date"]) if candidates else None


def upcoming_dividends(holdings, events, today, window=EX_DATE_WINDOW_DAYS):
    """Holdings with an ex-dividend date within `window` days.

    `events` maps symbol -> {'ex_date': date, 'amount': float|None}. The share
    count is today's holding, which is a ceiling guess: only shares owned the
    day before the ex-date earn the payment.
    """
    rows = []
    for symbol, holding in sorted(holdings.items()):
        event = events.get(symbol)
        shares = int(_number(holding.get("shares")))
        if not event or shares <= 0 or not isinstance(event.get("ex_date"), date):
            continue
        days = (event["ex_date"] - today).days
        if not 0 <= days <= window:
            continue
        per_share = _number(event.get("amount"))
        rows.append({"symbol": symbol, "ex_date": event["ex_date"], "days": days,
                     "per_share": per_share or None, "shares": shares,
                     "gross": round(per_share * shares, 2) if per_share else None,
                     "net": round(per_share * shares * (1 - DIVIDEND_TAX_RATE), 2) if per_share else None,
                     "estimated": bool(event.get("estimated"))})
    return sorted(rows, key=lambda r: (r["ex_date"], r["symbol"]))


def format_dividend_calendar(rows, window=EX_DATE_WINDOW_DAYS, failed=()):
    names = ", ".join(s.removesuffix(".BK") for s in failed)
    if not rows and failed:
        return ("⚠️ ดึงข้อมูลปฏิทินปันผลไม่สำเร็จ: " + names + "\n"
                "นี่ไม่ได้แปลว่าไม่มี XD ตรวจวัน XD ที่ SET หรือแอปโบรกเกอร์ก่อน")
    if not rows:
        return f"🗓 ไม่พบหุ้นในพอร์ตที่ขึ้น XD ภายใน {window} วัน (ตามข้อมูลที่ดึงได้ อาจไม่ครบ)"
    lines = [f"🗓 หุ้นที่จะขึ้น XD ภายใน {window} วัน", "━━━━━━━━━━━━━━━━━━"]
    for r in rows:
        when = "วันนี้" if r["days"] == 0 else f"อีก {r['days']} วัน"
        tag = " ประมาณการจากปีก่อน" if r["estimated"] else ""
        lines.append(f"• {r['symbol'].removesuffix('.BK')} XD {r['ex_date'].isoformat()} ({when}){tag}")
        if r["per_share"]:
            lines.append(f"  ประมาณ {r['per_share']:.4f} บาท/หุ้น × {r['shares']:,} หุ้น "
                         f"= {r['gross']:,.2f} บาท ก่อนภาษี (หลังภาษี 10% ≈ {r['net']:,.2f})")
        else:
            lines.append("  ยังไม่ทราบอัตราปันผลต่อหุ้น")
    lines += ["",
              "วันและยอดเป็นประมาณการจากรอบปีก่อน บริษัทอาจเลื่อน งด หรือเปลี่ยนยอด ยืนยันจากประกาศ SET ก่อน",
              "ต้องถือหุ้นไว้ก่อนวัน XD จึงมีสิทธิ จำนวนเงินจริงและวันจ่ายดูจากประกาศบริษัท",
              "เมื่อเงินเข้าบัญชีจริง ให้บันทึกด้วย บันทึกปันผล SYMBOL ก่อนภาษี ภาษี วันรับ เลขอ้างอิง",
              "ข้อมูลจากแหล่งฟรี อาจคลาดเคลื่อน ตรวจกับ SET ก่อนตัดสินใจ"]
    if failed:
        lines.append("⚠️ ดึงข้อมูลไม่สำเร็จ ไม่ได้ตรวจ: " + names)
    return "\n".join(lines)


def monthly_summary(portfolio, month_start):
    """Summary of the calendar month beginning at `month_start` (a date)."""
    last = calendar.monthrange(month_start.year, month_start.month)[1]
    key = month_key(month_start)
    first_day, last_day = month_start.replace(day=1).isoformat(), month_start.replace(day=last).isoformat()
    history = [r for r in portfolio.get("history", []) if isinstance(r, dict)]
    contributions = sum(_number(r.get("amount")) for r in history
                        if r.get("type") == "MONTHLY_CONTRIBUTION" and r.get("month") == key)
    reversed_refs = {r.get("reference") for r in history if r.get("type") == "DISTRIBUTION_REVERSAL"}
    dividends = capital = 0.0
    for r in history:
        paid = r.get("paid_date")
        if (r.get("type") in {"DIVIDEND", "CAPITAL_RETURN"} and isinstance(paid, str)
                and first_day <= paid <= last_day and r.get("reference") not in reversed_refs):
            if r["type"] == "DIVIDEND":
                dividends += _number(r.get("amount"))
            else:
                capital += _number(r.get("amount"))
    buys = sum(1 for r in history if r.get("type") == "RECORDED_BUY"
               and str(r.get("recorded_at", ""))[:7] == key)
    sells = sum(1 for r in history if r.get("type") == "RECORDED_SELL"
                and str(r.get("recorded_at", ""))[:7] == key)
    strategy = portfolio.get("investment_strategy")
    plan = _number(strategy.get("monthly_contribution_baht")) if isinstance(strategy, dict) else 0.0
    goal = portfolio.get("dividend_goal")
    return {"month": key, "plan": plan, "contributions": contributions,
            "shortfall": max(0.0, plan - contributions), "dividends": dividends,
            "capital_returns": capital, "buys": buys, "sells": sells,
            "cash": _number(portfolio.get("cash")),
            "goal_monthly": _number(goal.get("monthly_net")) if isinstance(goal, dict) else 0.0}


def format_monthly_summary(s):
    lines = [f"📅 สรุปเดือน {s['month']}", "━━━━━━━━━━━━━━━━━━"]
    if s["plan"]:
        mark = "✅" if s["shortfall"] <= 0 else "⚠️"
        lines.append(f"{mark} เติมเงิน {s['contributions']:,.0f} / แผน {s['plan']:,.0f} บาท"
                     + (f" (ขาด {s['shortfall']:,.0f})" if s["shortfall"] > 0 else ""))
    else:
        lines.append(f"เติมเงินที่บันทึก {s['contributions']:,.0f} บาท (ยังไม่ได้ตั้งแผนเติมเงิน)")
    lines.append(f"💰 ปันผลสุทธิที่รับจริง {s['dividends']:,.2f} บาท"
                 + (f" (เป้า {s['goal_monthly']:,.0f} บาท/เดือน)" if s["goal_monthly"] else ""))
    if s["capital_returns"]:
        lines.append(f"↩️ เงินคืนทุน {s['capital_returns']:,.2f} บาท (ไม่นับเป็นปันผล)")
    lines.append(f"🔁 บันทึกซื้อ {s['buys']} ครั้ง ขาย {s['sells']} ครั้ง")
    lines.append(f"💵 เงินสดในบอต {s['cash']:,.2f} บาท" + (" — ยังไม่ได้นำไปลงทุน" if s["cash"] >= 1000 else ""))
    lines.append("ปันผลเดือนเดียวไม่ใช่ค่าเฉลี่ย ดูรายได้ย้อนหลัง 12 เดือนด้วย รายได้ปันผล")
    return "\n".join(lines)
