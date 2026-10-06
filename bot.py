import os
import json
import pandas as pd
import yfinance as yf
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', 'YOUR_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

PORTFOLIO_FILE = 'portfolio.json'

SET50_WATCHLIST = [
    "PTT.BK", "PTTEP.BK", "AOT.BK", "ADVANC.BK", "KBANK.BK", 
    "SCB.BK", "BBL.BK", "KTB.BK", "CPALL.BK", "BDMS.BK", 
    "GULF.BK", "SCC.BK", "TRUE.BK", "CRC.BK", "BH.BK", 
    "AAV.BK", "BEM.BK", "TOP.BK", "IVL.BK", "CPN.BK"
]

def load_data():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_data(data):
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def format_symbol(symbol):
    symbol = symbol.upper().strip()
    if not symbol.endswith(".BK"):
        symbol += ".BK"
    return symbol

def clean_symbol_display(symbol):
    return symbol.replace(".BK", "")

def check_set_market_trend():
    try:
        df_set = yf.Ticker("^SET").history(period="60d")
        if df_set.empty:
            return True, "🟢 ตลาดรวม: ปกติ"
        set_price = df_set['Close'].iloc[-1]
        set_ema50 = df_set['Close'].ewm(span=50, adjust=False).mean().iloc[-1]
        if set_price < set_ema50:
            return False, "⚠️ คำเตือนตลาดรวม: SET Index อยู่ต่ำกว่าเส้น EMA 50 (ระมัดระวังการซื้อ)"
        else:
            return True, "🟢 ตลาดรวม: SET Index โครงสร้างขาขึ้นแข็งแกร่ง"
    except:
        return True, "🟢 ตลาดรวม: โครงสร้างปกติ"

def get_stock_sector(symbol):
    sector_map = {
        "PTT.BK": "Energy (พลังงาน)", "PTTEP.BK": "Energy (พลังงาน)", "TOP.BK": "Energy (พลังงาน)", "BCP.BK": "Energy (พลังงาน)",
        "ADVANC.BK": "Communication (สื่อสาร)", "TRUE.BK": "Communication (สื่อสาร)",
        "AOT.BK": "Transportation (ขนส่ง)", "AAV.BK": "Transportation (ขนส่ง)", "BEM.BK": "Transportation (ขนส่ง)",
        "KBANK.BK": "Banking (ธนาคาร)", "SCB.BK": "Banking (ธนาคาร)", "BBL.BK": "Banking (ธนาคาร)", "KTB.BK": "Banking (ธนาคาร)",
        "CPALL.BK": "Commerce (ค้าปลีก)", "CPAXT.BK": "Commerce (ค้าปลีก)", "CRC.BK": "Commerce (ค้าปลีก)", "HMPRO.BK": "Commerce (ค้าปลีก)",
        "BDMS.BK": "Healthcare (การแพทย์)", "BCH.BK": "Healthcare (การแพทย์)", "BH.BK": "Healthcare (การแพทย์)",
        "GULF.BK": "Energy & Utilities (พลังงานและสาธารณูปโภค)", "GPSC.BK": "Energy & Utilities (พลังงานและสาธารณูปโภค)",
        "SCC.BK": "Construction Materials (วัสดุก่อสร้าง)", "IVL.BK": "Petrochemicals (ปิโตรเคมี)", "CPN.BK": "Property Development (พัฒนาอสังหาริมทรัพย์)"
    }
    if symbol in sector_map:
        return sector_map[symbol]
    
    try:
        info = yf.Ticker(symbol).info
        sector = info.get('sector', None)
        if not sector:
            return "General / Others (ทั่วไป)"
        sector_translations = {
            "Energy": "Energy (พลังงาน)",
            "Financial Services": "Financial Services (การเงิน/ธนาคาร)",
            "Communication Services": "Communication (สื่อสาร)",
            "Industrials": "Industrials (อุตสาหกรรม/ขนส่ง)",
            "Consumer Defensive": "Consumer Defensive (สินค้าอุปโภคบริโภค)",
            "Consumer Cyclical": "Consumer Cyclical (สินค้าฟุ่มเฟือย)",
            "Healthcare": "Healthcare (การแพทย์)",
            "Utilities": "Utilities (สาธารณูปโภค)"
        }
        return sector_translations.get(sector, f"{sector} (สากล)")
    except:
        return "General / Others (ทั่วไป)"

