"""Portfolio-wide review with explicit missing data and scenario assumptions."""
import math
from datetime import date

from dividend_goal import income_summary, money, project_goal
from long_term_review import assess_holding


def instrument_type(symbol, holding):
    return holding.get('instrument_type') or ('WARRANT' if '-W' in symbol else
                                             'REIT' if 'REIT' in symbol else 'STOCK')


def value_portfolio(portfolio, frames, expected_date):
    cash = money(portfolio.get('cash', 0))
    rows, unpriced, equity = [], [], 0.
    for symbol, holding in portfolio.get('stocks', {}).items():
        shares = int(holding.get('shares', 0) or 0)
        if shares <= 0:
            continue
        frame = frames.get(symbol)
        value, price, quote_date = None, None, None
        status = holding.get('status', 'ACTIVE')
        if frame is not None and not frame.empty and 'Close' in frame:
            quote_date = frame.index[-1].strftime('%Y-%m-%d')
            try:
                close = float(frame['Close'].iloc[-1])
                if status == 'ACTIVE' and quote_date == expected_date.isoformat() and math.isfinite(close) and close > 0:
                    price, value = close, shares * close
            except (TypeError, ValueError, OverflowError):
                pass
        if value is None:
            unpriced.append(symbol)
        else:
            equity += value
        rows.append({'symbol': symbol, 'shares': shares, 'price': price, 'value': value,
                     'quote_date': quote_date, 'status': status, 'weight': None,
                     'instrument_type': instrument_type(symbol, holding)})
    total = cash + equity if not unpriced else None
    if total:
        for row in rows:
            row['weight'] = row['value'] / total
    return {'cash': cash, 'known_total': cash + equity, 'total': total,
            'equity': equity, 'rows': rows, 'unpriced': unpriced}


