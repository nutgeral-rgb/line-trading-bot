"""A small, separately accounted working-capital pool for short-term rotation.

The pool is a ledger the user keeps in sync with real trades. It never sends
orders. Cash moved into it leaves the main portfolio cash, and its positions
live outside ``portfolio["stocks"]`` so the long-term plan, position cap and
dividend tracking never see them. Caller owns the per-user transaction lock.
"""
import math

from dividend_goal import money

VERSION = 1
DEFAULT_RISK_PER_TRADE = 0.04   # share of sleeve equity risked per position
DEFAULT_MAX_POSITIONS = 2
HALT_DRAWDOWN = 0.10            # stop opening positions after a 10% fall
MAX_LOG = 20


def _round(value):
    return round(float(value), 2)


def _log(sleeve, kind, amount, recorded_at, symbol=None):
    entry = {"type": kind, "amount": _round(amount), "at": recorded_at}
    if symbol:
        entry["symbol"] = symbol
    sleeve["log"] = (sleeve.get("log", []) + [entry])[-MAX_LOG:]


def new_sleeve():
    return {"version": VERSION, "cash": 0.0, "net_in": 0.0, "positions": {},
            "realized_pnl": 0.0, "risk_per_trade": DEFAULT_RISK_PER_TRADE,
            "max_positions": DEFAULT_MAX_POSITIONS, "log": []}


def transfer_in(portfolio, amount, recorded_at):
    """Move cash from the main portfolio into the sleeve (creating it)."""
    amount = money(amount, positive=True)
    cash = money(portfolio.get("cash", 0))
    if amount > cash + 0.005:
        raise ValueError(f"เงินสดในพอร์ตหลัก {cash:,.2f} บาท ไม่พอโอน {amount:,.2f} บาท "
                         "(ถ้าเพิ่งฝากเงิน ให้ บันทึกเงินเติม ก่อน)")
    sleeve = portfolio.setdefault("rotation_sleeve", new_sleeve())
    portfolio["cash"] = _round(cash - amount)
    sleeve["cash"] = _round(sleeve["cash"] + amount)
    sleeve["net_in"] = _round(sleeve["net_in"] + amount)
    _log(sleeve, "TRANSFER_IN", amount, recorded_at)
    portfolio["overnight_plan"] = None
    return sleeve


def transfer_out(portfolio, amount, recorded_at):
    amount = money(amount, positive=True)
    sleeve = portfolio.get("rotation_sleeve")
    if not isinstance(sleeve, dict):
        raise ValueError("ยังไม่ได้ตั้งถุงหมุนเวียน")
    if amount > sleeve["cash"] + 0.005:
        raise ValueError(f"เงินสดในถุง {sleeve['cash']:,.2f} บาท ไม่พอโอนออก {amount:,.2f} บาท")
    sleeve["cash"] = _round(sleeve["cash"] - amount)
    sleeve["net_in"] = _round(sleeve["net_in"] - amount)
    portfolio["cash"] = _round(money(portfolio.get("cash", 0)) + amount)
    _log(sleeve, "TRANSFER_OUT", amount, recorded_at)
    portfolio["overnight_plan"] = None
    return sleeve


def _trade_inputs(shares, price, fee, estimate_rate):
    if isinstance(shares, bool) or not isinstance(shares, int) or not 0 < shares <= 1_000_000_000:
        raise ValueError("จำนวนหุ้นต้องเป็นจำนวนเต็มมากกว่า 0")
    price = money(price, positive=True)
    gross = shares * price
    fee = gross * estimate_rate if fee is None else money(fee)
    if fee > gross * 0.05:
        raise ValueError("ค่าใช้จ่ายสูงผิดปกติ ตรวจตัวเลขอีกครั้ง")
    return price, gross, fee


def record_buy(portfolio, symbol, shares, price, fee, today, recorded_at, estimate_rate):
    """Returns (message_info dict). Rejects overspending; warns above the cap."""
    sleeve = portfolio.get("rotation_sleeve")
    if not isinstance(sleeve, dict):
        raise ValueError("ยังไม่ได้ตั้งถุงหมุนเวียน พิมพ์ ตั้งถุงหมุนเวียน 12000")
    price, gross, fee = _trade_inputs(shares, price, fee, estimate_rate)
    total = gross + fee
    if total > sleeve["cash"] + 0.005:
        raise ValueError(f"เงินสดในถุง {sleeve['cash']:,.2f} บาท ไม่พอซื้อรวมค่าใช้จ่าย {total:,.2f} บาท")
    positions = sleeve["positions"]
    old = positions.get(symbol, {})
    old_shares, old_cost = int(old.get("shares", 0)), float(old.get("avg_price", 0))
    new_shares = old_shares + shares
    over_cap = symbol not in positions and len(positions) >= sleeve["max_positions"]
    positions[symbol] = {"shares": new_shares,
                         "avg_price": round((old_shares * old_cost + total) / new_shares, 4),
                         "opened": old.get("opened", today)}
    sleeve["cash"] = _round(sleeve["cash"] - total)
    _log(sleeve, "BUY", total, recorded_at, symbol)
    return {"shares": new_shares, "cash": sleeve["cash"], "fee": fee, "over_cap": over_cap}


