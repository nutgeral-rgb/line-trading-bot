from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import logging
import os
import sys

from apscheduler.schedulers.background import (
    BackgroundScheduler,
)
exchange_calendars_available = True
try:
    import exchange_calendars as xcals
except ImportError:
    exchange_calendars_available = False

from flask import Flask, abort, request
import linebot
from linebot import (
    LineBotApi,
    WebhookHandler,
)
from linebot.exceptions import (
    InvalidSignatureError,
)
from linebot.models import (
    MessageEvent,
    TextMessage,
    TextSendMessage,
)
import numpy as np
import pandas as pd
import requests

from fundamental_analysis import analyze_fundamentals
from set100_constituents import SET100_SYMBOLS

# =========================================================
# CONFIGURATION & LOGGING
# =========================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv(
    "LINE_CHANNEL_ACCESS_TOKEN", ""
)
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    logger.warning(
        "ยังไม่ได้ตั้งค่า LINE Token หรือ Secret ใน Environment Variables"
    )

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

PORTFOLIO_FILE = "portfolio.json"

INDEX_SYMBOL = "SET.BK"
UNIVERSE = SET100_SYMBOLS

MAX_POSITIONS = 5
MIN_UNIVERSE_COVERAGE = 0.80
MOMENTUM_LOOKBACK = 63
VOLATILITY_LOOKBACK = 20
MIN_AVG_DAILY_VALUE_BAHT = float(os.getenv("MIN_AVG_DAILY_VALUE_BAHT", "5000000"))

# =========================================================
# CALENDAR FUNCTIONS (exchange_calendars XBKK)
# =========================================================
def is_set_session(day):
    if not exchange_calendars_available:
        return day.weekday() < 5
    try:
        calendar = xcals.get_calendar("XBKK")
        return bool(calendar.is_session(day.isoformat()))
    except Exception:
        logger.exception("ตรวจวันซื้อขาย SET ไม่สำเร็จ")
        return day.weekday() < 5


def next_set_session(day):
    if not exchange_calendars_available:
        candidate = day + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate
    try:
        calendar = xcals.get_calendar("XBKK")
        next_day = day + timedelta(days=1)
        return calendar.date_to_session(
            next_day.isoformat(),
            direction="next",
        ).date()
    except Exception:
        logger.exception("หาวันซื้อขายถัดไปไม่สำเร็จ")
        candidate = day + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate


# =========================================================
# TIME & PORTFOLIO STORAGE
# =========================================================
def now_bkk():
    return datetime.utcnow() + timedelta(hours=7)


def utc_text():
    return now_bkk().strftime("%Y-%m-%d %H:%M:%S")


def load_all_portfolios():
    if not os.path.exists(PORTFOLIO_FILE):
        return {}
    try:
        with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("อ่านไฟล์พอร์ตไม่สำเร็จ")
        return {}


def save_all_portfolios(data):
    try:
        with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("บันทึกไฟล์พอร์ตไม่สำเร็จ")


def get_portfolio(user_id):
    all_p = load_all_portfolios()
    if user_id not in all_p:
        all_p[user_id] = {
            "cash": 10000.0,
            "stocks": {},
            "history": [],
            "watchlist": [],
            "overnight_plan": None,
        }
        save_all_portfolios(all_p)
    return all_p[user_id]


def mutate_portfolio(user_id, func):
    all_p = load_all_portfolios()
    if user_id not in all_p:
        all_p[user_id] = {
            "cash": 10000.0,
            "stocks": {},
            "history": [],
            "watchlist": [],
            "overnight_plan": None,
        }
    p = all_p[user_id]
    res = func(p)
    save_all_portfolios(all_p)
    return res


def registered_users():
    return list(load_all_portfolios().keys())


# =========================================================
# MARKET DATA & ANALYSIS
# =========================================================
def get_stock_history(symbol, period="3mo"):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={period}&interval=1d"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return None
        res = r.json()
        result = res["chart"]["result"][0]
        timestamp = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        adjusted = result.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose")
        close = quote.get("close")
        df = pd.DataFrame({
            "Open": quote.get("open"),
            "High": quote.get("high"),
            "Low": quote.get("low"),
            "Close": close,
            "Adj Close": adjusted if adjusted is not None else close,
            "Volume": quote.get("volume"),
        }, index=pd.to_datetime(timestamp, unit="s"))
        df.sort_index(inplace=True)
        df.dropna(subset=["Close", "Adj Close"], inplace=True)
        return df if not df.empty else None
    except Exception:
        return None


