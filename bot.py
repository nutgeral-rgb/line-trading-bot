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

# กำหนดค่า LINE Access Token และ Secret
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', 'YOUR_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

PORTFOLIO_FILE = 'portfolio.json'

def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"cash": 100000, "stocks": {}, "last_user_id": None}

def save_portfolio(data):
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# เพิ่มหน้านี้เพื่อให้ UptimeRobot เข้ามาเช็กแล้วเจอสถานะปกติ (แก้ปัญหา Error 404)
@app.route("/", methods=['GET'])
def home():
    return "Line Trading Bot is running!"

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
    
    portfolio = load_portfolio()
    portfolio['last_user_id'] = user_id
    save_portfolio(portfolio)

    reply_text = ""

    if text == "ดูพอร์ต":
        cash = portfolio.get("cash", 100000)
        stocks = portfolio.get("stocks", {})
        reply_text = f"💰 เงินสด: {cash:,.2f} บาท\n📦 หุ้นในพอร์ต:\n"
        if not stocks:
            reply_text += "ไม่มีหุ้นในพอร์ต"
        else:
            for symbol, info in stocks.items():
                reply_text += f"- {symbol}: {info['shares']} หุ้น (ซื้อที่ {info['buy_price']:.2f} ฿)\n"

    elif text.startswith("ซื้อ"):
        parts = text.split()
        if len(parts) >= 3:
            symbol = parts[1].upper() + ".BK"
            try:
                shares = int(parts[2])
                ticker = yf.Ticker(symbol)
                todays_data = ticker.history(period='1d')
                if not todays_data.empty:
                    price = todays_data['Close'].iloc[-1]
                    total_cost = price * shares
                    cash = portfolio.get("cash", 100000)
                    
                    if cash >= total_cost:
                        portfolio['cash'] = cash - total_cost
                        if symbol not in portfolio['stocks']:
                            portfolio['stocks'][symbol] = {"shares": 0, "buy_price": 0}
                        
                        old_shares = portfolio['stocks'][symbol]['shares']
                        old_price = portfolio['stocks'][symbol]['buy_price']
                        new_shares = old_shares + shares
                        new_avg_price = ((old_shares * old_price) + (shares * price)) / new_shares if new_shares > 0 else price
                        
                        portfolio['stocks'][symbol]['shares'] = new_shares
                        portfolio['stocks'][symbol]['buy_price'] = new_avg_price
                        save_portfolio(portfolio)
                        
                        tp = price * 1.10
                        sl = price * 0.97
                        reply_text = (f"✅ ซื้อสำเร็จ:\n- หุ้น: {symbol}\n- จำนวน: {shares} หุ้น\n"
                                      f"- ราคาซื้อ: {price:.2f} ฿\n- ยอดรวม: {total_cost:.2f} ฿\n"
                                      f"🎯 เป้า TP: {tp:.2f} ฿ (+10%)\n🛡️ จุด SL: {sl:.2f} ฿ (-3%)")
                    else:
                        reply_text = "❌ เงินสดในพอร์ตไม่พอซื้อ"
                else:
                    reply_text = f"❌ ไม่พบข้อมูลราคาหุ้น {symbol}"
            except Exception as e:
                reply_text = f"❌ เกิดข้อผิดพลาด: {str(e)}"
        else:
            reply_text = "⚠️ รูปแบบคำสั่งไม่ถูกต้อง (ตัวอย่าง: ซื้อ PTT 10)"
    else:
        reply_text = f"รับทราบคำสั่ง: {text}\n(พิมพ์ 'ดูพอร์ต' หรือ 'ซื้อ [หุ้น] [จำนวน]')"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

def automated_background_task():
    with app.app_context():
        portfolio = load_portfolio()
        user_id = portfolio.get('last_user_id')
        if not user_id:
            return
        
        watchlist = ["PTT.BK", "AOT.BK", "CPALL.BK", "KBANK.BK", "SCC.BK"]
        scan_alerts = []
        for sym in watchlist:
            try:
                df = yf.Ticker(sym).history(period='5d')
                if not df.empty and len(df) >= 2:
                    change = (df['Close'].iloc[-1] - df['Close'].iloc[-2]) / df['Close'].iloc[-2] * 100
                    if change > 1.0:
                        scan_alerts.append(f"🔥 [Auto Screener] สัญญาณซื้อ {sym} บวก +{change:.2f}%")
            except:
                pass

        if scan_alerts:
            line_bot_api.push_message(user_id, TextSendMessage(text="\n".join(scan_alerts)))

        stocks = portfolio.get("stocks", {})
        for sym, info in stocks.items():
            try:
                current_price = yf.Ticker(sym).history(period='1d')['Close'].iloc[-1]
                buy_price = info['buy_price']
                if current_price >= buy_price * 1.10:
                    line_bot_api.push_message(user_id, TextSendMessage(text=f"🎯 [Auto Alert] หุ้น {sym} ถึงเป้า Take Profit แล้ว! ราคาปัจจุบัน: {current_price:.2f} ฿"))
                elif current_price <= buy_price * 0.97:
                    line_bot_api.push_message(user_id, TextSendMessage(text=f"🛡️ [Auto Alert] หุ้น {sym} หลุดจุด Stop Loss! ราคาปัจจุบัน: {current_price:.2f} ฿"))
            except:
                pass

scheduler = BackgroundScheduler()
scheduler.add_job(func=automated_background_task, trigger="interval", minutes=30)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