def record_sell(portfolio, symbol, shares, price, fee, recorded_at, estimate_rate):
    sleeve = portfolio.get("rotation_sleeve")
    if not isinstance(sleeve, dict):
        raise ValueError("ยังไม่ได้ตั้งถุงหมุนเวียน")
    price, gross, fee = _trade_inputs(shares, price, fee, estimate_rate)
    held = sleeve["positions"].get(symbol)
    if not held or shares > int(held["shares"]):
        have = int(held["shares"]) if held else 0
        raise ValueError(f"ถุงหมุนเวียนถือ {symbol.removesuffix('.BK')} {have:,} หุ้น ขาย {shares:,} หุ้นไม่ได้")
    pnl = gross - fee - shares * float(held["avg_price"])
    remaining = int(held["shares"]) - shares
    if remaining:
        held["shares"] = remaining
    else:
        del sleeve["positions"][symbol]
    sleeve["cash"] = _round(sleeve["cash"] + gross - fee)
    sleeve["realized_pnl"] = _round(sleeve["realized_pnl"] + pnl)
    _log(sleeve, "SELL", gross - fee, recorded_at, symbol)
    return {"remaining": remaining, "cash": sleeve["cash"], "fee": fee, "pnl": pnl}


def equity(sleeve, prices):
    """Cash plus positions at `prices` ({symbol: close}); None if any is missing."""
    total = float(sleeve["cash"])
    for symbol, pos in sleeve["positions"].items():
        price = prices.get(symbol)
        if price is None or not math.isfinite(price) or price <= 0:
            return None
        total += int(pos["shares"]) * price
    return total


def drawdown(sleeve, current_equity):
    """Fractional fall of equity below the net cash put in (0 if above)."""
    if sleeve["net_in"] <= 0 or current_equity is None:
        return None
    return max(0.0, 1 - current_equity / sleeve["net_in"])


def is_halted(sleeve, current_equity):
    fall = drawdown(sleeve, current_equity)
    return fall is not None and fall >= HALT_DRAWDOWN - 1e-9


def validate_sleeve(obj):
    """Validate a restored sleeve; returns a clean copy or raises ValueError."""
    if not isinstance(obj, dict) or obj.get("version") != VERSION:
        raise ValueError("รูปแบบถุงหมุนเวียนไม่ถูกต้อง")
    clean = new_sleeve()
    for key in ("cash", "net_in", "realized_pnl"):
        value = obj.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                or abs(value) > 1e12 or (key == "cash" and value < 0):
            raise ValueError("ตัวเลขถุงหมุนเวียนไม่ถูกต้อง")
        clean[key] = _round(value)
    risk = obj.get("risk_per_trade", DEFAULT_RISK_PER_TRADE)
    cap = obj.get("max_positions", DEFAULT_MAX_POSITIONS)
    if isinstance(risk, bool) or not isinstance(risk, (int, float)) or not 0 < risk <= 0.1:
        raise ValueError("ความเสี่ยงต่อรายการของถุงหมุนเวียนไม่ถูกต้อง")
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 5:
        raise ValueError("จำนวนตำแหน่งของถุงหมุนเวียนไม่ถูกต้อง")
    clean["risk_per_trade"], clean["max_positions"] = float(risk), cap
    positions = obj.get("positions", {})
    if not isinstance(positions, dict) or len(positions) > 10:
        raise ValueError("รายการหุ้นในถุงหมุนเวียนไม่ถูกต้อง")
    for symbol, pos in positions.items():
        if (not isinstance(symbol, str) or not isinstance(pos, dict)
                or isinstance(pos.get("shares"), bool) or not isinstance(pos.get("shares"), int)
                or pos["shares"] <= 0):
            raise ValueError("หุ้นในถุงหมุนเวียนไม่ถูกต้อง")
        avg = money(pos.get("avg_price"), positive=True)
        clean["positions"][symbol] = {"shares": pos["shares"], "avg_price": avg,
                                      "opened": str(pos.get("opened", ""))[:10]}
    return clean


def format_status(sleeve, prices=None):
    lines = ["🔄 ถุงหมุนเวียน (บัญชีแยกจากพอร์ตสะสมปันผล)", "━━━━━━━━━━━━━━━━━━",
             f"💵 เงินสดในถุง: {sleeve['cash']:,.2f} บาท",
             f"📥 เงินที่ใส่สุทธิ: {sleeve['net_in']:,.2f} บาท",
             f"✅ กำไร/ขาดทุนที่รับรู้แล้ว: {sleeve['realized_pnl']:+,.2f} บาท"]
    if sleeve["positions"]:
        lines.append("📌 หุ้นในถุง")
        for symbol, pos in sleeve["positions"].items():
            last = (prices or {}).get(symbol)
            tail = (f" | ราคาล่าสุด {last:.2f} ({(last / pos['avg_price'] - 1) * 100:+.1f}%)"
                    if last else "")
            lines.append(f"• {symbol.removesuffix('.BK')} | {pos['shares']:,} หุ้น | "
                         f"ต้นทุน {pos['avg_price']:.2f}{tail}")
    else:
        lines.append("📌 ยังไม่มีหุ้นในถุง")
    now = equity(sleeve, prices or {}) if prices is not None else None
    fall = drawdown(sleeve, now)
    if now is not None:
        lines.append(f"💰 มูลค่ารวมประมาณ {now:,.2f} บาท"
                     + (f" | ต่ำกว่าเงินที่ใส่ {fall * 100:.1f}%" if fall else ""))
        if is_halted(sleeve, now):
            lines.append(f"⛔ ลดลงถึง {HALT_DRAWDOWN:.0%} จากเงินที่ใส่ บอตงดเสนอเปิดสถานะใหม่ ทบทวนกลยุทธ์ก่อน")
    lines.append(f"กติกา: เสี่ยงไม่เกิน {sleeve['risk_per_trade']:.0%} ของถุงต่อหุ้น 1 ตัว "
                 f"ถือไม่เกิน {sleeve['max_positions']} ตัว งดเปิดสถานะใหม่เมื่อถุงลดลง {HALT_DRAWDOWN:.0%}")
    return "\n".join(lines)
