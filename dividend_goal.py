"""Deterministic nominal-THB scenarios and actual cash distributions.

No market forecasts, brokerage orders or automatic portfolio initialization.
Caller owns the existing per-user transaction lock.
"""
from datetime import date, timedelta
import math
import re


def money(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError('ยอดเงินต้องเป็นตัวเลข')
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError('ยอดเงินต้องเป็นตัวเลข') from exc
    if not math.isfinite(result) or result < 0 or result > 1e12 or (positive and result == 0):
        raise ValueError('ยอดเงินต้องอยู่ในช่วงที่ถูกต้องและไม่ติดลบ')
    return result


def create_goal(monthly_net, years, today, existing=None):
    monthly_net = money(monthly_net, positive=True)
    if isinstance(years, bool) or not isinstance(years, int) or not 1 <= years <= 50:
        raise ValueError('ระยะเวลาเป็นจำนวนเต็ม 1–50 ปี')
    if (isinstance(existing, dict) and existing.get('monthly_net') == monthly_net
            and existing.get('years') == years):
        validate_goal(existing)
        return dict(existing)
    try:
        target = today.replace(year=today.year + years)
    except ValueError:
        target = today.replace(year=today.year + years, day=28)
    return {'version': 1, 'monthly_net': monthly_net, 'years': years,
            'created_date': today.isoformat(), 'target_date': target.isoformat(),
            'risk_review_pct': 10.0, 'priority': 'FIXED_DEADLINE'}


def validate_goal(goal):
    if not isinstance(goal, dict) or goal.get('version') != 1:
        raise ValueError('รูปแบบเป้าปันผลไม่ถูกต้อง')
    money(goal.get('monthly_net'), positive=True)
    years = goal.get('years')
    if isinstance(years, bool) or not isinstance(years, int) or not 1 <= years <= 50:
        raise ValueError('ระยะเวลาเป้าปันผลไม่ถูกต้อง')
    try:
        start = date.fromisoformat(goal['created_date'])
        end = date.fromisoformat(goal['target_date'])
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError('วันที่เป้าปันผลไม่ถูกต้อง') from exc
    expected = create_goal(goal['monthly_net'], years, start)
    if expected['target_date'] != end.isoformat():
        raise ValueError('วันที่สิ้นสุดไม่ตรงระยะเวลาที่ตั้ง')
    return dict(expected)


def project_goal(goal, capital, contribution, today):
    goal = validate_goal(goal)
    contribution = money(contribution)
    target = date.fromisoformat(goal['target_date'])
    # Count end-of-month deposits remaining before the fixed target day.
    months = max(0, (target.year - today.year) * 12 + target.month - today.month)
    if target <= today:
        months = 0
    annual_income = goal['monthly_net'] * 12
    targets = [annual_income / y for y in (.03, .04, .05)]
    result = {'months': months, 'capital_targets': targets, 'scenarios': []}
    if capital is None:
        return result
    capital = money(capital)
    for annual_return in (.04, .06, .08):
        rate = (1 + annual_return) ** (1 / 12) - 1
        growth = (1 + rate) ** months
        annuity = (growth - 1) / rate if months else 0
        future = capital * growth + contribution * annuity
        required = max(0., (targets[1] - capital * growth) / annuity) if months else None
        result['scenarios'].append({'annual_return': annual_return, 'projected': future,
                                    'required_monthly': required,
                                    'monthly_income_at_net4': future * .04 / 12})
    return result


def record_distribution(portfolio, symbol, gross, tax, paid_date, reference, kind, today):
    gross, tax = round(money(gross, positive=True), 2), round(money(tax), 2)
    if gross <= 0 or tax > gross or kind not in {'DIVIDEND', 'CAPITAL_RETURN'}:
        raise ValueError('ยอดเงินรับหรือภาษีไม่ถูกต้อง')
    if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9]{1,10}(?:-[A-Z0-9]{1,5})?\.BK', symbol):
        raise ValueError('ชื่อหลักทรัพย์ไม่ถูกต้อง')
    if not isinstance(reference, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', reference):
        raise ValueError('เลขอ้างอิงใช้ A–Z a–z 0–9 ขีด และขีดล่าง ไม่เกิน 60 ตัว')
    try:
        paid = date.fromisoformat(paid_date)
    except (TypeError, ValueError) as exc:
        raise ValueError('วันที่ต้องเป็น YYYY-MM-DD') from exc
    if paid > today:
        raise ValueError('บันทึกเมื่อได้รับเงินจริงแล้วเท่านั้น')
    row = {'type': kind, 'symbol': symbol, 'gross': gross, 'tax': tax,
           'amount': round(gross - tax, 2), 'paid_date': paid_date, 'reference': reference}
    history = portfolio.get('history', [])
    for old in history:
        if isinstance(old, dict) and old.get('type') in {'DIVIDEND', 'CAPITAL_RETURN'} and old.get('reference') == reference:
            if all(old.get(k) == v for k, v in row.items()):
                return False
            raise ValueError('เลขอ้างอิงนี้ใช้แล้วแต่ข้อมูลต่างกัน โปรดตรวจรายการเดิม')
    cash = money(portfolio.get('cash', 0))
    new_cash = money(cash + row['amount'])
    row['recorded_at'] = today.isoformat()
    portfolio['cash'] = round(new_cash, 2)
    portfolio.setdefault('history', []).append(row)
    portfolio['overnight_plan'] = None
    return True


def reverse_distribution(portfolio, reference, today):
    history = portfolio.get('history', [])
    if any(isinstance(row, dict) and row.get('type') == 'DISTRIBUTION_REVERSAL'
           and row.get('reference') == reference for row in history):
        return False
    original = next((row for row in history if isinstance(row, dict)
                     and row.get('type') in {'DIVIDEND', 'CAPITAL_RETURN'}
                     and row.get('reference') == reference), None)
    if original is None:
        raise ValueError('ไม่พบเลขอ้างอิงเงินรับนี้')
    amount, cash = money(original['amount']), money(portfolio.get('cash', 0))
    if cash < amount:
        raise ValueError('เงินสดไม่พอหักคืน โปรดกระทบยอดรายการที่ใช้เงินนี้ก่อน')
    portfolio['cash'] = round(cash - amount, 2)
    history.append({'type': 'DISTRIBUTION_REVERSAL', 'reference': reference,
                    'amount': amount, 'recorded_at': today.isoformat()})
    portfolio['overnight_plan'] = None
    return True


def income_summary(portfolio, today):
    history = portfolio.get('history', [])
    reversed_refs = {row.get('reference') for row in history if isinstance(row, dict)
                     and row.get('type') == 'DISTRIBUTION_REVERSAL'}
    dividends, capital_returns, count = 0., 0., 0
    cutoff = today - timedelta(days=365)
    for row in history:
        if not isinstance(row, dict) or row.get('type') not in {'DIVIDEND', 'CAPITAL_RETURN'} or row.get('reference') in reversed_refs:
            continue
        try:
            paid = date.fromisoformat(row['paid_date'])
            amount = money(row['amount'])
        except (KeyError, ValueError, TypeError):
            continue
        if not cutoff < paid <= today:
            continue
        if row['type'] == 'DIVIDEND':
            dividends += amount
            count += 1
        else:
            capital_returns += amount
    return {'net_dividends': dividends, 'monthly_average': dividends / 12,
            'capital_returns': capital_returns, 'count': count}


def validate_distribution_history(history, today):
    """Validate new receipt schemas before any backup replaces a portfolio."""
    receipts, reversals = {}, set()
    for row in history:
        if not isinstance(row, dict):
            raise ValueError('ประวัติแต่ละรายการต้องเป็นข้อมูลรายการ')
        kind = row.get('type')
        if kind is not None and not isinstance(kind, str):
            raise ValueError('ประเภทประวัติไม่ถูกต้อง')
        if kind not in {'DIVIDEND', 'CAPITAL_RETURN', 'DISTRIBUTION_REVERSAL'}:
            continue  # Preserve existing trade/balance journal formats.
        reference = row.get('reference')
        if not isinstance(reference, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', reference):
            raise ValueError('เลขอ้างอิงเงินรับในไฟล์สำรองไม่ถูกต้อง')
        keys = ('amount',) if kind == 'DISTRIBUTION_REVERSAL' else ('gross', 'tax', 'amount')
        for key in keys:
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError('ยอดเงินรับในไฟล์สำรองต้องเป็นตัวเลข')
            checked = money(value)
            if checked != round(checked, 2):
                raise ValueError('ยอดเงินรับต้องไม่เกินสองตำแหน่งทศนิยม')
        try:
            recorded = date.fromisoformat(row['recorded_at'])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError('วันที่บันทึกเงินรับไม่ถูกต้อง') from exc
        if recorded > today:
            raise ValueError('วันที่บันทึกเงินรับยังมาไม่ถึง')
        if kind == 'DISTRIBUTION_REVERSAL':
            original = receipts.get(reference)
            if original is None or reference in reversals or row['amount'] != original['amount']:
                raise ValueError('รายการยกเลิกไม่ตรงเงินรับเดิมหรือซ้ำ')
            if recorded < date.fromisoformat(original['recorded_at']):
                raise ValueError('วันที่ยกเลิกก่อนวันที่บันทึกรายการเดิม')
            reversals.add(reference)
            continue
        if reference in receipts:
            raise ValueError('เลขอ้างอิงเงินรับซ้ำในไฟล์สำรอง')
        if row['amount'] != round(row['gross'] - row['tax'], 2):
            raise ValueError('เงินสุทธิไม่ตรงยอดก่อนภาษีหักภาษี')
        # Reuse all entry validations without mutating the real portfolio.
        check = {'cash': 0, 'history': []}
        record_distribution(check, row.get('symbol'), row['gross'], row['tax'],
                            row.get('paid_date'), reference, kind, recorded)
        receipts[reference] = row
