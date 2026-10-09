from collections import Counter, defaultdict
import math

import pandas as pd


def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def symbol_name(symbol):
    return str(symbol).removesuffix(".BK")


class ScannerUpgrade:
    """
    วิเคราะห์จากแท่งรายวันที่จบแล้ว
    ไม่ส่งคำสั่งซื้อขายและไม่แก้ข้อมูลธุรกรรม
    """

    def __init__(
        self,
        get_frames,
        universe,
        sectors,
        index_symbol,
        commission,
        get_portfolio,
        risk_profile,
        risk_status,
        now_local,
        min_avg_value=10000000,
        min_atr_pct=1.2,
        max_atr_pct=12.0,
        max_extension_pct=12.0,
        board_lot=100,
        allow_odd_lot=False,
    ):
        self.get_frames = get_frames
        self.universe = list(dict.fromkeys(universe))
        self.sectors = sectors
        self.index_symbol = index_symbol
        self.commission = commission
        self.get_portfolio = get_portfolio
        self.risk_profile = risk_profile
        self.risk_status = risk_status
        self.now_local = now_local

        self.min_avg_value = float(min_avg_value)
        self.min_atr_pct = float(min_atr_pct)
        self.max_atr_pct = float(max_atr_pct)
        self.max_extension_pct = float(max_extension_pct)
        self.board_lot = max(1, int(board_lot))
        self.allow_odd_lot = bool(allow_odd_lot)

        # เกณฑ์เริ่มต้นสำหรับทดลอง ไม่ใช่ค่าที่พิสูจน์ว่าดีที่สุด
        self.min_coverage = 0.80
        self.min_market_sample = 20
        self.min_sector_sample = 3
        self.min_breadth = 50.0
        self.max_sector_weight = 0.35
        self.min_net_rr = 1.50

        # สมมติส่วนต่างราคาที่เสียเปรียบ 0.2% ต่อข้าง
        self.slippage = 0.002

        # อายุข้อมูลตามวันปฏิทิน ไม่ใช่ปฏิทินซื้อขายของ SET
        self.max_data_age_days = 7

    def _completed_daily(self, frame, now):
        if frame is None or frame.empty:
            return None

        columns = ["Open", "High", "Low", "Close", "Volume"]
        if not set(columns).issubset(frame.columns):
            return None

        data = frame[columns].copy()
        index = pd.DatetimeIndex(pd.to_datetime(data.index))

        if index.tz is not None:
            index = index.tz_convert("Asia/Bangkok").tz_localize(None)

        data.index = index.normalize()
        data = data.sort_index()
        data = data[~data.index.duplicated(keep="last")]

        for column in columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")

        data = data.replace([float("inf"), -float("inf")], float("nan"))
        data = data.dropna(subset=columns)

        valid = (
            (data["Open"] > 0)
            & (data["High"] > 0)
            & (data["Low"] > 0)
            & (data["Close"] > 0)
            & (data["Volume"] >= 0)
            & (data["High"] >= data["Low"])
            & (data["High"] >= data["Open"])
            & (data["High"] >= data["Close"])
            & (data["Low"] <= data["Open"])
            & (data["Low"] <= data["Close"])
        )
        data = data.loc[valid]

        today = pd.Timestamp(now.date())

        # เวลาตัดข้อมูลที่เลือกอย่างระมัดระวังสำหรับระบบนี้
        # ไม่ใช่การประกาศเวลาเปิด/ปิดตลาดอย่างเป็นทางการ
        if (now.hour, now.minute) < (17, 30):
            data = data[data.index < today]
        else:
            data = data[data.index <= today]

        return None if data.empty else data

    @staticmethod
    def _rsi(close, period=14):
        changes = close.diff().dropna().tolist()
        if len(changes) < period:
            return None

        average_gain = sum(max(x, 0.0) for x in changes[:period]) / period
        average_loss = sum(max(-x, 0.0) for x in changes[:period]) / period

        for change in changes[period:]:
            average_gain = (
                average_gain * (period - 1) + max(change, 0.0)
            ) / period
            average_loss = (
                average_loss * (period - 1) + max(-change, 0.0)
            ) / period

        if average_loss == 0:
            return 100.0 if average_gain > 0 else 50.0

        return 100.0 - 100.0 / (1.0 + average_gain / average_loss)

    def _snapshot(self, data):
        if data is None or len(data) < 90:
            return None

        close = data["Close"]
        price = float(close.iloc[-1])

        ema20 = close.ewm(span=20, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()

        macd = (
            close.ewm(span=12, adjust=False).mean()
            - close.ewm(span=26, adjust=False).mean()
        )
        histogram = macd - macd.ewm(span=9, adjust=False).mean()

        previous_close = close.shift(1)
        true_range = pd.concat(
            [
                data["High"] - data["Low"],
                (data["High"] - previous_close).abs(),
                (data["Low"] - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        atr = float(true_range.rolling(14).mean().iloc[-1])
        if not math.isfinite(atr) or atr <= 0:
            return None

        previous20 = data.iloc[-21:-1]
        average_volume = float(previous20["Volume"].mean())
        volume_ratio = (
            float(data["Volume"].iloc[-1]) / average_volume
            if average_volume > 0
            else 0.0
        )

        last = data.iloc[-1]
        candle_range = float(last["High"] - last["Low"])
        close_location = (
            float(last["Close"] - last["Low"]) / candle_range
            if candle_range > 0
            else 0.0
        )
        strong_close = (
            close_location >= 0.65
            and float(last["Close"]) > float(last["Open"])
        )

        # ใช้เฉพาะสัปดาห์ที่จบแล้ว ณ วันที่ข้อมูล
        weekly_close = close.resample("W-FRI").last().dropna()
        weekly_close = weekly_close[
            weekly_close.index <= data.index[-1]
        ]

        weekly_up = None
        if len(weekly_close) >= 26:
            weekly_ema = weekly_close.ewm(
                span=20, adjust=False
            ).mean()
            weekly_up = bool(
                weekly_close.iloc[-1] > weekly_ema.iloc[-1]
                and weekly_ema.iloc[-1] > weekly_ema.iloc[-2]
            )

        # Squeeze เทียบกับประวัติก่อนหน้า ไม่ใช้เป็นสัญญาณซื้อเดี่ยว ๆ
        middle = close.rolling(20).mean()
        deviation = close.rolling(20).std(ddof=0)
        upper = middle + 2 * deviation
        lower = middle - 2 * deviation
        bandwidth = (upper - lower) / middle

        threshold = bandwidth.rolling(
            120, min_periods=60
        ).quantile(0.20).shift(1)

        squeezed = bandwidth <= threshold
        is_squeeze = bool(squeezed.iloc[-1])

        squeeze_release = bool(
            squeezed.iloc[-6:-1].any()
            and price > float(upper.iloc[-2])
            and bandwidth.iloc[-1] > bandwidth.iloc[-2]
            and volume_ratio >= 1.3
            and strong_close
        )

        prior_high = float(previous20["High"].max())
        prior_low = float(data["Low"].iloc[-11:-1].min())

        ema20_value = float(ema20.iloc[-1])
        ema50_value = float(ema50.iloc[-1])
        extension = (price / ema20_value - 1.0) * 100.0

        daily_up = price > ema20_value > ema50_value
        setup = None

        if daily_up:
            if price > prior_high and volume_ratio >= 1.3:
                setup = "เบรกแนวต้าน 20 วัน"
            elif (
                float(last["Low"]) <= ema20_value * 1.015
                and price >= ema20_value
                and histogram.iloc[-1] > histogram.iloc[-2]
                and strong_close
            ):
                setup = "ย่อตัวและกลับมายืนเหนือ EMA20"
            elif (
                histogram.iloc[-1] > 0
                and histogram.iloc[-1] > histogram.iloc[-2]
                and strong_close
            ):
                setup = "โมเมนตัมขาขึ้นต่อเนื่อง"

        # วาง Stop ใต้แนวรับและเผื่อความผันผวน
        # หากระยะไกลเกินเกณฑ์ จะไม่บีบ Stop ให้ใกล้เพื่อฝืนซื้อ
        stop = round(min(prior_low * 0.995, price - 1.5 * atr), 2)
        target = round(price + 2 * (price - stop), 2)

        return {
            "date": data.index[-1].date(),
            "price": price,
            "ema20": ema20_value,
            "ema50": ema50_value,
            "daily_up": daily_up,
            "weekly_up": weekly_up,
            "macd_hist": float(histogram.iloc[-1]),
            "rsi": self._rsi(close),
            "atr_pct": atr / price * 100.0,
            "volume_ratio": volume_ratio,
            "average_volume": average_volume,
            "average_value": float(
                (previous20["Close"] * previous20["Volume"]).mean()
            ),
            "ret20": (price / float(close.iloc[-21]) - 1.0) * 100.0,
            "extension": extension,
            "strong_close": strong_close,
            "squeeze": is_squeeze,
            "squeeze_release": squeeze_release,
            "setup": setup,
            "stop": stop,
            "target": target,
        }

    def _context(self, extra_symbols=None):
        now = self.now_local()
        requested = list(dict.fromkeys(
            self.universe
            + list(extra_symbols or [])
            + [self.index_symbol]
        ))

        raw_frames = self.get_frames(requested)
        snapshots = {}

        for symbol in requested:
            try:
                daily = self._completed_daily(raw_frames.get(symbol), now)
                snapshot = self._snapshot(daily)
                if snapshot is not None:
                    snapshots[symbol] = snapshot
            except (TypeError, ValueError, KeyError, IndexError):
                # หุ้นที่ข้อมูลผิดจะไม่ถูกนับเป็นผ่าน
                continue

        dates = [
            snapshots[symbol]["date"]
            for symbol in self.universe
            if symbol in snapshots
        ]
        asof = max(dates) if dates else None

        sample = {
            symbol: snapshots[symbol]
            for symbol in self.universe
            if symbol in snapshots
            and snapshots[symbol]["date"] == asof
        }

        coverage = len(sample) / len(self.universe) if self.universe else 0
        breadth = (
            sum(s["price"] > s["ema50"] for s in sample.values())
            / len(sample) * 100.0
            if sample
            else None
        )

        index = snapshots.get(self.index_symbol)
        index_ok = bool(
            index is not None
            and asof is not None
            and index["date"] == asof
        )
        index_up = bool(
            index_ok
            and index["price"] > index["ema50"]
            and index["ema20"] >= index["ema50"]
        )

        issues = []
        if len(sample) < self.min_market_sample:
            issues.append("จำนวนหุ้นอ้างอิงที่ใช้ได้ต่ำกว่าเกณฑ์")
        if coverage < self.min_coverage:
            issues.append("ข้อมูลกลุ่มอ้างอิงครอบคลุมไม่ถึง 80%")
        if not index_ok:
            issues.append("ข้อมูลดัชนีอ้างอิงขาดหายหรือคนละวันกับหุ้น")
        if asof is None:
            issues.append("ไม่พบวันที่ข้อมูลที่ใช้วิเคราะห์ได้")
        elif (now.date() - asof).days > self.max_data_age_days:
            issues.append("ข้อมูลเก่าเกินเกณฑ์ที่ตั้งไว้")

        grouped = defaultdict(list)
        for symbol, snapshot in sample.items():
            sector = self.sectors.get(symbol)
            if sector:
                grouped[sector].append(snapshot["ret20"])

        sector_scores = {
            sector: float(pd.Series(values).median())
            for sector, values in grouped.items()
            if len(values) >= self.min_sector_sample
        }

        leaders = sorted(
            [
                sector
                for sector, performance in sector_scores.items()
                if performance > 0
            ],
            key=lambda sector: sector_scores[sector],
            reverse=True,
        )[:3]

        return {
            "now": now,
            "asof": asof,
            "snapshots": snapshots,
            "sample_count": len(sample),
            "coverage": coverage,
            "breadth": breadth,
            "index": index if index_ok else None,
            "index_up": index_up,
            "leaders": leaders,
            "issues": issues,
        }

    def _analyse(self, context, symbol, min_score):
        snapshot = context["snapshots"].get(symbol)
        if snapshot is None:
            return None

        sector = self.sectors.get(symbol, "ไม่ระบุกลุ่ม")
        index = context["index"]
        rs = (
            snapshot["ret20"] - index["ret20"]
            if index is not None and snapshot["date"] == index["date"]
            else None
        )

        healthy_market = bool(
            not context["issues"]
            and context["index_up"]
            and context["breadth"] is not None
            and context["breadth"] >= self.min_breadth
        )

        # น้ำหนักรวมสูงสุด 100 คะแนน
        score = 0
        reasons = []

        if snapshot["daily_up"]:
            score += 20
            reasons.append("ราคายืนเหนือ EMA20 และ EMA50")
        if snapshot["weekly_up"] is True:
            score += 15
            reasons.append("แนวโน้มสัปดาห์ที่จบแล้วยังเป็นบวก")
        if rs is not None and rs > 0:
            score += 15
            reasons.append(f"ผลตอบแทน 20 วันสูงกว่าดัชนี {rs:.1f} จุดเปอร์เซ็นต์")
        if sector in context["leaders"]:
            score += 10
            reasons.append("กลุ่มมีโมเมนตัมเด่นในชุดตัวอย่าง")
        if snapshot["volume_ratio"] >= 1.3:
            score += 10
            reasons.append(f"Volume {snapshot['volume_ratio']:.2f} เท่าของค่าเฉลี่ย")
        if snapshot["setup"]:
            score += 10
        if snapshot["squeeze_release"]:
            score += 5
            reasons.append("เบรกขึ้นหลัง Squeeze พร้อม Volume ยืนยัน")
        if snapshot["strong_close"]:
            score += 5
        if healthy_market:
            score += 10

        blockers = list(context["issues"])

        if snapshot["date"] != context["asof"]:
            blockers.append("ข้อมูลหุ้นไม่ตรงวันอ้างอิง")
        if not healthy_market and not context["issues"]:
            blockers.append("ภาวะตลาดยังไม่ผ่านเกณฑ์เปิดสถานะใหม่")
        if snapshot["weekly_up"] is None:
            blockers.append("ข้อมูลรายสัปดาห์ไม่พอ")
        elif snapshot["weekly_up"] is False:
            blockers.append("แนวโน้มรายสัปดาห์ไม่ผ่าน")
        if snapshot["average_value"] < self.min_avg_value:
            blockers.append("สภาพคล่องต่ำกว่าเกณฑ์")
        if not self.min_atr_pct <= snapshot["atr_pct"] <= self.max_atr_pct:
            blockers.append("ความผันผวนอยู่นอกช่วงที่กำหนด")
        if snapshot["extension"] > self.max_extension_pct:
            blockers.append("ราคาห่าง EMA20 มากเกินเกณฑ์")
        if snapshot["rsi"] is None or snapshot["rsi"] >= 80:
            blockers.append("RSI ไม่พร้อมใช้หรือสูงเกินเกณฑ์")
        if not snapshot["strong_close"]:
            blockers.append("แท่งเทียนยังไม่ยืนยันการเข้าซื้อ")
        if not snapshot["setup"]:
            blockers.append("ยังไม่มีรูปแบบเข้าซื้อ")
        if (
            snapshot["stop"] <= 0
            or snapshot["stop"] >= snapshot["price"]
            or (snapshot["price"] - snapshot["stop"])
            / snapshot["price"] > 0.10
        ):
            blockers.append("ระยะ Stop ไม่เหมาะกับเกณฑ์ความเสี่ยง")
        if score < min_score:
            blockers.append(f"คะแนนต่ำกว่าเกณฑ์ {min_score}")

        return {
            **snapshot,
            "symbol": symbol,
            "sector": sector,
            "rs": rs,
            "score": score,
            "reasons": reasons,
            "blockers": list(dict.fromkeys(blockers)),
            "action": "BUY" if not blockers else "WATCH",
        }

    def _portfolio_state(self, portfolio, context):
        cash = number(portfolio.get("cash"), -1)
        issues = []
        if cash < 0:
            issues.append("ข้อมูลเงินสดไม่ถูกต้อง")

        equity = max(0.0, cash)
        open_risk = 0.0
        sector_values = defaultdict(float)

        for symbol, holding in portfolio.get("stocks", {}).items():
            quantity = int(number(holding.get("shares")))
            if quantity <= 0:
                continue

            snapshot = context["snapshots"].get(symbol)
            if snapshot is None or snapshot["date"] != context["asof"]:
                issues.append(
                    f"ประเมินมูลค่า {symbol_name(symbol)} ไม่ได้: ข้อมูลราคาไม่ครบ"
                )
                continue

            price = snapshot["price"]
            stop = number(holding.get("stop_loss"))
            value = price * quantity
            equity += value
            sector_values[self.sectors.get(symbol, "ไม่ระบุกลุ่ม")] += value

            if stop <= 0:
                issues.append(f"{symbol_name(symbol)} ยังไม่มี Stop ที่ใช้ได้")
            elif price <= stop:
                issues.append(
                    f"{symbol_name(symbol)} ถึง Stop เดิม: ตรวจสถานะก่อนซื้อเพิ่ม"
                )

            open_risk += max(
                0.0, price - max(0.0, stop) * (1 - self.slippage)
            ) * quantity
            open_risk += self.commission(value)

        if issues:
            return None, issues

        return {
            "cash": cash,
            "equity": equity,
            "open_risk": open_risk,
            "sector_values": sector_values,
        }, []

    def _economics(self, result, quantity):
        entry = result["price"] * (1 + self.slippage)
        stop_exit = result["stop"] * (1 - self.slippage)
        target_exit = result["target"] * (1 - self.slippage)

        cost = entry * quantity + self.commission(entry * quantity)
        stop_proceeds = (
            stop_exit * quantity - self.commission(stop_exit * quantity)
        )
        target_proceeds = (
            target_exit * quantity - self.commission(target_exit * quantity)
        )
        return cost, max(0.0, cost - stop_proceeds), target_proceeds - cost

    def _size(self, result, state, profile):
        equity = state["equity"]
        risk_budget = min(
            equity * profile["risk_per_trade"],
            max(0.0, equity * profile["max_total_risk"] - state["open_risk"]),
        )

        sector_room = max(
            0.0,
            equity * self.max_sector_weight
            - state["sector_values"][result["sector"]],
        )
        money_cap = min(
            state["cash"],
            equity * profile["max_weight"],
            sector_room,
        )

        entry = result["price"] * (1 + self.slippage)
        cap = max(0, int(money_cap / entry))

        # จำกัดปริมาณเสนอซื้อไม่เกิน 0.5% ของ Volume เฉลี่ย
        cap = min(cap, int(result["average_volume"] * 0.005))

        if cap < self.board_lot:
            step = 1 if self.allow_odd_lot else self.board_lot
        else:
            step = self.board_lot

        low, high = 0, cap // step
        while low < high:
            middle = (low + high + 1) // 2
            quantity = middle * step
            cost, loss, _ = self._economics(result, quantity)

            if cost <= money_cap and loss <= risk_budget:
                low = middle
            else:
                high = middle - 1

        quantity = low * step
        if quantity <= 0:
            return 0

        _, loss, profit = self._economics(result, quantity)
        if loss <= 0 or profit / loss < self.min_net_rr:
            return 0

        return quantity

    def _header(self, context, title):
        breadth = (
            f"{context['breadth']:.1f}%"
            if context["breadth"] is not None
            else "ไม่ทราบ"
        )
        return [
            title,
            f"เวลารายงาน: {context['now'].strftime('%d/%m/%Y %H:%M')}",
            f"ข้อมูลราคา: {context['asof'] or 'ไม่พร้อมใช้'}",
            "ใช้แท่งรายวันที่จบแล้ว ไม่ใช่ราคาสด",
            "",
            "ภาพรวม",
            f"• Breadth: {breadth} จาก "
            f"{context['sample_count']}/{len(self.universe)} หุ้นอ้างอิง",
            "• ดัชนีอ้างอิง: "
            + ("ข้อมูลพร้อมใช้" if context["index"] is not None else "ข้อมูลไม่ครบ"),
            "• กลุ่มเด่นในชุดตัวอย่าง: "
            + (", ".join(context["leaders"]) or "ยังยืนยันไม่ได้"),
        ]

    def _entry_report(self, portfolio, symbols, title, limit):
        holdings = list(portfolio.get("stocks", {}).keys())
        context = self._context(list(symbols) + holdings)
        lines = self._header(context, title)

        state, issues = self._portfolio_state(portfolio, context)
        if issues:
            return "\n".join(
                lines + ["", "งดเสนอซื้อ: ข้อมูลพอร์ตยังไม่พร้อม"]
                + [f"• {issue}" for issue in issues]
            )

        status = self.risk_status(portfolio, state["equity"])
        profile = self.risk_profile(
            portfolio, state["equity"], status["drawdown"]
        )

        slots = max(
            0,
            profile["max_positions"] - len(portfolio.get("stocks", {})),
        )
        lines.extend([
            f"• เงินสด: {state['cash']:,.2f} บาท",
            f"• ช่องเปิดสถานะใหม่: {slots}",
        ])

        if status["paused"]:
            return "\n".join(
                lines + ["", "ข้อสรุป: หยุดเปิดสถานะใหม่ตามกฎความเสี่ยงพอร์ต"]
            )

        if slots == 0:
            return "\n".join(
                lines + ["", "ข้อสรุป: จำนวนหุ้นที่ถือเต็มเพดานแล้ว"]
            )

        analysed = []
        missing = 0

        for symbol in dict.fromkeys(symbols):
            if symbol in portfolio.get("stocks", {}):
                continue

            result = self._analyse(context, symbol, profile["min_score"])
            if result is None:
                missing += 1
            else:
                analysed.append(result)

        analysed.sort(key=lambda item: item["score"], reverse=True)
        selected = []

        # กันเงินสด ความเสี่ยง และสัดส่วนกลุ่มทีละรายการ
        # การเปลี่ยนแปลงนี้อยู่ในแผนจำลอง ไม่แก้พอร์ตจริง
        for result in analysed:
            if result["action"] != "BUY":
                continue
            if len(selected) >= min(limit, slots):
                break

            quantity = self._size(result, state, profile)
            if quantity <= 0:
                continue

            cost, loss, profit = self._economics(result, quantity)
            selected.append((result, quantity, cost, loss, profit))

            state["cash"] -= cost
            state["open_risk"] += loss
            state["sector_values"][result["sector"]] += cost

        if not selected:
            lines.extend(["", "ข้อสรุป: ยังไม่มีรายการซื้อที่ผ่านทั้งสัญญาณและงบพอร์ต"])

            counts = Counter(
                reason
                for result in analysed
                for reason in result["blockers"]
            )
            for reason, count in counts.most_common(3):
                lines.append(f"• {reason}: {count} หุ้น")

            if any(result["action"] == "BUY" for result in analysed):
                lines.append(
                    "• มีสัญญาณซื้อ แต่ติดงบ เงินสด สัดส่วนกลุ่ม "
                    "ขนาดล็อต หรืออัตรากำไรต่อขาดทุนหลังค่าใช้จ่าย"
                )
            if missing:
                lines.append(f"• ข้อมูลไม่พอวิเคราะห์: {missing} หุ้น")

            lines.append("ไม่พบสัญญาณ ไม่ได้แปลว่าระบบขัดข้อง")
            return "\n".join(lines)

        lines.extend([
            "",
            f"ข้อสรุป: พบ {len(selected)} รายการน่าพิจารณา",
            "คะแนนเป็นคะแนนตามกฎ ไม่ใช่เปอร์เซ็นต์โอกาสชนะ",
        ])

        for index, (result, quantity, cost, loss, profit) in enumerate(selected, 1):
            lines.extend([
                "",
                f"{index}) {symbol_name(result['symbol'])} — พิจารณาซื้อ",
                f"กลุ่ม: {result['sector']} | คะแนน: {result['score']}/100",
                f"รูปแบบ: {result['setup']}",
                "เหตุผล",
            ])
            lines.extend(f"• {reason}" for reason in result["reasons"][:3])

            lines.extend([
                "แผนราคา",
                f"• อ้างอิง {result['price']:.2f} | Stop {result['stop']:.2f}",
                f"• เป้า 2R ก่อนค่าใช้จ่าย: {result['target']:.2f}",
                "ขนาดสถานะ",
                f"• {quantity} หุ้น | เงินที่กันไว้ประมาณ {cost:,.2f} บาท",
                f"• ขาดทุนกรณีออกที่ Stop: {loss:,.2f} บาท",
                f"• กำไรกรณีถึงเป้า: {profit:,.2f} บาท",
                f"• กำไรต่อขาดทุนหลังสมมติค่าใช้จ่าย: {profit / loss:.2f} เท่า",
            ])

            if quantity % self.board_lot:
                lines.append(
                    "• เป็น Odd Lot: ต้องตรวจราคากับสภาพคล่องกระดานหน่วยย่อย"
                )

        lines.extend([
            "",
            "ข้อควรระวัง",
            "• ใช้ค่าธรรมเนียมที่ตั้งไว้ และสมมติส่วนต่างราคา 0.2% ต่อข้าง",
            "• ราคาเป้าหมายเป็นแผน ไม่ใช่การคาดการณ์ว่าจะไปถึง",
            "• Stop ไม่รับประกันราคาขายหรือวงเงินขาดทุนสูงสุด",
            "• ตรวจราคาจริงและช่วงราคาที่ส่งคำสั่งได้กับโบรกเกอร์ก่อนใช้งาน",
        ])
        return "\n".join(lines)

    def buy_today(self, portfolio):
        symbols = list(dict.fromkeys(
            self.universe + portfolio.get("watchlist", [])
        ))
        return self._entry_report(
            portfolio, symbols, "หุ้นน่าพิจารณา — แผนซื้อใหม่", 5
        )

    def swing_report(self, portfolio, symbols, title, limit=10):
        return self._entry_report(portfolio, symbols, title, limit)

    def _holding_decision(self, context, symbol, holding):
        snapshot = context["snapshots"].get(symbol)
        if (
            snapshot is None
            or snapshot["date"] != context["asof"]
            or context["asof"] is None
            or (context["now"].date() - context["asof"]).days
            > self.max_data_age_days
        ):
            return "DATA", "ข้อมูลราคาไม่พร้อม จึงยังสรุปว่าควรถือหรือขายไม่ได้"

        stop = number(holding.get("stop_loss"))
        target = number(holding.get("target_price"))
        price = snapshot["price"]

        if stop <= 0:
            return "DATA", "ยังไม่มีจุด Stop ที่ใช้ได้ในพอร์ต"

        if price <= stop:
            return (
                "STOP",
                f"เข้าเงื่อนไขตัดขาดทุน: ราคาอ้างอิง {price:.2f} "
                f"ไม่สูงกว่า Stop เดิม {stop:.2f} บาท",
            )

        if target > 0 and price >= target:
            return (
                "TARGET",
                f"ถึงเป้าขายที่บันทึกไว้ {target:.2f} บาท "
                "ควรตรวจคำสั่งขายตามแผน",
            )

        if (
            snapshot["weekly_up"] is False
            and price < snapshot["ema50"]
            and snapshot["macd_hist"] < 0
        ):
            return (
                "REVIEW",
                "พิจารณาลดหรือปิดสถานะ: แนวโน้มสัปดาห์ไม่ผ่าน "
                "ราคาอยู่ต่ำกว่า EMA50 และ MACD ติดลบ",
            )

        if snapshot["weekly_up"] is None:
            return (
                "DATA",
                "ยังไม่ถึง Stop/เป้าที่บันทึกไว้ "
                "แต่ข้อมูลรายสัปดาห์ไม่พอประเมินแนวโน้ม",
            )

        target_text = f"{target:.2f}" if target > 0 else "ยังไม่ได้กำหนด"
        return (
            "HOLD",
            f"ยังไม่เข้าเงื่อนไขขายที่ตรวจในโมดูลนี้ "
            f"ติดตาม Stop เดิม {stop:.2f} บาท | เป้าที่บันทึก {target_text}",
        )

    def single_report(self, portfolio, symbol):
        symbol = str(symbol).upper().strip()
        if not symbol.endswith(".BK") and not symbol.startswith("^"):
            symbol += ".BK"

        holdings = portfolio.get("stocks", {})
        context = self._context([symbol] + list(holdings))
        lines = self._header(
            context, f"วิเคราะห์ {symbol_name(symbol)}"
        )

        if symbol in holdings:
            holding = holdings[symbol]
            _, decision = self._holding_decision(context, symbol, holding)
            lines.extend([
                "",
                "สถานะ: มีหุ้นอยู่ในพอร์ต",
                f"จำนวน: {int(number(holding.get('shares')))} หุ้น",
                f"ต้นทุนเฉลี่ย: {number(holding.get('average_cost')):.2f} บาท",
                "",
                "ข้อสรุปสำหรับหุ้นที่ถืออยู่",
                decision,
                "",
                "ใช้ Stop/เป้าจากพอร์ต ไม่ย้ายเป้าตามผลสแกนใหม่",
                "เป็นการตรวจจากข้อมูลรายวัน ไม่ใช่ระบบขายอัตโนมัติ",
            ])
            return "\n".join(lines)

        return self._entry_report(
            portfolio, [symbol], f"วิเคราะห์ {symbol_name(symbol)} — ยังไม่มีในพอร์ต", 1
        )

    def sell_alerts(self, portfolio, frames=None):
        holdings = portfolio.get("stocks", {})
        if not holdings:
            return ""

        # ไม่ใช้ frames ที่มีเฉพาะหุ้นในพอร์ตสร้าง Market Breadth
        context = self._context(list(holdings))
        lines = []

        for symbol, holding in holdings.items():
            action, reason = self._holding_decision(
                context, symbol, holding
            )
            if action == "HOLD":
                continue

            lines.extend([
                f"{symbol_name(symbol)} — {int(number(holding.get('shares')))} หุ้น",
                reason,
                "",
            ])

        if not lines:
            return ""

        return "\n".join([
            f"ตรวจพอร์ตจากข้อมูลวันที่ {context['asof'] or 'ไม่ทราบ'}",
            "เป็นเงื่อนไขแจ้งเตือน ไม่ใช่คำสั่งขายที่ดำเนินการแล้ว",
            "",
        ] + lines)

    def portfolio_report(self, user_id, update_trailing=False):
        portfolio = self.get_portfolio(user_id)
        holdings = portfolio.get("stocks", {})
        context = self._context(list(holdings))
        state, issues = self._portfolio_state(portfolio, context)

        lines = [
            "สรุปพอร์ต",
            f"ข้อมูลราคา: {context['asof'] or 'ไม่พร้อมใช้'}",
            f"เงินสด: {number(portfolio.get('cash')):,.2f} บาท",
        ]

        if state is not None:
            lines.append(f"มูลค่ารวมประมาณ: {state['equity']:,.2f} บาท")
        else:
            lines.append("ยังไม่แสดงมูลค่ารวม เพราะข้อมูลบางส่วนไม่พร้อม")
            lines.extend(f"• {issue}" for issue in issues)

        if not holdings:
            lines.append("ยังไม่มีหุ้นในพอร์ต")

        for symbol, holding in holdings.items():
            _, reason = self._holding_decision(context, symbol, holding)
            lines.extend([
                "",
                f"{symbol_name(symbol)} — {int(number(holding.get('shares')))} หุ้น",
                f"ต้นทุนเฉลี่ย: {number(holding.get('average_cost')):.2f} บาท",
                reason,
            ])

        if update_trailing:
            lines.extend([
                "",
                "หมายเหตุ: รุ่นปรับรายงานนี้ยังไม่เลื่อน Stop ที่บันทึกไว้",
                "การเพิ่ม Trailing Stop ต้องทดสอบการบันทึกข้อมูลแยกต่างหาก",
            ])

        return "\n".join(lines)

    def plan_report(self, portfolio, frames=None):
        context = self._context(list(portfolio.get("stocks", {})))
        state, issues = self._portfolio_state(portfolio, context)

        if state is None:
            return "\n".join(
                ["แผนการเงิน: ยังประเมินมูลค่าพอร์ตไม่ได้"]
                + [f"• {issue}" for issue in issues]
            )

        target = number(
            portfolio.get("financial_plan", {}).get("target_value")
        )
        equity = state["equity"]

        lines = [
            "ติดตามแผนการเงิน",
            f"มูลค่าพอร์ตประมาณ: {equity:,.2f} บาท",
            f"เป้าหมายที่ตั้งไว้: {target:,.2f} บาท",
            f"ส่วนต่างถึงเป้าหมาย: {max(0.0, target - equity):,.2f} บาท",
            "",
            "ยังไม่ประมาณจำนวนครั้งที่ต้องเทรด",
            "เพราะต้องใช้สถิติจริงหลังค่าใช้จ่ายและรวมการขายบางส่วนเป็นรอบเดียว",
            "เงินฝากเพิ่มต้องแยกจากกำไรลงทุนก่อนคำนวณผลตอบแทน",
            "เป้าหมายที่ตั้งไว้ไม่ใช่ผลตอบแทนที่ระบบรับประกัน",
        ]
        return "\n".join(lines)

    def daily_plan(self, portfolio):
        sections = [self.buy_today(portfolio)]
        alerts = self.sell_alerts(portfolio)

        if alerts:
            sections.append("รายการในพอร์ตที่ต้องตรวจ\n\n" + alerts)
        else:
            sections.append(
                "การตรวจพอร์ต: ไม่พบรายการแจ้งเตือนจากเงื่อนไขที่ตรวจในรอบนี้"
            )

        sections.append(
            "ดูรายละเอียดเพิ่มเติม: พิมพ์ ดูพอร์ต หรือ แผนการเงิน"
        )
        return "\n\n--------------------\n\n".join(sections)
