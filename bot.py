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

# รายชื่อหุ้นกลุ่ม SET50 มาตรฐานสำหรับเป็น Watchlist เริ่มต้นของระบบ
SET50_WATCHLIST = [
    "PTT.BK", "PTTEP.BK", "TOP.BK", "BCP.BK", "ADVANC.BK", 
    "TRUE.BK", "AOT.BK", "AAV.BK", "BEM.BK", "KBANK.BK", 
    "SCB.BK", "BBL.BK", "KTB.BK", "CPALL.BK", "CPAXT.BK", 
    "CRC.BK", "HMPRO.BK", "BDMS.BK", "BCH.BK", "BH.BK",
    "GULF.BK", "GPSC.BK", "SCC.BK", "IVL.BK", "CPN.BK"
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
            "watchlist": SET50_WATCHLIST.copy(), # บูรณาการ SET50 เป็น Watchlist เริ่มต้น
            "transactions": []
        }
        save_data(data)
    else:
        # อัปเดตผู้ใช้เดิมให้มี Watchlist ครบชุดหากยังเป็นรายการเก่า
        if "watchlist" not in data[user_id] or len(data[user_id]["watchlist"]) <= 5:
            data[user_id]["watchlist"] = SET50_WATCHLIST.copy()
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

def analyze_momentum_breakout(symbol):
    formatted_sym = format_symbol(symbol)
    try:
        df = yf.Ticker(formatted_sym).history(period='40d')
        if df.empty or len(df) < 25:
            return None
        cur_price = df['Close'].iloc[-1]
        high_20 = df['High'].iloc[-21:-1].max()
        cur_vol = df['Volume'].iloc[-1]
        avg_vol_20 = df['Volume'].iloc[-21:-1].mean()
        vol_ratio = (cur_vol / avg_vol_20) if avg_vol_20 > 0 else 1.0
        is_breakout = (cur_price >= high_20 * 0.995) and (vol_ratio >= 2.0)
        
        suggested_buy_price = round(cur_price, 2)
        suggested_stop_loss = round(cur_price * 0.97, 2)
        suggested_take_profit = round(cur_price * 1.08, 2)
        
        reasons = []
        if is_breakout:
            reasons.append(f"🔥 Breakout: เบรกแนวต้าน 20 วันที่ {high_20:.2f} ฿")
            reasons.append(f"⚡ Volume พุ่งสูงกว่าค่าเฉลี่ย {vol_ratio:.1f} เท่า")
        else:
            reasons.append(f"⏳ ยังไม่เบรกแนวต้าน (ราคา: {cur_price:.2f} ฿ | แนวต้าน: {high_20:.2f} ฿)")
            
        return {
            "price": cur_price,
            "high_20": high_20,
            "vol_ratio": vol_ratio,
            "is_breakout": is_breakout,
            "reasons": reasons,
            "buy_price": suggested_buy_price,
            "stop_loss": suggested_stop_loss,
            "take_profit": suggested_take_profit
        }
    except:
        return None

def calculate_trade_statistics(portfolio):
    transactions = portfolio.get("transactions", [])
    sells = [t for t in transactions if t.get("type"] == "SELL"]
    if not sells:
        return "📊 ยังไม่มีประวัติการขายทำกำไร/ขาดทุนเพื่อคำนวณสถิติ"
    total_trades = len(sells)
    winning_trades = [t for t in sells if t.get("realized_pnl", 0) > 0]
    losing_trades = [t for t in sells if t.get("realized_pnl", 0) <= 0]
    win_rate = (len(winning_trades) / total_trades) * 100 if total_trades > 0 else 0
    total_profit = sum(t.get("realized_pnl", 0) for t in winning_trades)
    total_loss = abs(sum(t.get("realized_pnl", 0) for t in losing_trades))
    profit_factor = (total_profit / total_loss) if total_loss > 0 else (total_profit if total_profit > 0 else 0)
    return (
        f"📊 สถิติประสิทธิภาพการเทรด:\n"
        f"- จำนวนเทรดขาย: {total_trades} ครั้ง\n"
        f"- ชนะ: {len(winning_trades)} | แพ้: {len(losing_trades)}\n"
        f"- 🎯 **Win Rate:** {win_rate:.1f}%\n"
        f"- กำไรรวม: +{total_profit:,.2f} ฿\n"
        f"- ขาดทุนรวม: -{total_loss:,.2f} ฿\n"
        f"- 📈 **Profit Factor:** {profit_factor:.2f}"
    )