def get_market_frames(symbols):
    unique_symbols = sorted(set(symbols))
    if not unique_symbols:
        return {}

    def fetch_one(sym):
        return sym, get_stock_history(sym, period="6mo")

    frames = {}
    worker_count = min(8, len(unique_symbols))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for sym, df in executor.map(fetch_one, unique_symbols):
            if df is not None and len(df) >= 20:
                frames[sym] = df
    return frames


def combined_universe(portfolio):
    custom = portfolio.get("watchlist", [])
    return sorted(set(UNIVERSE + custom))


def _close_and_sma20(df, as_of):
    if df is None or len(df) < 20 or df.index[-1].strftime("%Y-%m-%d") != as_of:
        return None
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    closes = df[close_col].dropna()
    if len(closes) < 20 or closes.index[-1].strftime("%Y-%m-%d") != as_of:
        return None
    close = float(closes.iloc[-1])
    sma20 = float(closes.rolling(20).mean().iloc[-1])
    if not pd.notna(close) or not pd.notna(sma20) or sma20 <= 0:
        return None
    return close, sma20


def _selection_metrics(df, as_of):
    """Return risk-adjusted momentum, return, volatility, and liquidity."""
    if df is None or "Volume" not in df.columns:
        return None
    history = df.loc[df.index.strftime("%Y-%m-%d") <= as_of].copy()
    if history.empty or history.index[-1].strftime("%Y-%m-%d") != as_of:
        return None
    close_col = "Adj Close" if "Adj Close" in history.columns else "Close"
    closes = pd.to_numeric(history[close_col], errors="coerce").dropna()
    if len(closes) < MOMENTUM_LOOKBACK + 1:
        return None
    recent = history.loc[closes.index].tail(MOMENTUM_LOOKBACK + 1)
    recent_close = pd.to_numeric(recent[close_col], errors="coerce")
    returns = recent_close.pct_change().dropna()
    if len(returns) < MOMENTUM_LOOKBACK:
        return None
    momentum = float(recent_close.iloc[-1] / recent_close.iloc[0] - 1)
    volatility = float(returns.tail(VOLATILITY_LOOKBACK).std(ddof=1) * np.sqrt(252))
    traded_value = (
        pd.to_numeric(recent["Close"], errors="coerce")
        * pd.to_numeric(recent["Volume"], errors="coerce")
    ).tail(VOLATILITY_LOOKBACK)
    valid_traded_value = traded_value.replace([np.inf, -np.inf], np.nan).dropna()
    if len(valid_traded_value) < VOLATILITY_LOOKBACK:
        return None
    avg_value = float(valid_traded_value.median())
    if not all(np.isfinite(x) for x in (momentum, volatility, avg_value)):
        return None
    if volatility <= 0 or avg_value <= 0:
        return None
    return momentum / volatility, momentum, volatility, avg_value


def market_context(frames):
    idx_df = frames.get(INDEX_SYMBOL)
    if idx_df is None or len(idx_df) < 20:
        return {
            "sufficient": False, "date": "", "breadth": 0,
            "coverage": 0, "pass": False,
        }

    as_of = idx_df.index[-1].strftime("%Y-%m-%d")
    valid = {}
    for sym in UNIVERSE:
        values = _close_and_sma20(frames.get(sym), as_of)
        if values is not None:
            valid[sym] = values

    coverage = len(valid) / len(UNIVERSE) if UNIVERSE else 0
    if coverage < MIN_UNIVERSE_COVERAGE:
        return {
            "sufficient": False, "date": as_of, "breadth": 0,
            "coverage": coverage, "pass": False,
        }

    above_count = sum(close > sma20 for close, sma20 in valid.values())
    breadth = above_count / len(valid) * 100
    return {
        "sufficient": True,
        "date": as_of,
        "breadth": breadth,
        "coverage": coverage,
        "pass": breadth >= 50.0,
    }


