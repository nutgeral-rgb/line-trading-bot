from datetime import datetime
import json
import os
import threading
import time
import pandas as pd
import yfinance as yf
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

line_bot_api = LineBotApi(
    "ikfRWKCkWZsBiZqqJy/0XZVi33oYenA0ZoQ/Ba2hlfo1nh4H4W3riGz89K35jLNevUmHCZs+bpK/qyNKRaaKGPZsVsVc/rwWvhtnUbZE+PvG4w6MmkH7eKIGztWXFSLLE0MbaL6yIgPqS8x3iWiE6QdB04t89/1O/w1cDnyilFU="
)
handler = WebhookHandler("f739518f774d7da71d96f746a489e613")

PORTFOLIO_FILE = "portfolio.json"


def load_portfolio():
  if os.path.exists(PORTFOLIO_FILE):
    try:
      with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        print("[DEBUG] โหลดพอร์ตจากไฟล์สำเร็จ:", data)
        return data
    except Exception as e:
      print("[DEBUG] โหลดพอร์ตล้มเหลว:", e)
  print("[DEBUG] ไม่พบไฟล์พอร์ต ใช้ค่าเริ่มต้นเงินสด 9,975.00 บาท")
  return {
      "cash": 9975.0,
      "stocks": {},
      "initial_capital": 10000.0,
      "max_loss_limit": 2000.0,
      "history": [],
  }


def save_portfolio():
  try:
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
      json.dump(portfolio, f, ensure_ascii=False, indent=4)
    print("[DEBUG] บันทึกพอร์ตลงไฟล์สำเร็จ")
  except Exception as e:
    print("[DEBUG] บันทึกพอร์ตล้มเหลว:", e)


def check_set_trend():
  """ตรวจสอบว่าตลาดรวม SET Index อยู่ในขาขึ้น (เหนือ EMA 50) หรือไม่"""
  try:
    set_ticker = yf.Ticker("^SET.BK")
    hist = set_ticker.history(period="3mo")
    if not hist.empty and len(hist) >= 50:
      close = hist["Close"]
      ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
      current_set = float(close.iloc[-1])
      return current_set >= ema50
  except Exception as e:
    print("SET Trend Check Error:", e)
  return True  # หากดึงข้อมูลไม่ได้ ให้ผ่านไปก่อนเพื่อไม่ให้ติดขัด


portfolio = load_portfolio()
active_user_id = None


def monitor_portfolio():
  global active_user_id
  while True:
    time.sleep(60)
    if active_user_id and portfolio["stocks"]:
      for sym, data in list(portfolio["stocks"].items()):
        try:
          ticker = yf.Ticker(sym + ".BK")
          hist = ticker.history(period="1d")
          if not hist.empty:
            current_price = float(hist["Close"].iloc[-1])

            # 1. ระบบ Trailing Stop อัปเดตราคาสูงสุดและจุดล็อกกำไร
            cost = data.get("cost", current_price)
            peak = data.get("peak_price", cost)
            if current_price > peak:
              data["peak_price"] = current_price
              data["trailing_stop"] = current_price * 0.97  # ล็อกกำไรถอย 3%
              save_portfolio()

            tp = data.get("tp")
            sl = data.get("sl")
            trailing_stop = data.get("trailing_stop")

            # 2. เงื่อนไขแจ้งเตือน Take Profit หรือ Trailing Stop
            if (tp and current_price >= tp) or (
                trailing_stop
                and current_price <= trailing_stop
                and current_price > cost
            ):
              reason = (
                  "Take Profit เป้าหมาย"
                  if (tp and current_price >= tp)
                  else "Trailing Stop ล็อกกำไร"
              )
              line_bot_api.push_message(
                  active_user_id,
                  TextSendMessage(
                      text=(
                          f"🎯 แจ้งเตือน {reason}!\nหุ้น {sym} ถึงเกณฑ์ทำกำไรแล้ว"
                          f"!\n- ราคาปัจจุบัน: {current_price:.2f} ฿\n- จุดสูงสุด:"
                          f" {data.get('peak_price', current_price):.2f}"
                          f" ฿\nพิจารณาขายทำรอบได้เลยครับ!"
                      )
                  ),
              )
              data["tp"] = None
              data["trailing_stop"] = None
              save_portfolio()

            # 3. เงื่อนไขแจ้งเตือน Stop Loss
            elif sl and current_price <= sl:
              line_bot_api.push_message(
                  active_user_id,
                  TextSendMessage(
                      text=(
                          f"🛡️ แจ้งเตือนด่วน Stop Loss!\nหุ้น {sym}"
                          f" หลุดแนวรับจำกัดความเสี่ยง!\n- ราคาปัจจุบัน:"
                          f" {current_price:.2f} ฿\n- จุดตัดขาดทุน (SL):"
                          f" {sl:.2f} ฿\nพิจารณาตัดความเสี่ยงด่วนครับ!"
                      )
                  ),
              )
              data["sl"] = None
              save_portfolio()
        except Exception as e:
          print("Background Monitor Error:", e)


