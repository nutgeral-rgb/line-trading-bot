"""Render assets/richmenu.png (2500x843, 3x2 buttons) for the LINE rich menu.

Needs Pillow and a Thai font (e.g. apt-get install fonts-thai-tlwg). The PNG is
committed, so the bot itself does not need Pillow or fonts at runtime.
Keep BUTTONS in the same order as RICH_MENU_BUTTONS in bot.py.
"""
import sys
from PIL import Image, ImageDraw, ImageFont

BUTTONS = [("ทบทวนพอร์ต", "ถือ/ขาย/ซื้อเพิ่ม + ปันผล"), ("แนะนำหุ้นสะสม", "หุ้นปันผลรอบนี้"),
           ("ดูพอร์ต", "เงินสดและหุ้นที่บันทึก"), ("แผนหมุนเวียน", "ถุงเทรดรอบสั้น"),
           ("ดูถุงหมุนเวียน", "เงินสด หุ้น กำไรขาดทุน"), ("เมนูคำสั่ง", "คำสั่งทั้งหมด")]
WIDTH, HEIGHT, COLS, ROWS = 2500, 843, 3, 2


def main(font_path="/usr/share/fonts/truetype/tlwg/Garuda-Bold.ttf",
         small_path="/usr/share/fonts/truetype/tlwg/Garuda.ttf", out="assets/richmenu.png"):
    image = Image.new("RGB", (WIDTH, HEIGHT), "#0F3D3E")
    draw = ImageDraw.Draw(image)
    title, sub = ImageFont.truetype(font_path, 96), ImageFont.truetype(small_path, 54)
    cell_w, cell_h = WIDTH // COLS, HEIGHT // ROWS
    for index, (label, hint) in enumerate(BUTTONS):
        col, row = index % COLS, index // COLS
        x0, y0 = col * cell_w, row * cell_h
        draw.rounded_rectangle([x0 + 18, y0 + 18, x0 + cell_w - 18, y0 + cell_h - 18],
                               radius=36, fill="#1F6F6B" if index % 2 == 0 else "#2A7F62")
        cx = x0 + cell_w / 2
        draw.text((cx, y0 + cell_h * 0.42), label, font=title, fill="white", anchor="mm")
        draw.text((cx, y0 + cell_h * 0.72), hint, font=sub, fill="#D7F0E8", anchor="mm")
    image.save(out, optimize=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