def build_daily_plan(portfolio, frames=None):
    symbols = combined_universe(portfolio)
    if frames is None:
        symbols_to_load = sorted(set(UNIVERSE + symbols + list(portfolio["stocks"]) + [INDEX_SYMBOL]))
        frames = get_market_frames(symbols_to_load)

    ctx = market_context(frames)
    report_lines = []
    selected = []
    now_str = now_bkk().strftime("%Y-%m-%d %H:%M")
    data_date = ctx["date"] if ctx["date"] else "ไม่ทราบ"

    report_lines.append(f"📊 แผนหมุนทุน | จัดทำ {now_str}")
    report_lines.append(f"📅 วันที่ข้อมูลราคา: {data_date}")
    report_lines.append(
        f"🌐 ภาพตลาด: {'ผ่านเกณฑ์' if ctx['pass'] else 'ยังไม่ผ่านเกณฑ์'}"
    )
    report_lines.append(
        f"   Breadth {ctx['breadth']:.1f}%  •  "
        f"ข้อมูลครบ {ctx['coverage'] * 100:.0f}%"
    )

    if not ctx["sufficient"]:
        report_lines.append("")
        report_lines.append(
            f"⚠️ ข้อมูลหุ้นไม่ครบ (ต้องมีอย่างน้อย {MIN_UNIVERSE_COVERAGE:.0%} "
            "ของหุ้นหลักในวันเดียวกัน)"
        )
        report_lines.append("จึงยังไม่สร้างสัญญาณ และควรรอข้อมูลรอบถัดไป")
    elif ctx["pass"]:
        ranked = []
        for sym in symbols:
            values = _close_and_sma20(frames.get(sym), data_date)
            metrics = _selection_metrics(frames.get(sym), data_date)
            if values is None or metrics is None:
                continue
            close, sma20 = values
            score, momentum, volatility, avg_value = metrics
            if close > sma20 and momentum > 0 and avg_value >= MIN_AVG_DAILY_VALUE_BAHT:
                ranked.append((score, sym, momentum, volatility, avg_value))

        ranked.sort(key=lambda item: (-item[0], item[1]))
        selected_rows = ranked[:MAX_POSITIONS]
        selected = [row[1] for row in selected_rows]
        fundamentals = {}
        if selected_rows:
            with ThreadPoolExecutor(max_workers=min(5, len(selected_rows))) as executor:
                fundamentals = dict(zip(
                    selected,
                    executor.map(analyze_fundamentals, selected),
                ))
        report_lines.append("")
        if selected:
            weight = 100 / len(selected)
            report_lines.append(f"✅ หุ้นที่ผ่านเกณฑ์ ({len(selected)} ตัว)")
            for index, (_, sym, momentum, volatility, avg_value) in enumerate(selected_rows, 1):
                fundamental_text = fundamentals.get(sym, {}).get(
                    "summary", "ข้อมูลพื้นฐานไม่พร้อม"
                )
                report_lines.append(
                    f"{index}. 📈 {sym}\\n"
                    f"   ผลตอบแทน 63 วัน: {momentum * 100:+.1f}%\\n"
                    f"   ความผันผวนต่อปี: {volatility * 100:.1f}%\\n"
                    f"   มูลค่าซื้อขายมัธยฐาน: {avg_value:,.0f} บาท/วัน\\n"
                    f"   พื้นฐาน: {fundamental_text}"
                )
            report_lines.append("")
            report_lines.append(f"⚖️ น้ำหนักเป้าหมาย: หุ้นละ {weight:.1f}%")
            report_lines.append("🔁 ใช้สัญญาณราคาปิดเพื่อวางแผนวันซื้อขายถัดไป")
            report_lines.append("ทบทวนขายเมื่อหลุด 5 อันดับแรกหรือปิดต่ำกว่า SMA20")
        else:
            report_lines.append("💵 ยังไม่มีหุ้นผ่านเกณฑ์ จึงแนะนำถือเงินสด")
    else:
        report_lines.append("")
        report_lines.append("🛡️ ตลาดไม่ผ่านเกณฑ์ Breadth")
        report_lines.append("แนะนำถือเงินสดและงดเปิดสถานะใหม่")

    return "\n".join(report_lines), selected




# =========================================================
# OVERNIGHT PLAN SYSTEM
# =========================================================
def read_overnight_plan(user_id):
    portfolio = get_portfolio(user_id)
    plan = portfolio.get("overnight_plan")
    
    if not isinstance(plan, dict):
        return (
            "ยังไม่มีแผนที่ประมวลผลหลังตลาดปิด\n"
            "บอตจะตรวจข้อมูลหลังปิดตลาดในวันซื้อขาย "
            "และเตรียมแผนสำหรับเช้าวันซื้อขายถัดไป"
        )
    
    today = now_bkk().date().isoformat()
    delivery_day = plan.get("delivery_day")
    status = plan.get("status")
    
    if delivery_day != today:
        if status == "READY" and delivery_day > today:
            return (
                f"เตรียมแผนจากราคาปิด {plan['data_date']} แล้ว\n"
                f"รอส่งเช้าวันซื้อขาย {delivery_day}"
            )
        return "ยังไม่มีแผนที่ตรวจสอบได้สำหรับวันนี้ จึงไม่ใช้แผนเก่ามาแนะนำซื้อ"
        
    if status == "READY":
        return "แผนสำหรับวันนี้เตรียมแล้ว แต่ยังไม่ถึงรอบส่งอัตโนมัติ"
        
    if status in ("SENT", "DELIVERY_UNKNOWN"):
        return f"แผนสำหรับวันซื้อขาย {delivery_day}\nใช้ราคาปิด {plan['data_date']}\n\n" + plan["text"]
        
    return "ยังไม่มีแผนที่ใช้ได้สำหรับวันนี้"