@app.route("/callback", methods=["POST"])
def callback():
  signature = request.headers["X-Line-Signature"]
  body = request.get_data(as_text=True)
  try:
    handler.handle(body, signature)
  except InvalidSignatureError:
    abort(400)
  return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
  global active_user_id
  active_user_id = event.source.user_id

  text = event.message.text.strip()
  args = text.split()
  command = args[0] if args else ""

  if text == "ดูพอร์ต":
    stock_value = 0.0
    stock_details = ""
    for sym, data in portfolio["stocks"].items():
      try:
        ticker = yf.Ticker(sym + ".BK")
        hist = ticker.history(period="1d")
        current_price = (
            float(hist["Close"].iloc[-1])
            if not hist.empty
            else float(data["cost"])
        )
      except:
        current_price = float(data["cost"])
      val = data["shares"] * current_price
      stock_value += val
      stock_details += (
          f"- {sym}: {data['shares']:,} หุ้น (ทุน: {data['cost']:.2f}"
          f" | ปัจจุบัน: {current_price:.2f})\n"
      )

    total_value = portfolio["cash"] + stock_value
    profit_loss = total_value - portfolio["initial_capital"]
    roi_pct = (profit_loss / portfolio["initial_capital"]) * 100
    target_profit = portfolio["initial_capital"]
    progress_pct = max(0.0, min(100.0, (profit_loss / target_profit) * 100))

    history = portfolio.get("history", [])
    total_cycles = len(history)
    win_cycles = sum(1 for h in history if h["realized_pnl"] > 0)
    loss_cycles = sum(1 for h in history if h["realized_pnl"] <= 0)
    win_rate = (win_cycles / total_cycles * 100) if total_cycles > 0 else 0.0

    reply_text = (
        "📊 **สถานะพอร์ตและแผนทำรอบ (1 ปี):**\n"
        f"- **เงินสด:** {portfolio['cash']:,.2f} ฿\n"
        f"- **มูลค่าหุ้น:** {stock_value:,.2f} ฿\n"
        f"- **พอร์ตวม:** {total_value:,.2f} ฿\n"
        f"- **กำไร/ขาดทุน:** {profit_loss:,.2f} ฿ ({roi_pct:+.2f}%)\n"
        f"- **ความคืบหน้าเป้าหมาย 100%:** {progress_pct:.1f}%\n\n"
        f"🎯 **สถิติการทำรอบ (Cycle Tracker):**\n"
        f"- **รอบที่สำเร็จทั้งหมด:** {total_cycles} รอบ\n"
        f"- **ชนะ (กำไร):** {win_cycles} รอบ | **แพ้ (ขาดทุน):** {loss_cycles}"
        f" รอบ\n"
        f"- **Win Rate:** {win_rate:.1f}%"
    )
    if stock_details:
      reply_text += f"\n\nหุ้นที่ถืออยู่:\n{stock_details}"

  elif command == "ประวัติรอบ":
    history = portfolio.get("history", [])
    if not history:
      reply_text = (
          "📁 ยังไม่มีประวัติการปิดรอบการเทรด\n(ประวัติจะบันทึกอัตโนมัติเมื่อมีการขายหุ้น)"
      )
    else:
      reply_text = "📁 **ประวัติรอบการทำรอบที่ผ่านมา:**\n"
      for i, h in enumerate(history[-5:], 1):
        reply_text += (
            f"{i}. [{h['date']}] {h['symbol']} ({h['shares']} หุ้น)\n"
            f"   - ซื้อ/ขาย: {h['buy_price']:.2f} ➡️ {h['sell_price']:.2f}\n"
            f"   - กำไรสุทธิ: {h['realized_pnl']:+,.2f} ฿"
            f" ({h['return_pct']:+.2f}%)\n"
        )

  elif command == "ราคา" and len(args) > 1:
    sym = args[1].upper()
    try:
      ticker = yf.Ticker(sym + ".BK")
      hist = ticker.history(period="1d")
      if not hist.empty:
        price = float(hist["Close"].iloc[-1])
        reply_text = f"ราคาหุ้น {sym}.BK ล่าสุด: {price:.2f} บาท"
      else:
        reply_text = f"ไม่พบข้อมูลหุ้น {sym}"
    except Exception:
      reply_text = f"เกิดข้อผิดพลาดในการดึงข้อมูลหุ้น {sym}"

  elif text == "สแกนตลาด":
    watchlist = ["PTT", "PTTEP", "ADVANC", "CPALL", "BDMS", "AOT", "KBANK", "GULF"]
    reply_text = "📊 สรุปกระดานหุ้นทำรอบ (Swing Trading):\n"
    for sym in watchlist:
      try:
        ticker = yf.Ticker(sym + ".BK")
        hist = ticker.history(period="2mo")
        if not hist.empty and len(hist) >= 20:
          close = hist["Close"]
          sma5 = float(close.rolling(window=5).mean().iloc[-1])
          sma20 = float(close.rolling(window=20).mean().iloc[-1])
          current = float(close.iloc[-1])
          delta = close.diff()
          gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
          loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
          rs = gain / loss
          rsi = 100 - (100 / (1 + rs))
          current_rsi = float(rsi.iloc[-1])
          trend = "🚀ขาขึ้น" if sma5 >= sma20 else "⚠️พักตัว"
          reply_text += (
              f"- {sym}: {current:.2f} ฿ | RSI: {current_rsi:.1f} | {trend}\n"
          )
      except:
        continue
    reply_text += "\nพิมพ์ 'วิเคราะห์ [หุ้น]' เพื่อดูเจาะลึกรายตัว"

  elif command == "วิเคราะห์" and len(args) > 1:
    sym = args[1].upper()
    try:
      ticker = yf.Ticker(sym + ".BK")
      hist = ticker.history(period="3mo")
      if not hist.empty and len(hist) >= 30:
        close = hist["Close"]
        sma5 = float(close.rolling(window=5).mean().iloc[-1])
        sma20 = float(close.rolling(window=20).mean().iloc[-1])
        current = float(close.iloc[-1])
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        current_rsi = float(rsi.iloc[-1])
        exp12 = close.ewm(span=12, adjust=False).mean()
        exp26 = close.ewm(span=26, adjust=False).mean()
        macd = exp12 - exp26
        signal_line = macd.ewm(span=9, adjust=False).mean()
        current_macd = float(macd.iloc[-1])
        current_signal = float(signal_line.iloc[-1])
        vol = hist["Volume"]
        avg_vol = float(vol.rolling(window=5).mean().iloc[-1])
        current_vol = float(vol.iloc[-1])
        vol_status = (
            "🔥 วอลุ่มสูง" if current_vol > avg_vol * 1.2 else "⚖️ วอลุ่มปกติ"
        )
        trend_signal = "🚀 ขาขึ้นสั้น" if sma5 > sma20 else "⚠️ พักตัว/อ่อนแรง"
        rsi_signal = (
            "🔥 ซื้อมาก"
            if current_rsi > 70
            else ("💧 ขายมาก (น่าสะสม)" if current_rsi < 30 else "✅ ปกติ")
        )
        macd_signal = (
            "📈 โมเมนตัมขาขึ้น" if current_macd > current_signal else "📉 ขาลง"
        )
        reply_text = (
            f"ผลวิเคราะห์เชิงลึก {sym}.BK:\n"
            f"- ราคา: {current:.2f} บาท\n"
            f"- SMA5/20: {sma5:.2f}/{sma20:.2f} ({trend_signal})\n"
            f"- RSI(14): {current_rsi:.1f} ({rsi_signal})\n"
            f"- MACD: {current_macd:.2f} ({macd_signal})\n"
            f"- Volume: {vol_status}"
        )
      else:
        reply_text = f"ข้อมูลหุ้น {sym} ไม่เพียงพอสำหรับการวิเคราะห์"
    except Exception as e:
      reply_text = f"เกิดข้อผิดพลาดในการวิเคราะห์: {str(e)}"

  elif command == "ซื้อ" and len(args) > 2:
    sym = args[1].upper()
    try:
      shares = int(args[2])
      if shares <= 0:
        raise ValueError()
    except ValueError:
      reply_text = "กรุณาระบุจำนวนหุ้นเป็นตัวเลข เช่น ซื้อ PTT 100"
    else:
      # 1. ตรวจสอบ SET Trend Filter ก่อนอนุญาตให้ซื้อ
      if not check_set_trend():
        reply_text = (
            f"⚠️ ปฏิเสธคำสั่งซื้อ {sym}: ตลาดรวม (SET Index) อยู่ในช่วงขาลง"
            " (ต่ำกว่า EMA 50) งดเปิดความเสี่ยงตามแผนกลยุทธ์"
        )
      else:
        try:
          ticker = yf.Ticker(sym + ".BK")
          hist = ticker.history(period="1d")
          if hist.empty:
            reply_text = f"ไม่พบข้อมูลหุ้น {sym}"
          else:
            price = float(hist["Close"].iloc[-1])
            total_cost = price * shares

            # 2. คำนวณ RRR (Risk-Reward Ratio) ต้องมากกว่าหรือเท่ากับ 2.0
            tp_price = float(price * 1.10)
            sl_price = float(price * 0.97)
            reward = tp_price - price
            risk = price - sl_price
            rrr = reward / risk if risk > 0 else 0

            if total_cost > portfolio["cash"]:
              reply_text = (
                  f"เงินสดไม่พอ! ต้องการ {total_cost:,.2f} บาท"
                  f" (มี {portfolio['cash']:,.2f} บาท)"
              )
            elif sym not in portfolio["stocks"] and len(portfolio["stocks"]) >= 2:
              reply_text = "จำกัดถือหุ้นไม่เกิน 2 ตัว เพื่อควบคุมความเสี่ยง"
            elif rrr < 2.0:
              reply_text = (
                  f"⚠️ ปฏิเสธคำสั่งซื้อ: สัดส่วนความคุ้มค่า (RRR) เท่ากับ"
                  f" {rrr:.2f} ต่ำกว่าเกณฑ์ขั้นต่ำ (1:2)"
              )
            else:
              portfolio["cash"] = float(portfolio["cash"] - total_cost)

              if sym in portfolio["stocks"]:
                old = portfolio["stocks"][sym]
                new_shares = int(old["shares"] + shares)
                new_cost = float(
                    ((old["shares"] * old["cost"]) + (shares * price))
                    / new_shares
                )
                portfolio["stocks"][sym] = {
                    "shares": new_shares,
                    "cost": new_cost,
                    "tp": tp_price,
                    "sl": sl_price,
                    "peak_price": max(price, old.get("peak_price", price)),
                    "trailing_stop": price * 0.97,
                }
              else:
                portfolio["stocks"][sym] = {
                    "shares": int(shares),
                    "cost": price,
                    "tp": tp_price,
                    "sl": sl_price,
                    "peak_price": price,
                    "trailing_stop": price * 0.97,
                }

              save_portfolio()

              reply_text = (
                  f"ซื้อสำเร็จ (ผ่านเกณฑ์กลยุทธ์ RRR & SET):\n- หุ้น: {sym}\n-"
                  f" จำนวน: {shares:,} หุ้น\n- ราคาซื้อ: {price:.2f} ฿\n- ยอดรวม:"
                  f" {total_cost:,.2f} ฿\n🎯 เป้า TP: {tp_price:.2f} ฿ (+10%)\n🛡️"
                  f" จุด SL: {sl_price:.2f} ฿ (-3%)\n📈 Trailing Stop: เปิดใช้งาน"
              )
        except Exception as e:
          reply_text = f"เกิดข้อผิดพลาด: {str(e)}"

  elif command == "ขาย" and len(args) > 2:
    sym = args[1].upper()
    try:
      shares = int(args[2])
      if shares <= 0:
        raise ValueError()
    except ValueError:
      reply_text = "กรุณาระบุจำนวนหุ้นเป็นตัวเลข เช่น ขาย PTT 100"
    else:
      if sym not in portfolio["stocks"]:
        reply_text = f"คุณไม่ได้ถือหุ้น {sym} ในพอร์ต"
      elif shares > portfolio["stocks"][sym]["shares"]:
        reply_text = (
            f"หุ้นในพอร์ตไม่พอขาย! มีอยู่ {portfolio['stocks'][sym]['shares']:,}"
            f" หุ้น"
        )
      else:
        try:
          ticker = yf.Ticker(sym + ".BK")
          hist = ticker.history(period="1d")
          price = (
              float(hist["Close"].iloc[-1])
              if not hist.empty
              else float(portfolio["stocks"][sym]["cost"])
          )

          buy_cost = float(portfolio["stocks"][sym]["cost"])
          total_sale = float(price * shares)
          cost_basis = float(buy_cost * shares)
          realized_pnl = float(total_sale - cost_basis)
          return_pct = float(((price - buy_cost) / buy_cost) * 100)

          if "history" not in portfolio:
            portfolio["history"] = []

          portfolio["history"].append({
              "symbol": sym,
              "shares": int(shares),
              "buy_price": buy_cost,
              "sell_price": price,
              "realized_pnl": realized_pnl,
              "return_pct": return_pct,
              "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
          })

          portfolio["cash"] = float(portfolio["cash"] + total_sale)
          portfolio["stocks"][sym]["shares"] = int(
              portfolio["stocks"][sym]["shares"] - shares
          )

          if portfolio["stocks"][sym]["shares"] == 0:
            del portfolio["stocks"][sym]

          save_portfolio()

          reply_text = (
              f"ขายทำรอบสำเร็จ!\n- หุ้น: {sym}\n- จำนวน: {shares:,}"
              f" หุ้น\n- ราคาขาย: {price:.2f} บาท\n- เงินสดกลับเข้าพอร์ต:"
              f" {total_sale:,.2f} บาท\n- กำไร/ขาดทุนรอบนี้:"
              f" {realized_pnl:,.2f} บาท ({return_pct:+.2f}%)\n📁"
              " บันทึกประวัติรอบลงระบบเรียบร้อย"
          )
        except Exception as e:
          reply_text = f"เกิดข้อผิดพลาด: {str(e)}"

  else:
    reply_text = (
        "คำสั่งระบบพอร์ตหมุนเวียน:\n"
        "- ดูพอร์ต\n"
        "- ประวัติรอบ\n"
        "- ราคา [หุ้น]\n"
        "- สแกนตลาด\n"
        "- วิเคราะห์ [หุ้น]\n"
        "- ซื้อ [หุ้น] [จำนวน]\n"
        "- ขาย [หุ้น] [จำนวน]"
    )

  line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))


if __name__ == "__main__":
  threading.Thread(target=monitor_portfolio, daemon=True).start()
  app.run(port=5000)