def generate_sector_analysis_text(portfolio):
    watchlist = portfolio.get("watchlist", SET50_WATCHLIST)
    sectors_dict = {}
    for sym in watchlist:
        res = analyze_stock_deep(portfolio, sym)
        if res:
            sec = res['sector']
            if sec not in sectors_dict:
                sectors_dict[sec] = []
            sectors_dict[sec].append((clean_symbol_display(sym), res))
            
    if not sectors_dict:
        return "❌ ไม่พบข้อมูลหุ้นในระบบ"
        
    set_ok, set_msg = check_set_market_trend()
    msg = [f"🏭 รายงานวิเคราะห์รายอุตสาหกรรม (Sector Analysis)\n{set_msg}\n"]
    for sector_name, stocks in sectors_dict.items():
        msg.append(f"📂 **หมวด: {sector_name}**")
        stocks.sort(key=lambda x: x[1]['score'], reverse=True)
        for sym, res in stocks:
            vol_icon = "🔥" if res['vol_spike'] else ""
            msg.append(
                f"  - **{sym}** | ราคา: {res['price']:.2f} ฿ {vol_icon}\n"
                f"    สถานะ: [{res['action']}] (คะแนน: {res['score']}/100)\n"
                f"    โซนซื้อ: {res['buy_price']:.2f} ฿ | เป้าขาย: {res['take_profit']:.2f} ฿"
            )
        msg.append("")
    return "\n".join(msg)