def prepare_overnight_plans():
    current_day = now_bkk().date()
    if not is_set_session(current_day):
        return

    try:
        users = registered_users()
    except Exception:
        logger.exception("อ่านรายชื่อผู้ใช้ไม่สำเร็จ")
        return

    pending = {}
    for user_id in users:
        try:
            portfolio = get_portfolio(user_id)
            previous = portfolio.get("overnight_plan")
            if not isinstance(previous, dict) or previous.get("data_date") != current_day.isoformat():
                pending[user_id] = portfolio
        except Exception:
            logger.exception("อ่านพอร์ตเพื่อเตรียมแผนไม่สำเร็จ")

    if not pending:
        return

    symbols = set(UNIVERSE + [INDEX_SYMBOL])
    for portfolio in pending.values():
        symbols.update(combined_universe(portfolio))
        symbols.update(portfolio.get("stocks", {}).keys())

    frames = get_market_frames(symbols)
    ctx = market_context(frames)
    if not ctx["sufficient"] or ctx["date"] != current_day.isoformat():
        logger.info("ยังไม่มีข้อมูล SET100 ครบพอสำหรับสร้างแผนวันที่ %s", current_day)
        return

    for user_id, portfolio in pending.items():
        try:
            report, selected = build_daily_plan(portfolio, frames=frames)
            new_plan = {
                "data_date": current_day.isoformat(),
                "delivery_day": next_set_session(current_day).isoformat(),
                "prepared_at": utc_text(),
                "status": "READY",
                "text": report,
                "selected": selected,
            }

            def save_plan(p):
                p["overnight_plan"] = new_plan
                return "เตรียมแผนแล้ว"

            mutate_portfolio(user_id, save_plan)
        except Exception:
            logger.exception("เตรียมแผนล้มเหลวสำหรับผู้ใช้")


def send_overnight_plans():
    current = now_bkk()
    today = current.date()
    if not is_set_session(today):
        return
    if not ((current.hour == 8 and current.minute >= 30) or (current.hour == 9 and current.minute <= 5)):
        return
        
    try:
        users = registered_users()
    except Exception:
        return
        
    for user_id in users:
        try:
            portfolio = get_portfolio(user_id)
            plan = portfolio.get("overnight_plan")
            if not isinstance(plan, dict):
                continue
            if plan.get("delivery_day") != today.isoformat() or plan.get("status") != "READY":
                continue
                
            def claim(p):
                cur = p.get("overnight_plan")
                if not isinstance(cur, dict) or cur.get("status") != "READY":
                    return "SKIP"
                cur["status"] = "SENT"
                return "CLAIMED"
                
            if mutate_portfolio(user_id, claim) != "CLAIMED":
                continue
                
            line_bot_api.push_message(
                user_id,
                TextSendMessage(
                    text=f"แผนเช้าวันซื้อขาย {today.isoformat()}\nข้อมูลราคาปิด {plan['data_date']}\n\n" + plan["text"]
                )
            )
        except Exception:
            logger.exception("ส่งแผนล้มเหลว")


# =========================================================
# LINE WEBHOOK & COMMANDS
# =========================================================
@app.route("/", methods=["GET"])
def index():
    return "Bot is running!", 200


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    
    response_text = command_reply(user_id, text)
    if response_text:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=response_text)
        )


def help_text():
    return (
        "📊 PORTFOLIO BOT\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📈 แผนหุ้น\n"
        "แผนวันนี้ — ดูแผนหุ้นสำหรับวันซื้อขายนี้\n"
        "หรือพิมพ์: แผนเทรดวันนี้ / ควรซื้อวันนี้ / ซื้อวันนี้\n\n"
        "💼 พอร์ตและรายการติดตาม\n"
        "ดูพอร์ต — ดูเงินสดและหุ้นที่บอตบันทึก\n"
        "รายการติดตาม — ดูหุ้นที่ติดตาม\n"
        "ติดตาม AOT — เพิ่มหุ้นในรายการและใช้พิจารณาแผน\n"
        "เลิกติดตาม AOT — นำหุ้นออกจากรายการ\n\n"
        "❔ เมนูคำสั่ง\n"
        "พิมพ์: เมนู หรือ ช่วยเหลือ\n\n"
        "หมายเหตุ: บอตให้ข้อมูลประกอบการตัดสินใจ ไม่ส่งคำสั่งซื้อขายจริง"
    )



