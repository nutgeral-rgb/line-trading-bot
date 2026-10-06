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

def analyze_stock_deep(symbol):
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
        
        suggested_buy_price = round(lower, 2) if cur_price > lower else round(cur_price, 2)
        suggested_stop_loss = round(cur_price * 0.95, 2)
        suggested_take_profit = round(cur_price * 1.10, 2)

        reasons = []
        action = "HOLD"
        score = 50
        
        if rsi < 38 or cur_price <= lower * 1.02:
            action = "BUY"
            score += 35
            reasons.append(f"RSI อยู่ที่ {rsi:.1f} (เข้าเขต Oversold) และราคาลงมาทดสอบแนวรับกรอบล่าง Bollinger Bands โอกาสฟื้นตัวสูง")
            if vol_spike:
                score += 15
                reasons.append("พบปริมาณซื้อขายหนาแน่นผิดปกติ (Volume Spike) มีแรงซื้อสะสมจากผู้เล่นรายใหญ่")
        elif hist > 0 and cur_price > ema50:
            action = "ADD/HOLD"
            score += 25
            reasons.append(f"โครงสร้างราคายืนเหนือเส้น EMA 50 และ MACD Histogram เป็นบวก ({hist:.2f}) โมเมนตัมขาขึ้นแข็งแกร่ง")
            if vol_spike:
                score += 10
                reasons.append("มี Volume สนับสนุนการปรับตัวขึ้นต่อเนื่อง")
        elif rsi > 68 or cur_price >= upper * 0.98:
            action = "SELL"
            score -= 20
            reasons.append(f"RSI สูงถึง {rsi:.1f} เข้าเขต Overbought และราคาชนแนวต้านกรอบบน เสี่ยงโดนเทขายทำกำไร")
        else:
            action = "HOLD"
            reasons.append(f"ราคาเคลื่อนไหวออกข้าง (Sideway) รอดูความชัดเจนของทิศทางตลาด")

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
            "reasons": reasons,
            "buy_price": suggested_buy_price,
            "stop_loss": suggested_stop_loss,
            "take_profit": suggested_take_profit
        }
    except:
        return None