def generate_top5_summary_text(portfolio):
    watchlist = portfolio.get("watchlist", SET50_WATCHLIST)
    scanned_stocks = []
    for sym in watchlist:
        res = analyze_stock_deep(portfolio, sym)
        if res and res['action'] not in ["❌ ห้ามซื้อเด็ดขาด", "SELL"] and res['score'] >= 50:
            scanned_stocks.append((clean_symbol_display(sym), res))
    
    set_ok, set_msg = check_set_market_trend()
    if not scanned_stocks:
        return f"🌅 สรุปหุ้นวันนี้\n{set_msg}\n\n⚠️ วันนี้ไม่มีหุ้นผ่านเกณฑ์ความปลอดภัย"
        
    scanned_stocks.sort(key=lambda x: x[1]['score'], reverse=True)
    top5 = scanned_stocks[:5]
    
    msg = [f"🌅 สรุปหุ้นวันนี้\n{set_msg}\n⏱️ (คัดกรองจาก Watchlist / SET50):\n"]
    for rank, (sym, res) in enumerate(top5, 1):
        vol_text = " 🔥[Volume พุ่ง]" if res['vol_spike'] else ""
        est_cost = res['buy_price'] * res['rec_shares']
        msg.append(
            f"🏆 อันดับ {rank}: {sym} [{res['sector']}] (คะแนน: {res['score']}/100)\n"
            f"  - ราคา: {res['price']:.2f} ฿{vol_text} | สถานะ: [{res['action']}]\n"
            f"  - โซนซื้อ: {res['buy_price']:.2f} ฿ | เป้าขาย: {res['take_profit']:.2f} ฿\n"
            f"  - แนะนำซื้อ: {res['rec_shares']} หุ้น (~{est_cost:,.2f} ฿)\n"
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
    return "Financial Advisor Trading Bot (Integrated SET50 Watchlist) is running!"

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
                res = analyze_stock_deep(portfolio, symbol)
                if not res:
                    continue
                total_stock_value += shares * res['price']
                pl_pct = ((res['price'] - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0
                if res['price'] > avg_cost * 1.05:
                    new_trailing_sl = round(res['price'] * 0.96, 2)
                    if new_trailing_sl > sl:
                        sl = new_trailing_sl
                        info['stop_loss'] = sl
                        update_user_portfolio(user_id, portfolio)
                reason_text = "\n    - ".join(res['reasons'])
                if res['price'] <= sl or res['action'] == "SELL":
                    analysis_reports.append(f"🚨 [{display_sym}] ({res['sector']})\n  - แนะนำ: 🔴 ขาย\n  - กำไร/ขาดทุน: {pl_pct:+.2f}%\n  - Stop Loss: {sl:.2f} ฿")
                else:
                    analysis_reports.append(f"✅ [{display_sym}] ({res['sector']})\n  - แนะนำ: 🟢 ถือต่อ\n  - กำไร/ขาดทุน: {pl_pct:+.2f}%\n  - Trailing Stop: {sl:.2f} ฿")
            
            total_portfolio = cash + total_stock_value
            progress = (total_portfolio / target) * 100
            reply_text = f"🧠 วิเคราะห์พอร์ต (เป้าหมาย {progress:.1f}%):\n\n" + ("\n\n".join(analysis_reports) if analysis_reports else "• ไม่มีหุ้นในพอร์ต")

        elif text == "สแกนหุ้น":
            watchlist = portfolio.get("watchlist", SET50_WATCHLIST)
            scanned_stocks = []
            for sym in watchlist:
                res = analyze_stock_deep(portfolio, sym)
                if res and res['action'] not in ["❌ ห้ามซื้อเด็ดขาด", "SELL"] and res['score'] >= 50:
                    scanned_stocks.append((clean_symbol_display(sym), res))
            scanned_stocks.sort(key=lambda x: x[1]['score'], reverse=True)
            top10 = scanned_stocks[:10]
            scan_results = [f"🏆 {sym} [{res['sector']}]: {res['action']} (คะแนน {res['score']}/100) | โซนซื้อ: {res['buy_price']} ฿" for sym, res in top10]
            set_ok, set_msg = check_set_market_trend()
            reply_text = f"📊 ผลสแกน Watchlist / SET50 (Top 10):\n{set_msg}\n\n" + ("\n".join(scan_results) if scan_results else "ไม่มีหุ้นผ่านเกณฑ์")

        elif text == "สแกนหุ้นซิ่ง":
            watchlist = portfolio.get("watchlist", SET50_WATCHLIST)
            breakout_results = []
            for sym in watchlist:
                res = analyze_momentum_breakout(sym)
                if res and res['is_breakout']:
                    breakout_results.append((clean_symbol_display(sym), res))
            results_text = [f"🔥 {sym} (แนวต้าน: {res['high_20']} ฿ | Vol: {res['vol_ratio']:.1f}x)" for sym, res in breakout_results]
            reply_text = "🚀 ผลสแกนหุ้นซิ่ง (Momentum Breakout):\n\n" + ("\n".join(results_text) if results_text else "ยังไม่มีหุ้นเบรกแนวต้านวันนี้")

        elif text == "วิเคราะห์อุตสาหกรรม":
            reply_text = generate_sector_analysis_text(portfolio)

        elif text == "สรุปหุ้นวันนี้":
            reply_text = generate_top5_summary_text(portfolio)

        elif text == "สถิติการเทรด":
            reply_text = calculate_trade_statistics(portfolio)

        elif text.startswith("สแกน "):
            if len(parts) >= 2:
                symbol = format_symbol(parts[1])
                display_sym = clean_symbol_display(symbol)
                res = analyze_stock_deep(portfolio, symbol)
                if res:
                    reasons_formatted = "\n  - ".join(res['reasons'])
                    est_cost = res['buy_price'] * res['rec_shares']
                    reply_text = (
                        f"🔎 วิเคราะห์เจาะลึก [{display_sym}] ({res['sector']})\n"
                        f"- คะแนน: {res['score']}/100 | ราคา: {res['price']:.2f} ฿\n"
                        f"- สถานะ: [{res['action']}]\n"
                        f"- เหตุผล:\n  - {reasons_formatted}\n"
                        f"- โซนซื้อ: {res['buy_price']:.2f} ฿ | Stop Loss: {res['stop_loss']:.2f} ฿\n"
                        f"- แนะนำซื้อ: {res['rec_shares']} หุ้น (~{est_cost:,.2f} ฿)"
                    )
                else:
                    reply_text = f"❌ ไม่สามารถดึงข้อมูลหุ้น {display_sym} ได้"
            else:
                reply_text = "❌ รูปแบบไม่ถูกต้อง (ตัวอย่าง: สแกน PTT)"

        elif text in ["ช่วยเหลือ", "help", "คำสั่ง"]:
            reply_text = (
                "📋 คู่มือคำสั่งบอต:\n"
                "1. ดูพอร์ต\n"
                "2. วิเคราะห์พอร์ต\n"
                "3. สแกนหุ้น (จาก Watchlist/SET50)\n"
                "4. สแกนหุ้นซิ่ง (Momentum Breakout)\n"
                "5. วิเคราะห์อุตสาหกรรม (Sector)\n"
                "6. สรุปหุ้นวันนี้ (Top 5)\n"
                "7. สถิติการเทรด\n"
                "8. สแกน [หุ้น] (เช่น สแกน PTT)\n"
                "9. ซื้อ / ขาย [หุ้น] [จำนวน] [ราคา] [ค่าธรรมเนียม]\n"
                "10. ติดตาม / เลิกติดตาม / รายการติดตาม"
            )

        elif text == "รายการติดตาม":
            watchlist = portfolio.get("watchlist", SET50_WATCHLIST)
            clean_watchlist = [clean_symbol_display(s) for s in watchlist]
            reply_text = "📋 รายชื่อหุ้นในระบบติดตาม:\n" + ", ".join(clean_watchlist)

        elif text.startswith("ติดตาม "):
            if len(parts) >= 2:
                symbol = format_symbol(parts[1])
                display_sym = clean_symbol_display(symbol)
                watchlist = portfolio.get("watchlist", SET50_WATCHLIST.copy())
                if symbol not in watchlist:
                    watchlist.append(symbol)
                    portfolio['watchlist'] = watchlist
                    update_user_portfolio(user_id, portfolio)
                    reply_text = f"✅ เพิ่ม {display_sym} เรียบร้อยแล้ว"
                else:
                    reply_text = f"ℹ️ {display_sym} มีในรายการอยู่แล้ว"

        elif text.startswith("เลิกติดตาม "):
            if len(parts) >= 2:
                symbol = format_symbol(parts[1])
                display_sym = clean_symbol_display(symbol)
                watchlist = portfolio.get("watchlist", SET50_WATCHLIST.copy())
                if symbol in watchlist:
                    watchlist.remove(symbol)
                    portfolio['watchlist'] = watchlist
                    update_user_portfolio(user_id, portfolio)
                    reply_text = f"🗑️ ลบ {display_sym} ออกแล้ว"
                else:
                    reply_text = f"❌ ไม่พบ {display_sym} ในรายการ"

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
                        portfolio['stocks'][symbol] = {
                            "shares": new_shares,
                            "total_cost": new_total_cost,
                            "average_cost": new_avg_price,
                            "stop_loss": sl
                        }
                        if "transactions" not in portfolio:
                            portfolio['transactions'] = []
                        portfolio['transactions'].append({
                            "type": "BUY", "symbol": symbol, "shares": shares, "price": price, "fee": fee, "net_amount": total_cost, "date": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")
                        })
                        update_user_portfolio(user_id, portfolio)
                        reply_text = f"✅ บันทึกซื้อสำเร็จ: {display_sym} จำนวน {shares} หุ้น ทุนเฉลี่ย {new_avg_price:.2f} ฿"
                    else:
                        reply_text = "⚠️ เงินสดในพอร์ตไม่พอ"
                except Exception as e:
                    reply_text = f"❌ เกิดข้อผิดพลาด: {str(e)}"

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
                            "type": "SELL", "symbol": symbol, "shares": shares_to_sell, "price": price, "fee": fee, "net_amount": total_income, "realized_pnl": profit, "date": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")
                        })
                        update_user_portfolio(user_id, portfolio)
                        reply_text = f"✅ บันทึกขายสำเร็จ: {display_sym} กำไรสุทธิ {profit:+,.2f} ฿ ({profit_pct:+.2f}%)"
                    else:
                        reply_text = f"❌ หุ้น {display_sym} ในพอร์ตมีไม่พอขาย"
                except Exception as e:
                    reply_text = f"❌ เกิดข้อผิดพลาด: {str(e)}"
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
