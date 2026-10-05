from apscheduler.schedulers.background import BackgroundScheduler
import datetime

# ฟังก์ชันตรวจสอบอัตโนมัติ (รันทุกๆ 30 นาที)
def automated_monitor():
    with app.app_context():
        # 1. เช็กหุ้นในพอร์ตเพื่อเตือนขาย (TP / SL)
        # (โหลดข้อมูลพอร์ต และลูปเช็กราคาปัจจุบันผ่าน yfinance เหมือนคำสั่งดูพอร์ต)
        print("Running automated portfolio & market check...")
        
        # ตัวอย่างการส่งข้อความแจ้งเตือนเข้า LINE (ใช้ User ID หรือ Group ID ที่บันทึกไว้)
        # line_bot_api.push_message(USER_ID, TextSendMessage(text="[Auto Alert] หุ้น XXX ถึงเป้า Take Profit แล้ว!"))

# ตั้งเวลาให้ระบบทำงานอัตโนมัติ
scheduler = BackgroundScheduler()
scheduler.add_job(func=automated_monitor, trigger="interval", minutes=30)
scheduler.start()