def generate_top5_summary_text(portfolio):
    watchlist = portfolio.get("watchlist", ["ADVANC.BK", "PTT.BK", "AOT.BK", "KBANK.BK", "CPALL.BK"])
    scanned_stocks = []
    for sym in watchlist:
        res = analyze_stock_deep(sym)
        if res:
            scanned_stocks.append((clean_symbol_display(sym), res))
    
    if not scanned_stocks:
        return "❌ ไม่พบข้อมูลหุ้นใน Watchlist สำหรับจัดอันดับ"
        
    scanned_stocks.sort(key=lambda x: x[1]['score'], reverse=True)
    top5 = scanned_stocks[:5]
    
    msg = ["🌅 สรุป 5 หุ้นน่าซื้อประจำวัน\n⏱️ (อ้างอิงข้อมูลราคาปิดย้อนหลัง 60 วันทำการ):\n"]
    for rank, (sym, res) in enumerate(top5, 1):
        vol_text = " 🔥[Volume พุ่งหนาแน่น]" if res['vol_spike'] else ""
        cost_10 = res['buy_price'] * 10
        msg.append(
            f"🏆 อันดับ {rank}: {sym} (คะแนน: {res['score']}/100)\n"
            f"  - ราคาปัจจุบัน: {res['price']:.2f} ฿{vol_text}\n"
            f"  - สถานะ: [{res['action']}]\n"
            f"  - 🟢 โซนราคาเข้าซื้อ: {res['buy_price']:.2f} ฿\n"
            f"  - 🔴 จุดตัดขาดทุน (Stop Loss): {res['stop_loss']:.2f} ฿\n"
            f"  - 🎯 เป้าหมายทำกำไร (Take Profit): {res['take_profit']:.2f} ฿\n"
            f"  - 🛒 **แนะนำสั่งซื้อ: 10 หุ้น** (ใช้วงเงินประมาณ {cost_10:,.2f} ฿)\n"
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
    return "Financial Advisor Trading Bot (No .BK required) is running!"

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
    reply_text = ""
    parts = text.split()

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
                
                stock_summary += (f"- {display_sym}: {shares} หุ้น\n  ทุนสุทธิ: {total_cost:,.2f} ฿ (เฉลี่ย {avg_cost:.2f})\n"
                                  f"  ปัจจุบัน: {cur_price:.2f} ฿ | กำไร/ขาดทุน: {pl:+,.2f} ฿ ({pl_pct:+.2f}%)\n"
                                  f"  RSI: {rsi:.1f}\n\n")
        
        total_portfolio = cash + total_stock_value
        progress = (total_portfolio / target) * 100
        diff = target - total_portfolio
        paused = total_portfolio <= plan.get("trading_paused_at_value", 8000)
        status_text = "⚠️ พอร์ตถึงขีดจำกัดความเสี่ยง (หยุดซื้อเพิ่ม)" if paused else "🟢 พอร์ตอยู่ในเกณฑ์ปกติ"

        reply_text = (
            f"📊 สถานะพอร์ต & แผนการเงิน:\n"
            f"- เงินสด: {cash:,.2f} ฿\n"
            f"- มูลค่าหุ้นรวม: {total_stock_value:,.2f} ฿\n"
            f"- พอร์ตวม: {total_portfolio:,.2f} ฿\n"
            f"- เป้าหมาย (3 เดือน): {target:,.2f} ฿ (คืบหน้า {progress:.1f}%)\n"
            f"- ขาดอีก: {diff:,.2f} ฿ ถึงเป้า\n"
            f"- สถานะความเสี่ยง: {status_text}\n\n"
            f"📈 หุ้นในพอร์ต:\n{stock_summary}"
        )

    elif text == "วิเคราะห์พอร์ต":
        cash = portfolio.get("cash", 10000.0)
        stocks = portfolio.get("stocks", {})
        plan = portfolio.get("financial_plan", {})
        target = plan.get("target_value", 20000.0)
        
        total_stock_value = 0
        analysis_reports = []
        
        for symbol, info in stocks.items():
            display_sym = clean_symbol_display(symbol)
            shares = info['shares']
            total_cost = info.get('total_cost', 0)
            avg_cost = info.get('average_cost', 0)
            sl = info.get('stop_loss', avg_cost * 0.95)
            
            res = analyze_stock_deep(symbol)
            if not res:
                analysis_reports.append(f"• {display_sym}: ดึงข้อมูลไม่ได้")
                continue
            
            total_stock_value += shares * res['price']
            pl_pct = ((res['price'] - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0
            reason_text = "\n    - ".join(res['reasons'])
            
            if res['price'] <= sl or res['action'] == "SELL":
                analysis_reports.append(
                    f"🚨 [{display_sym}] แนะนำ: 🔴 ขาย / ลดพอร์ต\n"
                    f"  - ราคาปัจจุบัน: {res['price']:.2f} ฿ (กำไร/ขาดทุน: {pl_pct:+.2f}%)\n"
                    f"  - จุดตัดขาดทุน: {sl:.2f} ฿\n"
                    f"  - เหตุผล:\n    - {reason_text}"
                )
            else:
                analysis_reports.append(
                    f"✅ [{display_sym}] แนะนำ: 🟢 ถือต่อ / ทยอยสะสม\n"
                    f"  - ราคาปัจจุบัน: {res['price']:.2f} ฿ (กำไร/ขาดทุน: {pl_pct:+.2f}%)\n"
                    f"  - โซนซื้อเพิ่ม: {res['buy_price']:.2f} ฿ | เป้าทำกำไร: {res['take_profit']:.2f} ฿\n"
                    f"  - เหตุผล:\n    - {reason_text}"
                )

        total_portfolio = cash + total_stock_value
        progress = (total_portfolio / target) * 100

        reply_text = (
            f"🧠 วิเคราะห์พอร์ตตามแผนการเงิน:\n"
            f"- ความคืบหน้าสู่เป้าหมาย: {progress:.1f}% ({total_portfolio:,.2f} / {target:,.2f} ฿)\n\n"
            f"📋 หุ้นในพอร์ตของคุณ:\n\n" + ("\n\n".join(analysis_reports) if analysis_reports else "• ไม่มีหุ้นในพอร์ต")
        )

    elif text == "สแกนหุ้น":
        watchlist = portfolio.get("watchlist", [])
        scanned_stocks = []
        for sym in watchlist:
            res = analyze_stock_deep(sym)
            if res:
                scanned_stocks.append((clean_symbol_display(sym), res))
        
        scanned_stocks.sort(key=lambda x: x[1]['score'], reverse=True)
        
        scan_results = []
        for rank, (sym, res) in enumerate(scanned_stocks, 1):
            vol_text = " 🔥 [Volume พุ่งหนาแน่น]" if res['vol_spike'] else ""
            reasons_formatted = "\n    - ".join(res['reasons'])
            cost_10_shares = res['buy_price'] * 10
            
            scan_results.append(
                f"🏆 อันดับที่ {rank}: {sym} (คะแนนความน่าสนใจ: {res['score']}/100)\n"
                f"  - ราคาปัจจุบัน: {res['price']:.2f} ฿{vol_text}\n"
                f"  - สถานะทางเทคนิค: [{res['action']}]\n"
                f"  - 💡 เหตุผลควรเข้าซื้อ:\n    - {reasons_formatted}\n"
                f"  - 📊 ระดับราคาตามกลยุทธ์:\n"
                f"    * โซนราคาเข้าซื้อ: {res['buy_price']:.2f} ฿\n"
                f"    * จุดตัดขาดทุน (Stop Loss): {res['stop_loss']:.2f} ฿\n"
                f"    * เป้าหมายทำกำไร (Take Profit): {res['take_profit']:.2f} ฿\n"
                f"  - 🛒 **คำแนะนำการลงทุน: สั่งซื้อ 10 หุ้น**\n"
                f"    (ใช้วงเงินประมาณ {cost_10_shares:,.2f} ฿)"
            )
        
        reply_text = (
            "📊 ผลสแกน Watchlist (จัดอันดับความน่าสนใจ)\n"
            "⏱️ (อ้างอิงข้อมูลราคาปิดย้อนหลัง 60 วันทำการ):\n\n"
            + ("\n\n".join(scan_results) if scan_results else "ยังไม่มีหุ้นใน Watchlist")
        )

    elif text == "สรุปหุ้นที่น่าซื้อวันนี้":
        reply_text = generate_top5_summary_text(portfolio)

    elif text.startswith("สแกน "):
        if len(parts) >= 2:
            symbol = format_symbol(parts[1])
            display_sym = clean_symbol_display(symbol)
            res = analyze_stock_deep(symbol)
            if res:
                vol_text = "มี (วอลุ่มหนาแน่นผิดปกติ ⚡)" if res['vol_spike'] else "ปกติ"
                reasons_formatted = "\n  - ".join(res['reasons'])
                cost_10_shares = res['buy_price'] * 10
                reply_text = (
                    f"🔎 แผนวิเคราะห์เจาะลึก [{display_sym}]\n"
                    f"⏱️ (อ้างอิงข้อมูลราคาปิดย้อนหลัง 60 วันทำการ):\n\n"
                    f"- คะแนนความน่าสนใจ: {res['score']}/100\n"
                    f"- ราคาปัจจุบัน: {res['price']:.2f} ฿\n"
                    f"- สถานะแนะนำ: [{res['action']}]\n\n"
                    f"💡 เหตุผลการเข้าซื้อ:\n  - {reasons_formatted}\n\n"
                    f"📊 ระดับราคาตามกลยุทธ์:\n"
                    f"  - 🟢 โซนราคาเข้าซื้อ: {res['buy_price']:.2f} ฿\n"
                    f"  - 🔴 จุดตัดขาดทุน (Stop Loss): {res['stop_loss']:.2f} ฿\n"
                    f"  - 🎯 เป้าหมายทำกำไร: {res['take_profit']:.2f} ฿\n\n"
                    f"🛒 **คำแนะนำการลงทุน: สั่งซื้อ 10 หุ้น**\n"
                    f"  (ใช้วงเงินประมาณ {cost_10_shares:,.2f} ฿)\n\n"
                    f"📌 ข้อมูลตัวชี้วัด:\n"
                    f"  - RSI: {res['rsi']:.1f} | MACD Hist: {res['macd_hist']:.2f}\n"
                    f"  - Volume Surge: {vol_text}"
                )
            else:
                reply_text = f"❌ ไม่สามารถดึงข้อมูลหุ้น {display_sym} ได้ หรือชื่อหุ้นไม่ถูกต้อง"
        else:
            reply_text = "❌ รูปแบบไม่ถูกต้อง (ตัวอย่าง: สแกน PTT)"

    elif text in ["ช่วยเหลือ", "help", "คำสั่ง"]:
        reply_text = (
            "📋 คู่มือคำสั่งบอตวางแผนการลงทุน:\n"
            "1. ดูพอร์ต -> ตรวจสอบเงินสด มูลค่าพอร์ต และเป้าหมาย\n"
            "2. วิเคราะห์พอร์ต -> สรุปแผนจัดการหุ้นในพอร์ต\n"
            "3. สแกนหุ้น -> จัดอันดับ Watchlist พร้อมเหตุผลและสั่ง 10 หุ้น\n"
            "4. สรุปหุ้นที่น่าซื้อวันนี้ -> สรุป 5 อันดับแรกประจำวันตามเวลาจริง\n"
            "5. สแกน [หุ้น] -> เจาะลึกรายตัว (เช่น สแกน PTT)\n"
            "6. ซื้อ [หุ้น] [จำนวน] [ราคา] [ค่าธรรมเนียม] -> บันทึกซื้อจริง\n"
            "7. ขาย [หุ้น] [จำนวน] [ราคา] [ค่าธรรมเนียม] -> บันทึกขายจริง\n"
            "8. ติดตาม [หุ้น] / เลิกติดตาม [หุ้น] / รายการติดตาม"
        )

    elif text == "รายการติดตาม":
        watchlist = portfolio.get("watchlist", [])
        clean_watchlist = [clean_symbol_display(s) for s in watchlist]
        reply_text = "📋 รายชื่อหุ้นในระบบติดตาม:\n" + ", ".join(clean_watchlist) if clean_watchlist else "ยังไม่มีหุ้นในรายการติดตาม"

    elif text.startswith("ติดตาม "):
        if len(parts) >= 2:
            symbol = format_symbol(parts[1])
            display_sym = clean_symbol_display(symbol)
            watchlist = portfolio.get("watchlist", [])
            if symbol not in watchlist:
                watchlist.append(symbol)
                portfolio['watchlist'] = watchlist
                update_user_portfolio(user_id, portfolio)
                reply_text = f"✅ เพิ่ม {display_sym} เรียบร้อยแล้ว"
            else:
                reply_text = f"ℹ️ {display_sym} มีในรายการอยู่แล้ว"
        else:
            reply_text = "❌ รูปแบบไม่ถูกต้อง (ตัวอย่าง: ติดตาม PTT)"

    elif text.startswith("เลิกติดตาม "):
        if len(parts) >= 2:
            symbol = format_symbol(parts[1])
            display_sym = clean_symbol_display(symbol)
            watchlist = portfolio.get("watchlist", [])
            if symbol in watchlist:
                watchlist.remove(symbol)
                portfolio['watchlist'] = watchlist
                update_user_portfolio(user_id, portfolio)
                reply_text = f"🗑️ ลบ {display_sym} ออกแล้ว"
            else:
                reply_text = f"❌ ไม่พบ {display_sym} ในรายการ"
        else:
            reply_text = "❌ รูปแบบไม่ถูกต้อง (ตัวอย่าง: เลิกติดตาม PTT)"

    elif text.startswith("ซื้อ"):
        if len(parts) >= 4:
            symbol = format_symbol(parts[1])
            display_sym = clean_symbol_display(symbol)
            try:
                shares = int(parts[2])
                price = float(parts[3])
                fee = float(parts[4]) if len(parts) >= 5 else 0.0
                
                total_cost = (price * shares) + fee
                cash = portfolio.get("cash", 10000.0)
                
                if cash >= total_cost:
                    portfolio['cash'] = cash - total_cost
                    if symbol not in portfolio['stocks']:
                        portfolio['stocks'][symbol] = {"shares": 0, "total_cost": 0.0}
                    
                    old_info = portfolio['stocks'][symbol]
                    old_shares = old_info['shares']
                    old_total_cost = old_info.get('total_cost', old_shares * old_info.get('average_cost', price))
                    
                    new_shares = old_shares + shares
                    new_total_cost = old_total_cost + total_cost
                    new_avg_price = new_total_cost / new_shares if new_shares > 0 else price
                    
                    sl = price * 0.95
                    tp1 = price * 1.10
                    
                    portfolio['stocks'][symbol] = {
                        "shares": new_shares,
                        "total_cost": new_total_cost,
                        "average_cost": new_avg_price,
                        "stop_loss": sl,
                        "take_profit_1": tp1
                    }
                    
                    if "transactions" not in portfolio:
                        portfolio['transactions'] = []
                    portfolio['transactions'].append({
                        "type": "BUY",
                        "symbol": symbol,
                        "shares": shares,
                        "price": price,
                        "fee": fee,
                        "net_amount": total_cost,
                        "date": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")
                    })
                    
                    update_user_portfolio(user_id, portfolio)
                    
                    reply_text = (f"✅ บันทึกซื้อสำเร็จ:\n- หุ้น: {display_sym}\n- จำนวน: {shares} หุ้น\n"
                                  f"- ราคาซื้อ: {price:.2f} ฿ | ค่าธรรมเนียม: {fee:.2f} ฿\n"
                                  f"- จ่ายสุทธิ: {total_cost:,.2f} ฿\n"
                                  f"- ทุนเฉลี่ยใหม่: {new_avg_price:.2f} ฿/หุ้น\n"
                                  f"- ตั้ง Stop Loss อัตโนมัติ: {sl:.2f} ฿")
                else:
                    reply_text = "⚠️ เงินสดในพอร์ตไม่พอจ่ายรวมค่าธรรมเนียม"
            except Exception as e:
                reply_text = f"❌ เกิดข้อผิดพลาด: {str(e)}"
        else:
            reply_text = "❌ รูปแบบคำสั่งไม่ถูกต้อง (ตัวอย่าง: ซื้อ PTT 10 35.0 20)"

    elif text.startswith("ขาย"):
        if len(parts) >= 4:
            symbol = format_symbol(parts[1])
            display_sym = clean_symbol_display(symbol)
            try:
                shares_to_sell = int(parts[2])
                price = float(parts[3])
                fee = float(parts[4]) if len(parts) >= 5 else 0.0
                
                stocks = portfolio.get("stocks", {})
                if symbol in stocks and stocks[symbol]['shares'] >= shares_to_sell:
                    info = stocks[symbol]
                    old_shares = info['shares']
                    old_total_cost = info.get('total_cost', old_shares * info.get('average_cost', price))
                    
                    cost_of_sold = (old_total_cost / old_shares) * shares_to_sell
                    total_income = (price * shares_to_sell) - fee
                    profit = total_income - cost_of_sold
                    profit_pct = (profit / cost_of_sold * 100) if cost_of_sold > 0 else 0
                    
                    portfolio['cash'] += total_income
                    portfolio['realized_pnl_total'] = portfolio.get('realized_pnl_total', 0.0) + profit
                    
                    if old_shares == shares_to_sell:
                        del stocks[symbol]
                    else:
                        remaining_shares = old_shares - shares_to_sell
                        remaining_cost = old_total_cost - cost_of_sold
                        stocks[symbol]['shares'] = remaining_shares
                        stocks[symbol]['total_cost'] = remaining_cost
                        stocks[symbol]['average_cost'] = remaining_cost / remaining_shares
                    
                    if "transactions" not in portfolio:
                        portfolio['transactions'] = []
                    portfolio['transactions'].append({
                        "type": "SELL",
                        "symbol": symbol,
                        "shares": shares_to_sell,
                        "price": price,
                        "fee": fee,
                        "net_amount": total_income,
                        "realized_pnl": profit,
                        "date": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")
                    })
                    
                    update_user_portfolio(user_id, portfolio)
                    
                    reply_text = (f"✅ บันทึกขายสำเร็จ:\n- หุ้น: {display_sym}\n- จำนวน: {shares_to_sell} หุ้น\n"
                                  f"- ราคาขาย: {price:.2f} ฿ | ค่าธรรมเนียม: {fee:.2f} ฿\n"
                                  f"- รับเงินสุทธิ: {total_income:,.2f} ฿\n"
                                  f"- กำไร/ขาดทุนสุทธิ: {profit:+,.2f} ฿ ({profit_pct:+.2f}%)")
                else:
                    reply_text = f"❌ หุ้น {display_sym} ในพอร์ตมีไม่พอขาย"
            except Exception as e:
                reply_text = f"❌ เกิดข้อผิดพลาด: {str(e)}"
        else:
            reply_text = "❌ รูปแบบคำสั่งไม่ถูกต้อง (ตัวอย่าง: ขาย PTT 10 38.0 20)"
    else:
        reply_text = f"🤖 รับทราบคำสั่ง: {text}\n(พิมพ์ 'ช่วยเหลือ' เพื่อดูคำสั่งทั้งหมด)"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
