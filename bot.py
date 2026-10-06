import os
import json
import pandas as pd
import yfinance as yf
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from apscheduler.schedulers.background import BackgroundScheduler

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

@app.route("/", methods=['GET'])
def home():
    return "Financial Advisor Trading Bot is running!"

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
                
                stock_summary += (f"- {symbol}: {shares} หุ้น\n  ทุนสุทธิ: {total_cost:,.2f} ฿ (เฉลี่ย {avg_cost:.2f})\n"
                                  f"  ปัจจุบัน: {cur_price:.2f} ฿ | กำไร/ขาดทุน: {pl:+,.2f} ฿ ({pl_pct:+.2f}%)\n"
                                  f"  RSI: {rsi:.1f}\n\n")
        
        total_portfolio = cash + total_stock_value
        progress = (total_portfolio / target) * 100
        diff = target - total_portfolio
        paused = total_portfolio <= plan.get("trading_paused_at_value", 8000)
        
        status_text = "⚠️ พอร์ตถึงขีดจำกัดความเสี่ยง (หยุดซื้อเพิ่ม)" if paused else "🟢 พอร์ตอยู่ในเกณฑ์ปกติ"

        reply_text = (
            f"📊 สถานะพอร์ตและการเงิน:\n"
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
        watchlist = portfolio.get("watchlist", [])
        
        total_stock_value = 0
        sell_advices = []
        
        for symbol, info in stocks.items():
            shares = info['shares']
            total_cost = info.get('total_cost', 0)
            avg_cost = info.get('average_cost', 0)
            sl = info.get('stop_loss', avg_cost * 0.95)
            try:
                df = yf.Ticker(symbol).history(period='60d')
                cur_price = df['Close'].iloc[-1]
                rsi = calculate_rsi(df)
                hist = calculate_macd(df)[2]
                ema50 = calculate_ema(df, 50)
            except:
                cur_price = avg_cost
                rsi, hist, ema50 = 50.0, 0.0, avg_cost
            
            market_value = shares * cur_price
            total_stock_value += market_value
            pl_pct = ((cur_price - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0
            
            if cur_price <= sl:
                sell_advices.append(f"🚨 {symbol} หลุด Stop Loss ({sl:.2f} ฿) แนะนำพิจารณาขายตัดขาดทุน")
            elif rsi > 70 or (cur_price < ema50 and hist < 0):
                sell_advices.append(f"⚠️ {symbol} สัญญาณเทคนิคอ่อนแอ (RSI: {rsi:.1f}) พิจารณาลดพอร์ต")
            else:
                sell_advices.append(f"✅ {symbol} โครงสร้างยังแข็งแกร่ง แนะนำถือต่อ")

        watchlist_picks = []
        for sym in watchlist:
            try:
                df = yf.Ticker(sym).history(period='60d')
                if not df.empty and len(df) >= 30:
                    rsi = calculate_rsi(df)
                    hist = calculate_macd(df)[2]
                    latest = df['Close'].iloc[-1]
                    if rsi < 40 or (hist > 0 and latest > calculate_ema(df, 50)):
                        watchlist_picks.append(f"• {sym} (RSI: {rsi:.1f}, MACD บวก น่าสะสม)")
            except:
                pass

        total_portfolio = cash + total_stock_value
        progress = (total_portfolio / target) * 100

        reply_text = (
            f"🧠 รายงานวิเคราะห์แผนการเงิน & เทคนิค:\n"
            f"- เป้าหมายพอร์ต: {target:,.2f} ฿ (คืบหน้า {progress:.1f}%)\n\n"
            f"📋 คำแนะนำหุ้นในพอร์ต:\n" + ("\n".join(sell_advices) if sell_advices else "• ไม่มีหุ้นในพอร์ต") +
            f"\n\n💡 โอกาสจาก Watchlist:\n" + ("\n".join(watchlist_picks[:3]) if watchlist_picks else "• รอดูจังหวะตลาดพักตัว")
        )

    elif text in ["ช่วยเหลือ", "help", "คำสั่ง"]:
        reply_text = (
            "📋 คู่มือคำสั่งบอตวางแผนการเงิน:\n"
            "1. ดูพอร์ต -> ตรวจสอบเงินสด มูลค่าพอร์ต และเป้าหมาย\n"
            "2. วิเคราะห์พอร์ต -> คำแนะนำซื้อ/ขายตามเทคนิคและ Stop Loss\n"
            "3. สแกนหุ้น -> ตรวจสอบสัญญาณ Watchlist (RSI/MACD/EMA)\n"
            "4. ซื้อ [หุ้น] [จำนวน] [ราคา] [ค่าธรรมเนียม] -> บันทึกซื้อจริง\n"
            "   (เช่น ซื้อ PTT.BK 10 35.0 20)\n"
            "5. ขาย [หุ้น] [จำนวน] [ราคา] [ค่าธรรมเนียม] -> บันทึกขายจริง\n"
            "   (เช่น ขาย PTT.BK 10 38.0 20)\n"
            "6. รายการติดตาม / ติดตาม [หุ้น] / เลิกติดตาม [หุ้น]"
        )

    elif text == "รายการติดตาม":
        watchlist = portfolio.get("watchlist", [])
        reply_text = "📋 รายชื่อหุ้นในระบบติดตาม:\n" + ", ".join(watchlist) if watchlist else "ยังไม่มีหุ้นในรายการติดตาม"

    elif text == "สแกนหุ้น":
        watchlist = portfolio.get("watchlist", [])
        scan_results = []
        for sym in watchlist:
            try:
                df = yf.Ticker(sym).history(period='60d')
                if not df.empty and len(df) >= 30:
                    latest = df['Close'].iloc[-1]
                    rsi = calculate_rsi(df)
                    hist = calculate_macd(df)[2]
                    ema50 = calculate_ema(df, 50)
                    
                    status = "ปกติ"
                    if rsi < 35 and hist > 0:
                        status = "สัญญาณกลับตัว (Oversold+MACD ตัดขึ้น)"
                    elif rsi > 70:
                        status = "ระวัง Overbought"
                    elif latest > ema50 and hist > 0:
                        status = "ขาขึ้นแข็งแกร่ง"
                    
                    scan_results.append(f"- {sym}: {latest:.2f} ฿ | RSI: {rsi:.1f} [{status}]")
            except:
                scan_results.append(f"- {sym}: ข้อมูลผิดพลาด")
        reply_text = "📈 ผลวิเคราะห์ Watchlist:\n" + ("\n".join(scan_results) if scan_results else "ยังไม่มีหุ้นใน Watchlist")

    elif text.startswith("ติดตาม "):
        if len(parts) >= 2:
            symbol = parts[1].upper()
            if not symbol.endswith(".BK"):
                symbol += ".BK"
            watchlist = portfolio.get("watchlist", [])
            if symbol not in watchlist:
                watchlist.append(symbol)
                portfolio['watchlist'] = watchlist
                update_user_portfolio(user_id, portfolio)
                reply_text = f"✅ เพิ่ม {symbol} เรียบร้อยแล้ว"
            else:
                reply_text = f"ℹ️ {symbol} มีในรายการอยู่แล้ว"
        else:
            reply_text = "❌ รูปแบบไม่ถูกต้อง (ตัวอย่าง: ติดตาม PTT)"

    elif text.startswith("เลิกติดตาม "):
        if len(parts) >= 2:
            symbol = parts[1].upper()
            if not symbol.endswith(".BK"):
                symbol += ".BK"
            watchlist = portfolio.get("watchlist", [])
            if symbol in watchlist:
                watchlist.remove(symbol)
                portfolio['watchlist'] = watchlist
                update_user_portfolio(user_id, portfolio)
                reply_text = f"🗑️ ลบ {symbol} ออกแล้ว"
            else:
                reply_text = f"❌ ไม่พบ {symbol} ในรายการ"
        else:
            reply_text = "❌ รูปแบบไม่ถูกต้อง (ตัวอย่าง: เลิกติดตาม PTT)"

    elif text.startswith("ซื้อ"):
        if len(parts) >= 4:
            symbol = parts[1].upper()
            if not symbol.endswith(".BK"):
                symbol += ".BK"
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
                    
                    reply_text = (f"✅ บันทึกซื้อสำเร็จ:\n- หุ้น: {symbol}\n- จำนวน: {shares} หุ้น\n"
                                  f"- ราคาซื้อ: {price:.2f} ฿ | ค่าธรรมเนียม: {fee:.2f} ฿\n"
                                  f"- จ่ายสุทธิ: {total_cost:,.2f} ฿\n"
                                  f"- ทุนเฉลี่ยใหม่: {new_avg_price:.2f} ฿/หุ้น\n"
                                  f"- ตั้ง Stop Loss อัตโนมัติ: {sl:.2f} ฿")
                else:
                    reply_text = "⚠️ เงินสดในพอร์ตไม่พอจ่ายรวมค่าธรรมเนียม"
            except Exception as e:
                reply_text = f"❌ เกิดข้อผิดพลาด: {str(e)}"
        else:
            reply_text = "❌ รูปแบบคำสั่งไม่ถูกต้อง (ตัวอย่าง: ซื้อ PTT.BK 10 35.0 20)"

    elif text.startswith("ขาย"):
        if len(parts) >= 4:
            symbol = parts[1].upper()
            if not symbol.endswith(".BK"):
                symbol += ".BK"
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
                    
                    reply_text = (f"✅ บันทึกขายสำเร็จ:\n- หุ้น: {symbol}\n- จำนวน: {shares_to_sell} หุ้น\n"
                                  f"- ราคาขาย: {price:.2f} ฿ | ค่าธรรมเนียม: {fee:.2f} ฿\n"
                                  f"- รับเงินสุทธิ: {total_income:,.2f} ฿\n"
                                  f"- กำไร/ขาดทุนสุทธิ: {profit:+,.2f} ฿ ({profit_pct:+.2f}%)")
                else:
                    reply_text = f"❌ หุ้น {symbol} ในพอร์ตมีไม่พอขาย"
            except Exception as e:
                reply_text = f"❌ เกิดข้อผิดพลาด: {str(e)}"
        else:
            reply_text = "❌ รูปแบบคำสั่งไม่ถูกต้อง (ตัวอย่าง: ขาย PTT.BK 10 38.0 20)"
    else:
        reply_text = f"🤖 รับทราบคำสั่ง: {text}\n(พิมพ์ 'ช่วยเหลือ' เพื่อดูคำสั่งทั้งหมด)"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