def format_manager_report(portfolio, frames, fundamentals, expected_date, today):
    value = value_portfolio(portfolio, frames, expected_date)
    lines = ['🧭 วิเคราะห์พอร์ตและเป้าปันผล',
             f'ราคาปิดที่ต้องใช้: {expected_date.isoformat()} | แหล่งราคา Yahoo Finance',
             f"เงินสดที่บันทึก {value['cash']:,.2f} บาท"]
    if value['total'] is None:
        lines += [f"⚠️ มูลค่าไม่ครบ: ส่วนที่ตีราคาได้รวมเงินสด {value['known_total']:,.2f} บาท",
                  'งดสรุปมูลค่ารวม สัดส่วน และเงินเติมที่ต้องใช้จนกว่าข้อมูลครบ']
    else:
        total = value['total']
        lines.append(f'มูลค่ารวมตามข้อมูลบอต {total:,.2f} บาท')
        if total > 0:
            stress = .15 * value['equity'] / total * 100
            lines.append(f'สมมติหลักทรัพย์ลด 15% เงินสดคงเดิม: พอร์ตลด {stress:.1f}%')
            if stress >= 10:
                lines.append('⚠️ เกินกรอบทบทวน 10%: พิจารณาลดการกระจุกตัวก่อนเพิ่มความเสี่ยง')
    lines.append('\n📌 รายหลักทรัพย์')
    for row in value['rows'][:20]:
        symbol = row['symbol']
        display = symbol.removesuffix('.BK')
        if row['status'] != 'ACTIVE':
            lines.append(f"• {display}: {row['status']} — ตรวจสิทธิ/สถานะกับโบรกเกอร์ ยังตีมูลค่าขายไม่ได้")
            continue
        if row['value'] is None:
            lines.append(f"• {display}: ข้อมูลไม่พอ/ราคาเก่า ({row['quote_date'] or 'ไม่มีราคา'})")
            continue
        weight = f" | {row['weight']*100:.1f}%" if row['weight'] is not None else ''
        lines.append(f"• {display} {row['shares']:,} หน่วย | {row['value']:,.0f} บาท{weight}")
        if row['instrument_type'] == 'WARRANT':
            lines.append('  ตรวจวันหมดอายุและต้นทุนใช้สิทธิ ไม่จัดเป็นหุ้นสะสมปันผล')
        elif row['instrument_type'] == 'REIT':
            lines.append('  ตรวจเฉพาะทาง: กระแสเงินสดและอายุสิทธิการเช่า แยกเงินคืนทุนจากปันผล')
        else:
            evidence = fundamentals.get(symbol) or {}
            review = assess_holding(frames.get(symbol), evidence,
                                    portfolio['stocks'][symbol].get('avg_price'))
            lines.append(f"  {review['status']}: {', '.join(review['reasons'])}")
        if row['weight'] is not None and row['weight'] > .35:
            lines.append('  น้ำหนักเกิน 35%: ทบทวนลดสัดส่วนหรือเติมในส่วนอื่นก่อน ไม่ขายเพราะน้ำหนักอย่างเดียว')
    if len(value['rows']) > 20:
        lines.append(f"แสดง 20 จาก {len(value['rows'])} รายการ; มูลค่ารวมคิดครบทุกตัว")
    goal = portfolio.get('dividend_goal')
    strategy = portfolio.get('investment_strategy') or {}
    planned = money(strategy.get('monthly_contribution_baht', 1000))
    contributed = 0.
    for entry in portfolio.get('history', []):
        if isinstance(entry, dict) and entry.get('type') == 'MONTHLY_CONTRIBUTION' and entry.get('month') == today.strftime('%Y-%m'):
            try:
                contributed += money(entry.get('amount', 0))
            except ValueError:
                continue
    lines += ['\n🎯 เป้าหมาย', f'แผนเติม {planned:,.0f}/เดือน | แจ้งเติมจริงเดือนนี้ {contributed:,.2f} บาท']
    if not goal:
        lines.append('ยังไม่ตั้งเป้า: พิมพ์ ตั้งเป้าปันผล 15000 10')
    else:
        projection = project_goal(goal, value['total'], planned, today)
        lines += [f"ปันผลสุทธิเฉลี่ย {goal['monthly_net']:,.0f}/เดือน ภายใน {goal['target_date']}",
                  'เงินต้นเป้าตามสมมติปันผลสุทธิ 3% / 4% / 5%:',
                  ' / '.join(f'{x:,.0f}' for x in projection['capital_targets']) + ' บาท']
        if date.fromisoformat(goal['target_date']) <= today:
            lines.append('ถึงวันเป้าหมายแล้ว: ทบทวนรายได้จริงและเป้าหมายใหม่')
        elif projection['months'] == 0:
            lines.append('เหลือไม่ถึงหนึ่งรอบเติมเงินปลายเดือนก่อนวันเป้าหมาย: ทบทวนยอดที่ยังขาด')
        elif projection['scenarios']:
            middle = projection['scenarios'][1]
            required = middle['required_monthly']
            lines += [f"เหลือ {projection['months']} เดือน | สมมติผลตอบแทนรวม 6% และปันผลสุทธิปลายทาง 4%",
                      f'ต้องเติมประมาณ {required:,.0f} บาท/เดือน',
                      f'เกินแผนเติมปัจจุบัน {max(0,required-planned):,.0f} บาท/เดือน',
                      f'เดือนนี้ยังขาดจากยอดจำลอง {max(0,required-contributed):,.0f} บาท',
                      'หากเติมตามแผน ที่ผลตอบแทนรวม 4% / 6% / 8%:',
                      ' / '.join(f"{r['projected']:,.0f}" for r in projection['scenarios']) + ' บาท ณ วันเป้า',
                      f"กรณีกลางเทียบปันผลสุทธิ 4% ≈ {middle['monthly_income_at_net4']:,.0f} บาท/เดือน"]
    income = income_summary(portfolio, today)
    lines += ['\n💵 เงินรับจริงที่บันทึกย้อนหลัง 365 วัน',
              f"ปันผลสุทธิ {income['net_dividends']:,.2f} บาท | เฉลี่ย {income['monthly_average']:,.2f}/เดือน",
              f"เงินคืนทุน {income['capital_returns']:,.2f} บาท (ไม่นับเป็นปันผล)",
              '\nตัวเลขอนาคตเป็นสมมติฐาน: เติมปลายเดือน นำปันผลกลับลงทุน ไม่ปรับเงินเฟ้อ ไม่รับประกันผลตอบแทน',
              'ระดับ 10% เป็นกรอบทบทวน ไม่ใช่การรับประกันขาดทุนสูงสุด; รายงานนี้ยังไม่ใช่ผลตอบแทนจริงทั้งบัญชี',
              'ซื้อเพิ่ม: ใช้ แนะนำหุ้นสะสม ตรวจราคาและค่าธรรมเนียมก่อนซื้อจริง; บอตไม่ส่งคำสั่งซื้อขาย']
    return '\n'.join(lines)