def get_user_portfolio(user_id):
    data = load_data()
    if user_id not in data:
        data[user_id] = {
            "cash": 10000.0,
            "initial_capital": 10000.0,
            "realized_pnl_total": 0.0,
            "fees_total": 0.0,
            "financial_plan": {
                "target_value": 20000.0,
                "target_months": 3,
                "monthly_contribution": 1000.0,
                "risk_level": "high",
                "max_portfolio_drawdown_pct": 0.20,
                "trading_paused_at_value": 8000.0,
                "risk_per_trade_pct": 0.02,
                "max_stock_weight_pct": 0.25,
                "default_stop_loss_pct": 0.05,
                "risk_reward_ratio": 2.0
            },
            "stocks": {},
            "watchlist": ["ADVANC.BK", "PTT.BK", "AOT.BK", "KBANK.BK", "CPALL.BK"],
            "transactions": []
        }
        save_data(data)
    return data[user_id]

def update_user_portfolio(user_id, portfolio):
    data = load_data()
    data[user_id] = portfolio
    save_data(data)

def calculate_rsi(df, window=14):
    if len(df) < window:
        return 50.0
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

def calculate_macd(df, span1=12, span2=26, signal=9):
    if len(df) < span2:
        return 0.0, 0.0, 0.0
    exp1 = df['Close'].ewm(span=span1, adjust=False).mean()
    exp2 = df['Close'].ewm(span=span2, adjust=False).mean()
    macd = exp1 - exp2
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    histogram = macd - signal_line
    return macd.iloc[-1], signal_line.iloc[-1], histogram.iloc[-1]

def calculate_ema(df, window=50):
    if len(df) < window:
        return df['Close'].iloc[-1]
    return df['Close'].ewm(span=window, adjust=False).mean().iloc[-1]

def calculate_bollinger_bands(df, window=20, num_std=2):
    if len(df) < window:
        cur = df['Close'].iloc[-1]
        return cur, cur, cur
    sma = df['Close'].rolling(window=window).mean()
    std = df['Close'].rolling(window=window).std()
    upper = sma + (std * num_std)
    lower = sma - (std * num_std)
    return upper.iloc[-1], sma.iloc[-1], lower.iloc[-1]

def check_volume_spike(df, window=20, mult=1.5):
    if len(df) < window + 1:
        return False
    avg_vol = df['Volume'].iloc[-(window+1):-1].mean()
    cur_vol = df['Volume'].iloc[-1]
    return cur_vol >= (avg_vol * mult)

