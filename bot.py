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
UNIVERSE = [
    "ADVANC.BK",
    "AOT.BK",
    "AWC.BK",
    "BANPU.BK",
    "BBL.BK",
    "BCP.BK",
    "BDMS.BK",
    "BEM.BK",
    "BH.BK",
    "BJC.BK",
    "CCET.BK",
    "COM7.BK",
    "CPALL.BK",
    "CPF.BK",
    "CPN.BK",
    "CRC.BK",
    "DELTA.BK",
    "EGCO.BK",
    "GPSC.BK",
    "GULF.BK",
    "HMPRO.BK",
    "IVL.BK",
    "KBANK.BK",
    "KCE.BK",
    "KKP.BK",
    "KTB.BK",
    "KTC.BK",
    "LH.BK",
    "MINT.BK",
    "MRDIYT.BK",
    "MTC.BK",
    "OR.BK",
    "OSP.BK",
    "PTT.BK",
    "PTTEP.BK",
    "PTTGC.BK",
    "RATCH.BK",
    "SCB.BK",
    "SCC.BK",
    "SCGP.BK",
    "TCAP.BK",
    "TFG.BK",
    "THAI.BK",
    "TIDLOR.BK",
    "TISCO.BK",
    "TLI.BK",
    "TOP.BK",
    "TRUE.BK",
    "TTB.BK",
    "TU.BK",
    "VGI.BK",
    "WHA.BK",
]

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
        df = pd.DataFrame({
            "Open": quote.get("open"),
            "High": quote.get("high"),
            "Low": quote.get("low"),
            "Close": quote.get("close"),
            "Volume": quote.get("volume"),
        }, index=pd.to_datetime(timestamp, unit="s"))
        df.dropna(subset=["Close"], inplace=True)
        return df if not df.empty else None
    except Exception:
        return None


def get_market_frames(symbols):
    frames = {}
    for sym in symbols:
        df = get_stock_history(sym, period="3mo")
        if df is not None and len(df) >= 20:
            frames[sym] = df
    return frames


def combined_universe(portfolio):
    custom = portfolio.get("watchlist", [])
    return list(set(UNIVERSE + custom))


def market_context(frames):
    if INDEX_SYMBOL not in frames:
        return {"sufficient": False, "date": "", "breadth": 0, "pass": False}
    idx_df = frames[INDEX_SYMBOL]
    latest_date = idx_df.index[-1].strftime("%Y-%m-%d")
    
    above_count = 0
    total_checked = 0
    for sym, df in frames.items():
        if sym == INDEX_SYMBOL:
            continue
        if len(df) >= 20:
            total_checked += 1
            close = df["Close"].iloc[-1]
            sma20 = df["Close"].rolling(20).mean().iloc[-1]
            if close > sma20:
                above_count += 1
                
    breadth = (above_count / total_checked * 100) if total_checked > 0 else 0
    market_pass = breadth >= 50.0
    return {
        "sufficient": True,
        "date": latest_date,
        "breadth": breadth,
        "pass": market_pass,
    }


def build_daily_plan(portfolio, frames=None):
    symbols = combined_universe(portfolio)
    if frames is None:
        frames = get_market_frames(UNIVERSE + symbols + list(portfolio["stocks"]) + [INDEX_SYMBOL])
    
    ctx = market_context(frames)
    report_lines = []
    selected = []
    
    now_str = now_bkk().strftime("%Y-%m-%d %H:%M")
    data_date = ctx["date"] if ctx["sufficient"] else "ไม่ทราบ"
    
    report_lines.append(f"เวลาใช้งานรายงาน {now_str}")
    report_lines.append(f"แท่งราคาล่าสุด {data_date}")
    report_lines.append(f"ตลาด {'ผ่านเกณฑ์' if ctx['pass'] else 'ไม่ผ่านเกณฑ์'} | Breadth {ctx['breadth']:.1f}%")
    
    if not ctx["sufficient"]:
        report_lines.append("เหตุผล: ข้อมูลดัชนีไม่พร้อมหรือเก่าเกินเกณฑ์")
    
    if ctx["pass"] and ctx["sufficient"]:
        candidates = []
        for sym in symbols:
            if sym in frames:
                df = frames[sym]
                close = df["Close"].iloc[-1]
                sma20 = df["Close"].rolling(20).mean().iloc[-1]
                if close > sma20:
                    candidates.append(sym)
        if candidates:
            selected = candidates[:5]
            report_lines.append(f"หุ้นน่าสนใจ: {', '.join(selected)}")
        else:
            report_lines.append("ยังไม่มีหุ้นเข้าเกณฑ์ซื้อ")
    else:
        report_lines.append("ยังไม่เสนอซื้อใหม่เพราะตลาดรวมไม่ผ่านเกณฑ์")
        
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
        return
        
    for user_id in users:
        try:
            portfolio = get_portfolio(user_id)
            previous = portfolio.get("overnight_plan")
            if isinstance(previous, dict) and previous.get("data_date") == current_day.isoformat():
                continue
                
            symbols = combined_universe(portfolio)
            frames = get_market_frames(UNIVERSE + symbols + list(portfolio["stocks"]) + [INDEX_SYMBOL])
            ctx = market_context(frames)
            
            if not ctx["sufficient"] or ctx["date"] != current_day.isoformat():
                continue
                
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
            logger.exception("เตรียมแผนล้มเหลว")


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


def command_reply(user_id, text):
    portfolio = get_portfolio(user_id)
    
    if text in {"แผนเทรดวันนี้", "แผนวันนี้", "ควรซื้อวันนี้", "ซื้อวันนี้"}:
        return read_overnight_plan(user_id)
        
    if text == "ดูพอร์ต":
        cash = portfolio["cash"]
        stocks = portfolio["stocks"]
        total_stock_val = sum(s["shares"] * s["avg_price"] for s in stocks.values())
        total_val = cash + total_stock_val
        lines = [f"เงินสด: {cash:,.2f} บาท", f"มูลค่าหุ้น: {total_stock_val:,.2f} บาท", f"รวมทั้งหมด: {total_val:,.2f} บาท"]
        if stocks:
            lines.append("หุ้นในพอร์ต:")
            for sym, info in stocks.items():
                lines.append(f"- {sym}: {info['shares']} หุ้น (@{info['avg_price']:.2f})")
        else:
            lines.append("ยังไม่มีหุ้นในพอร์ต")
        return "\n".join(lines)
        
    if text == "รายการติดตาม":
        watchlist = portfolio.get("watchlist", [])
        if not watchlist:
            return "ยังไม่มีรายการติดตาม (เพิ่มได้ด้วยคำสั่ง 'ติดตาม <ชื่อหุ้น>')"
        return "รายการติดตาม:\n" + ", ".join(watchlist)
        
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

    return "คำสั่งไม่ถูกต้อง พิมพ์ 'แผนวันนี้', 'ดูพอร์ต', หรือ 'รายการติดตาม'"


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