def command_reply(user_id, text):
    if text in {"เมนู", "ช่วยเหลือ", "คำสั่ง", "menu", "help"}:
        return help_text()

    portfolio = get_portfolio(user_id)
    
    if text in {"แผนเทรดวันนี้", "แผนวันนี้", "ควรซื้อวันนี้", "ซื้อวันนี้"}:
        return read_overnight_plan(user_id)
        
    if text == "ดูพอร์ต":
        cash = portfolio["cash"]
        stocks = portfolio["stocks"]
        total_stock_cost = sum(
            stock["shares"] * stock["avg_price"] for stock in stocks.values()
        )
        total_value = cash + total_stock_cost
        lines = [
            "💼 สรุปพอร์ต",
            "━━━━━━━━━━━━━━━━━━",
            f"💵 เงินสด: {cash:,.2f} บาท",
            f"📦 มูลค่าหุ้นตามต้นทุนเฉลี่ย: {total_stock_cost:,.2f} บาท",
            f"💰 รวมตามข้อมูลที่บันทึก: {total_value:,.2f} บาท",
            "",
        ]
        if stocks:
            lines.append("📌 หุ้นที่บันทึกไว้")
            for sym, info in stocks.items():
                lines.append(
                    f"• {sym}  |  {info['shares']} หุ้น  |  "
                    f"ต้นทุนเฉลี่ย {info['avg_price']:.2f} บาท"
                )
        else:
            lines.append("📌 ยังไม่มีหุ้นที่บันทึกไว้")
        lines.extend([
            "",
            "หมายเหตุ: มูลค่าหุ้นคำนวณจากต้นทุนเฉลี่ย ไม่ใช่ราคาตลาดปัจจุบัน",
        ])
        return "\n".join(lines)

    if text == "รายการติดตาม":
        watchlist = portfolio.get("watchlist", [])
        if not watchlist:
            return (
                "🔎 รายการติดตามยังว่างอยู่\n"
                "ลองพิมพ์: ติดตาม AOT"
            )
        return "🔎 หุ้นที่ติดตาม\n" + "\n".join(
            f"• {symbol}" for symbol in watchlist
        )

    if text.startswith("ติดตาม "):
        sym = text.replace("ติดตาม", "").strip().upper()
        if not sym.endswith(".BK"):
            sym += ".BK"
        def add_w(p):
            if "watchlist" not in p:
                p["watchlist"] = []
            if sym not in p["watchlist"]:
                p["watchlist"].append(sym)
                return f"เพิ่ม {sym} ลงในรายการติดตามแล้ว"
            return f"{sym} อยู่ในรายการติดตามอยู่แล้ว"
        return mutate_portfolio(user_id, add_w)
        
    if text.startswith("เลิกติดตาม "):
        sym = text.replace("เลิกติดตาม", "").strip().upper()
        if not sym.endswith(".BK"):
            sym += ".BK"
        def remove_w(p):
            if sym in p.get("watchlist", []):
                p["watchlist"].remove(sym)
                return f"นำ {sym} ออกจากรายการติดตามแล้ว"
            return f"ไม่พบ {sym} ในรายการติดตาม"
        return mutate_portfolio(user_id, remove_w)

    return "ไม่รู้จักคำสั่งนี้" + chr(10) + chr(10) + help_text()


# =========================================================
# SCHEDULER STARTUP
# =========================================================
scheduler = BackgroundScheduler(timezone="Asia/Bangkok")
scheduler.add_job(
    prepare_overnight_plans,
    "cron",
    day_of_week="mon-fri",
    hour="19,20,21",
    minute=15,
    id="prepare-overnight-plan",
    replace_existing=True,
)
scheduler.add_job(
    send_overnight_plans,
    "cron",
    day_of_week="mon-fri",
    hour=8,
    minute="30,35,40,45,50,55",
    id="send-overnight-plan-8",
    replace_existing=True,
)
scheduler.add_job(
    send_overnight_plans,
    "cron",
    day_of_week="mon-fri",
    hour=9,
    minute="0,5",
    id="send-overnight-plan-9",
    replace_existing=True,
)
scheduler.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