def calculate_dynamic_position_size(portfolio, price, stop_loss):
    cash = portfolio.get("cash", 10000.0)
    stocks = portfolio.get("stocks", {})
    total_stock_val = sum(info['shares'] * price for sym, info in stocks.items())
    total_portfolio = max(cash + total_stock_val, 10000.0)
    
    risk_budget = total_portfolio * 0.02
    risk_per_share = max(price - stop_loss, price * 0.03)
    shares_by_risk = risk_budget / risk_per_share if risk_per_share > 0 else 10
    
    max_weight_budget = total_portfolio * 0.25
    shares_by_weight = max_weight_budget / price if price > 0 else 10
    
    cash_budget = cash * 0.40
    shares_by_cash = cash_budget / price if price > 0 else 10
    
    recommended_shares = int(min(shares_by_risk, shares_by_weight, shares_by_cash))
    if recommended_shares < 10:
        recommended_shares = max(recommended_shares, 1)
    else:
        recommended_shares = (recommended_shares // 10) * 10
        if recommended_shares < 10:
            recommended_shares = 10
    return recommended_shares

def analyze_stock_deep(portfolio, symbol):
    formatted_sym = format_symbol(symbol)
    try:
        df = yf.Ticker(formatted_sym).history(period='60d')
        if df.empty or len(df) < 20:
            return None
        
        cur_price = df['Close'].iloc[-1]
        rsi = calculate_rsi(df)
        macd, sig, hist = calculate_macd(df)
        ema50 = calculate_ema(df, 50)
        upper, sma, lower = calculate_bollinger_bands(df)
        vol_spike = check_volume_spike(df)
        
        avg_vol_20 = df['Volume'].iloc[-20:].mean()
        cur_vol = df['Volume'].iloc[-1]

        suggested_buy_price = round(lower, 2) if cur_price > lower else round(cur_price, 2)
        suggested_stop_loss = round(suggested_buy_price * 0.95, 2)
        suggested_take_profit = round(suggested_buy_price * 1.10, 2)

        rec_shares = calculate_dynamic_position_size(portfolio, suggested_buy_price, suggested_stop_loss)
        sector = get_stock_sector(formatted_sym)

        reasons = []
        action = "HOLD"
        score = 50
        
        is_forbidden = False
        if cur_price < ema50 and hist < 0:
            is_forbidden = True
            reasons.append("❌ ผิดกฎเหล็กข้อ 1: ราคาต่ำกว่า EMA 50 และ MACD ติดลบ")
        if rsi > 75:
            is_forbidden = True
            reasons.append(f"❌ ผิดกฎเหล็กข้อ 2: RSI สูงถึง {rsi:.1f} (Overbought)")
        if cur_vol < avg_vol_20 * 0.5:
            is_forbidden = True
            reasons.append("❌ ผิดกฎเหล็กข้อ 3: ปริมาณซื้อขายแห้งเกินไป")

        if is_forbidden:
            action = "❌ ห้ามซื้อเด็ดขาด"
            score = 0
        else:
            if rsi < 38 or cur_price <= lower * 1.02:
                action = "BUY"
                score += 35
                reasons.append(f"RSI อยู่ที่ {rsi:.1f} (Oversold) เทสแนวรับ Bollinger Bands")
                if vol_spike:
                    score += 15
                    reasons.append("พบ Volume พุ่งหนาแน่นผิดปกติ")
            elif hist > 0 and cur_price > ema50:
                action = "ADD/HOLD"
                score += 25
                reasons.append(f"โครงสร้างยืนเหนือ EMA 50 และ MACD เป็นบวก")
                if vol_spike:
                    score += 10
                    reasons.append("มี Volume สนับสนุนต่อเนื่อง")
            elif rsi > 68 or cur_price >= upper * 0.98:
                action = "SELL"
                score -= 20
                reasons.append(f"RSI สูง (Overbought) ชนแนวต้าน")
            else:
                action = "HOLD"
                reasons.append("ราคาเคลื่อนไหว Sideway")

        return {
            "price": cur_price,
            "rsi": rsi,
            "macd_hist": hist,
            "ema50": ema50,
            "bb_upper": upper,
            "bb_lower": lower,
            "vol_spike": vol_spike,
            "action": action,
            "score": min(max(score, 0), 100),
            "sector": sector,
            "reasons": reasons,
            "buy_price": suggested_buy_price,
            "stop_loss": suggested_stop_loss,
            "take_profit": suggested_take_profit,
            "rec_shares": rec_shares
        }
    except:
        return None

def generate_set50_scan_text(portfolio):
    scanned_stocks = []
    for sym in SET50_WATCHLIST:
        res = analyze_stock_deep(portfolio, sym)
        # กรองเฉพาะหุ้นที่ไม่ติดกฎเหล็ก และไม่ใช่สถานะ SELL หรือคะแนนต่ำเกินไป
        if res and res['action'] not in ["❌ ห้ามซื้อเด็ดขาด", "SELL"] and res['score'] >= 50:
            scanned_stocks.append((clean_symbol_display(sym), res))
            
    if not scanned_stocks:
        return "⚠️ ไม่มีหุ้นในกลุ่ม SET50 ที่ผ่านเกณฑ์ความปลอดภัยวันนี้"
        
    scanned_stocks.sort(key=lambda x: x[1]['score'], reverse=True)
    top10 = scanned_stocks[:10]
    
    set_ok, set_msg = check_set_market_trend()
    msg = [f"📊 ผลสแกนหุ้นเด่น SET50 (Top 10 ผ่านเกณฑ์)\n{set_msg}\n"]
    for rank, (sym, res) in enumerate(top10, 1):
        vol_text = " 🔥[Volume พุ่ง]" if res['vol_spike'] else ""
        est_cost = res['buy_price'] * res['rec_shares']
        msg.append(
            f"🏆 อันดับ {rank}: {sym} [{res['sector']}] (คะแนน: {res['score']}/100)\n"
            f"  - ราคา: {res['price']:.2f} ฿{vol_text} | สถานะ: [{res['action']}]\n"
            f"  - โซนซื้อ: {res['buy_price']:.2f} ฿ | เป้าขาย: {res['take_profit']:.2f} ฿\n"
            f"  - แนะนำซื้อ: {res['rec_shares']} หุ้น (~{est_cost:,.2f} ฿)\n"
        )
    return "\n".join(msg)

def generate_top5_summary_text(portfolio):
    watchlist = portfolio.get("watchlist", ["ADVANC.BK", "PTT.BK", "AOT.BK", "KBANK.BK", "CPALL.BK"])
    scanned_stocks = []
    for sym in watchlist:
        res = analyze_stock_deep(portfolio, sym)
        if res and res['action'] not in ["❌ ห้ามซื้อเด็ดขาด", "SELL"] and res['score'] >= 50:
            scanned_stocks.append((clean_symbol_display(sym), res))
    
    set_ok, set_msg = check_set_market_trend()
    if not scanned_stocks:
        return f"🌅 สรุปหุ้นวันนี้\n{set_msg}\n\n⚠️ วันนี้ไม่มีหุ้นผ่านเกณฑ์"
        
    scanned_stocks.sort(key=lambda x: x[1]['score'], reverse=True)
    top5 = scanned_stocks[:5]
    
    msg = [f"🌅 สรุปหุ้นวันนี้\n{set_msg}\n"]
    for rank, (sym, res) in enumerate(top5, 1):
        vol_text = " 🔥[Volume พุ่ง]" if res['vol_spike'] else ""
        est_cost = res['buy_price'] * res['rec_shares']
        msg.append(
            f"🏆 อันดับ {rank}: {sym} [{res['sector']}] (คะแนน: {res['score']}/100)\n"
            f"  - ราคา: {res['price']:.2f} ฿{vol_text} | สถานะ: [{res['action']}]\n"
            f"  - โซนซื้อ: {res['buy_price']:.2f} ฿ | เป้าขาย: {res['take_profit']:.2f} ฿\n"
        )
    return "\n".join(msg)

def daily_morning_push_task():
    with app.app_context():
        data = load_data()
        for user_id, portfolio in data.items():
            if not user_id or not isinstance(portfolio, dict):
                continue
            summary_text = generate_top5_summary_text(portfolio)
            try:
                line_bot_api.push_message(user_id, TextSendMessage(text=summary_text))
            except Exception as e:
                print(f"Push error to {user_id}: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(func=daily_morning_push_task, trigger=CronTrigger(hour=8, minute=30))
scheduler.start()

@app.route("/", methods=['GET'])
def home():
    return "Financial Advisor Trading Bot (Fixed Sectors & Filter) is running!"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    portfolio = get_user_portfolio(user_id)
    portfolio['last_user_id'] = user_id
    reply_text = ""
    parts = text.split()

    try:
        if text == "ดูพอร์ต":
            cash = portfolio.get("cash", 10000.0)
            stocks = portfolio.get("stocks", {})
            plan = portfolio.get("financial_plan", {})
            target = plan.get("target_value", 20000.0)
            
            total_stock_value = 0
            stock_summary = ""
            if not stocks:
                stock_summary = "ไม่มีหุ้นในพอร์ต\n"
            else:
                for symbol, info in stocks.items():
                    display_sym = clean_symbol_display(symbol)
                    shares = info['shares']
                    total_cost = info.get('total_cost', 0)
                    avg_cost = info.get('average_cost', 0)
                    try:
                        df = yf.Ticker(symbol).history(period='30d')
                        cur_price = df['Close'].iloc[-1]
                        rsi = calculate_rsi(df)
                    except:
                        cur_price = avg_cost
                        rsi = 50.0
                    market_value = shares * cur_price
                    total_stock_value += market_value
                    pl = market_value - total_cost
                    pl_pct = (pl / total_cost * 100) if total_cost > 0 else 0
                    stock_summary += f"- {display_sym}: {shares} หุ้น | ทุน: {total_cost:,.2f} ฿ | กำไร: {pl:+,.2f} ฿ ({pl_pct:+.2f}%)\n"
            
            total_portfolio = cash + total_stock_value
            progress = (total_portfolio / target) * 100
            reply_text = f"📊 พอร์ตของคุณ:\n- เงินสด: {cash:,.2f} ฿\n- มูลค่าหุ้น: {total_stock_value:,.2f} ฿\n- รวม: {total_portfolio:,.2f} ฿ (เป้าหมาย {progress:.1f}%)\n\nหุ้นในพอร์ต:\n{stock_summary}"

        elif text == "สแกน SET50":
            reply_text = generate_set50_scan_text(portfolio)

        elif text == "สแกนหุ้น":
            watchlist = portfolio.get("watchlist", [])
            scanned_stocks = []
            for sym in watchlist:
                res = analyze_stock_deep(portfolio, sym)
                if res:
                    scanned_stocks.append((clean_symbol_display(sym), res))
            scanned_stocks.sort(key=lambda x: x[1]['score'], reverse=True)
            scan_results = [f"- {sym} [{res['sector']}]: {res['action']} (คะแนน {res['score']})" for sym, res in scanned_stocks]
            reply_text = "📊 ผลสแกน Watchlist:\n" + ("\n".join(scan_results) if scan_results else "ไม่มีหุ้นในรายการ")

        elif text == "สรุปหุ้นวันนี้":
            reply_text = generate_top5_summary_text(portfolio)

        elif text in ["ช่วยเหลือ", "help", "คำสั่ง"]:
            reply_text = (
                "📋 คู่มือคำสั่ง:\n"
                "1. ดูพอร์ต\n"
                "2. สแกนหุ้น (จาก Watchlist)\n"
                "3. สแกน SET50 (สแกนหุ้นเด่นในกลุ่ม SET50)\n"
                "4. สรุปหุ้นวันนี้\n"
                "5. สแกน [หุ้น] (เช่น สแกน PTT)\n"
                "6. ซื้อ / ขาย [หุ้น] [จำนวน] [ราคา] [ค่าธรรมเนียม]"
            )
        else:
            reply_text = f"🤖 รับทราบคำสั่ง: {text} (พิมพ์ 'ช่วยเหลือ')"
    except Exception as e:
        reply_text = f"⚠️ ระบบขัดข้อง: {str(e)}"

    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    except Exception as e:
        print(f"Line Reply Error: {e}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
