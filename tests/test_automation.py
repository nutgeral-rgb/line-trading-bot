"""Reminders, one-tap contribution confirmation, dividend calendar, summaries."""
from datetime import date
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import automation
import bot


def portfolio(**extra):
    base = {"cash": 100.0, "stocks": {}, "history": [],
            "investment_strategy": {"style": "LONG_TERM", "monthly_contribution_baht": 5000}}
    base.update(extra)
    return base


class AutomationLogicTests(unittest.TestCase):
    def test_reminder_waits_for_day_and_stops_when_paid(self):
        p = portfolio()
        self.assertIsNone(automation.contribution_reminder_due(p, date(2026, 11, 24)))
        self.assertEqual(automation.contribution_reminder_due(p, date(2026, 11, 25))["remaining"], 5000)
        p["history"].append({"type": "MONTHLY_CONTRIBUTION", "amount": 2000, "month": "2026-11"})
        self.assertEqual(automation.contribution_reminder_due(p, date(2026, 11, 26))["remaining"], 3000)
        p["history"].append({"type": "MONTHLY_CONTRIBUTION", "amount": 3000, "month": "2026-11"})
        self.assertIsNone(automation.contribution_reminder_due(p, date(2026, 11, 26)))

    def test_confirm_records_only_remainder_once(self):
        p = portfolio()
        p["history"].append({"type": "MONTHLY_CONTRIBUTION", "amount": 2000, "month": "2026-11"})
        first = automation.confirm_planned_contribution(p, date(2026, 11, 25), "t")
        self.assertTrue(first["ok"])
        self.assertEqual(first["amount"], 3000)
        self.assertEqual(p["cash"], 3100)
        second = automation.confirm_planned_contribution(p, date(2026, 11, 25), "t")
        self.assertFalse(second["ok"])
        self.assertEqual(p["cash"], 3100)
        self.assertEqual(automation.confirm_planned_contribution(
            portfolio(investment_strategy={"style": "LONG_TERM"}), date(2026, 11, 25), "t")["reason"], "NO_PLAN")

    def test_no_reminder_without_long_term_plan_or_after_sent(self):
        self.assertIsNone(automation.contribution_reminder_due(
            portfolio(investment_strategy={"monthly_contribution_baht": 5000}), date(2026, 11, 28)))
        p = portfolio(contribution_reminder={"month": "2026-11", "status": "SENT"})
        self.assertIsNone(automation.contribution_reminder_due(p, date(2026, 11, 28)))
        p["contribution_reminder"]["status"] = "FAILED"
        self.assertIsNotNone(automation.contribution_reminder_due(p, date(2026, 11, 28)))

    def test_upcoming_dividends_window_and_estimates(self):
        holdings = {"KBANK.BK": {"shares": 10}, "AOT.BK": {"shares": 100}, "MBK.BK": {"shares": 300}}
        events = {"KBANK.BK": {"ex_date": date(2026, 11, 20), "amount": 2.0},
                  "AOT.BK": {"ex_date": date(2026, 12, 30), "amount": 0.5},
                  "MBK.BK": {"ex_date": date(2026, 11, 16), "amount": None}}
        rows = automation.upcoming_dividends(holdings, events, date(2026, 11, 16))
        self.assertEqual([r["symbol"] for r in rows], ["MBK.BK", "KBANK.BK"])
        self.assertIsNone(rows[0]["gross"])
        self.assertEqual((rows[1]["gross"], rows[1]["net"]), (20.0, 18.0))
        text = automation.format_dividend_calendar(rows)
        self.assertIn("ยังไม่ทราบอัตรา", text)
        self.assertIn("KBANK XD 2026-11-20", text)
        self.assertIn("ไม่พบ", automation.format_dividend_calendar([]))

    def test_estimate_projects_last_year_and_skips_paid_or_new(self):
        today = date(2026, 11, 16)
        history = [(date(2025, 11, 20), 2.0), (date(2025, 5, 5), 1.5)]
        event = automation.estimate_next_event(history, today)
        self.assertEqual((event["ex_date"], event["amount"], event["estimated"]),
                         (date(2026, 11, 20), 2.0, True))
        # Out of window, and nothing projected from a payment too recent.
        self.assertIsNone(automation.estimate_next_event([(date(2025, 5, 5), 1.5)], today))
        self.assertIsNone(automation.estimate_next_event([], today))
        # Already paid near the projected date this year -> do not repeat it.
        paid = history + [(date(2026, 11, 10), 2.0)]
        self.assertIsNone(automation.estimate_next_event(paid, today))
        self.assertIn("ประมาณการจากปีก่อน", automation.format_dividend_calendar(
            automation.upcoming_dividends({"KBANK.BK": {"shares": 10}}, {"KBANK.BK": event}, today)))

    def test_monthly_summary_ignores_reversed_and_other_months(self):
        p = portfolio(dividend_goal={"monthly_net": 10000})
        p["history"] = [
            {"type": "MONTHLY_CONTRIBUTION", "amount": 3000, "month": "2026-10"},
            {"type": "MONTHLY_CONTRIBUTION", "amount": 5000, "month": "2026-11"},
            {"type": "DIVIDEND", "amount": 90, "paid_date": "2026-10-05", "reference": "a"},
            {"type": "DIVIDEND", "amount": 50, "paid_date": "2026-10-06", "reference": "b"},
            {"type": "DISTRIBUTION_REVERSAL", "reference": "b"},
            {"type": "DIVIDEND", "amount": 70, "paid_date": "2026-11-05", "reference": "c"},
            {"type": "CAPITAL_RETURN", "amount": 78, "paid_date": "2026-10-27", "reference": "d"},
            {"type": "RECORDED_BUY", "recorded_at": "2026-10-10T00:00:00"},
        ]
        s = automation.monthly_summary(p, date(2026, 10, 1))
        self.assertEqual((s["contributions"], s["shortfall"], s["dividends"], s["capital_returns"], s["buys"]),
                         (3000, 2000, 90, 78, 1))
        text = automation.format_monthly_summary(s)
        self.assertIn("ขาด 2,000", text)
        self.assertIn("ไม่นับเป็นปันผล", text)


class BotAutomationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for name, value in (("PORTFOLIO_FILE", Path(self.directory.name) / "p.json"),
                            ("DATABASE_URL", ""), ("ON_RENDER", False)):
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.user = "u1"
        bot.command_reply(self.user, "ตั้งแผนลงทุนระยะยาว")
        bot.command_reply(self.user, "ตั้งเงินเติมรายเดือน 5000")

    def test_confirm_command_is_idempotent(self):
        first = bot.command_reply(self.user, "เติมเงินตามแผน")
        self.assertIn("5,000.00", first)
        self.assertIn("ไม่บันทึกซ้ำ", bot.command_reply(self.user, "เติมเงินตามแผน"))
        self.assertEqual(bot.get_portfolio(self.user)["cash"], 5000)

    def test_reminder_sent_once_per_month_with_button(self):
        pushed = []
        with patch.object(bot, "line_bot_api") as api, \
                patch.object(bot, "now_bkk", return_value=bot.datetime(2026, 11, 26, 9, 10, tzinfo=bot.timezone.utc)):
            api.push_message.side_effect = lambda uid, msg: pushed.append(msg)
            bot.send_contribution_reminders()
            bot.send_contribution_reminders()
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0].quick_reply.items[0].action.text, "เติมเงินตามแผน")
        self.assertEqual(bot.get_portfolio(self.user)["contribution_reminder"]["status"], "SENT")

    def test_failed_push_is_retried_next_run(self):
        with patch.object(bot, "line_bot_api") as api, \
                patch.object(bot, "now_bkk", return_value=bot.datetime(2026, 11, 26, 9, 10, tzinfo=bot.timezone.utc)):
            api.push_message.side_effect = RuntimeError("down")
            bot.send_contribution_reminders()
            self.assertEqual(bot.get_portfolio(self.user)["contribution_reminder"]["status"], "FAILED")
            api.push_message.side_effect = None
            bot.send_contribution_reminders()
        self.assertEqual(bot.get_portfolio(self.user)["contribution_reminder"]["status"], "SENT")

    def test_reminder_day_command_validation(self):
        self.assertIn("1–28", bot.command_reply(self.user, "ตั้งวันเตือนเติมเงิน 31"))
        self.assertIn("วันที่ 10", bot.command_reply(self.user, "ตั้งวันเตือนเติมเงิน 10"))

    def test_dividend_calendar_uses_fetched_events_and_alerts_once(self):
        bot.command_reply(self.user, "ตั้งหุ้น KBANK 10 134.23")
        today = bot.now_bkk().date()
        event = {"ex_date": today + bot.timedelta(days=3), "amount": 2.0}
        with patch.object(bot, "fetch_dividend_event", side_effect=RuntimeError("blocked")):
            failed = bot.command_reply(self.user, "ปฏิทินปันผล")
            self.assertIn("ดึงข้อมูลปฏิทินปันผลไม่สำเร็จ", failed)
            self.assertNotIn("ไม่พบหุ้น", failed)
        with patch.object(bot, "fetch_dividend_event", return_value=event):
            self.assertIn("KBANK XD", bot.command_reply(self.user, "ปฏิทินปันผล"))
            with patch.object(bot, "line_bot_api") as api:
                bot.send_dividend_alerts()
                bot.send_dividend_alerts()
                self.assertEqual(api.push_message.call_count, 1)

    def test_fetch_dividend_history_parses_chart_and_caches(self):
        payload = {"chart": {"result": [{"events": {"dividends": {
            "1": {"amount": 2.0, "date": 1763600000}, "2": {"amount": 0, "date": 1700000000}}}}]}}

        class Response:
            def raise_for_status(self): pass
            def json(self): return payload
        bot._dividend_cache.clear()
        with patch.object(bot.requests, "get", return_value=Response()) as get:
            first = bot.fetch_dividend_history("KBANK.BK")
            second = bot.fetch_dividend_history("KBANK.BK")
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0][1], 2.0)
        self.assertEqual(first, second)
        self.assertEqual(get.call_count, 1)
        bot._dividend_cache.clear()
        with patch.object(bot.requests, "get", side_effect=bot.requests.ConnectionError("x")):
            with self.assertRaises(bot.requests.ConnectionError):
                bot.fetch_dividend_history("AOT.BK")

    def test_valuation_line_warns_when_missing_and_flags_derived(self):
        missing = bot.format_valuation_line({"valuation": {"trailing_pe": None, "price_to_book": None}})
        self.assertIn("ไม่ได้ตรวจ P/E และ P/B", missing)
        derived = bot.format_valuation_line({"valuation": {
            "trailing_pe": 12.5, "price_to_book": 2.5, "source": "คำนวณจากงบและราคา"}})
        self.assertIn("P/E 12.5 | P/B 2.50", derived)
        self.assertIn("คำนวณเอง", derived)
        self.assertNotIn("คำนวณเอง", bot.format_valuation_line(
            {"valuation": {"trailing_pe": 9.0, "price_to_book": None, "source": "Yahoo"}}))

    def test_warrant_or_suspended_holdings_do_not_block_recommendations(self):
        self.assertTrue(bot.is_unvaluable_holding("BTS-W8.BK", {"shares": 60}))
        self.assertTrue(bot.is_unvaluable_holding("ABC.BK", {"status": "SP"}))
        self.assertTrue(bot.is_unvaluable_holding("ABC.BK", {"status": "DELISTED"}))
        self.assertFalse(bot.is_unvaluable_holding("QHBREIT.BK", {"shares": 600}))
        self.assertFalse(bot.is_unvaluable_holding("AOT.BK", {"shares": 100, "status": "ACTIVE"}))
        self.assertTrue(bot.is_unvaluable_holding(
            "XYZ.BK", {"instrument_type": "WARRANT", "shares": 1}))

    def _plan_for(self, banpu_last_day, banpu_shares=38):
        import numpy as np
        import pandas as pd
        days = pd.bdate_range(end="2026-11-17", periods=260)  # last bar is today's partial one

        def frame(last_day):
            idx = days[days <= pd.Timestamp(last_day)]
            px = np.linspace(50, 80, len(idx))
            return pd.DataFrame({"Open": px, "High": px * 1.01, "Low": px * .99, "Close": px,
                                 "Adj Close": px, "Volume": [5e6] * len(idx)}, index=idx)
        frames = {s: frame("2026-11-17") for s in bot.UNIVERSE}
        frames["BANPU.BK"] = frame(banpu_last_day)
        portfolio = {"cash": 6000.0, "investment_strategy": {"style": "LONG_TERM"},
                     "stocks": {"AOT.BK": {"shares": 100, "avg_price": 71.12},
                                "BDMS.BK": {"shares": 400, "avg_price": 18.53},
                                "BANPU.BK": {"shares": banpu_shares, "avg_price": 28.47},
                                "BTS-W8.BK": {"shares": 60, "avg_price": 0.0}}}
        empty = {"score": None, "available": 0, "required": 5, "checks": {}, "valuation": {},
                 "valuation_concern": None, "quality_warnings": [], "quality_penalty": 0,
                 "sector": None, "summary": ""}
        with patch.object(bot, "get_market_frames", return_value=frames), \
                patch.object(bot, "set100_universe_warning", return_value=None), \
                patch.object(bot, "analyze_fundamentals", return_value=empty), \
                patch.object(bot, "now_bkk", return_value=bot.datetime(2026, 11, 17, 11, 21)):
            return bot.build_long_term_accumulation_plan(portfolio, [])

    def test_midday_request_ignores_unfinished_bar_and_tolerates_warrant_and_lag(self):
        text = self._plan_for("2026-11-13")
        self.assertNotIn("เก่าเกิน", text)
        self.assertNotIn("ประเมินหุ้นในพอร์ตได้ไม่ครบ", text)
        self.assertIn("ราคาปิด 2026-11-16", text)
        self.assertIn("ไม่นับในมูลค่าพอร์ต", text)
        self.assertIn("BANPU.BK (2026-11-13)", text)

    def test_position_cap_is_named_instead_of_blaming_budget(self):
        import numpy as np
        import pandas as pd
        days = pd.bdate_range(end="2026-11-16", periods=260)
        px = np.concatenate([np.linspace(50, 80, 235), np.linspace(80.0, 80.5, 25)])
        flat = pd.DataFrame({"Open": px, "High": px * 1.01, "Low": px * .99, "Close": px,
                             "Adj Close": px, "Volume": [5e6] * 260}, index=days)
        held = [f"ZZ{i}.BK" for i in range(6)]  # six holdings outside SET100 -> cap reached
        frames = {s: flat for s in list(bot.UNIVERSE) + held}
        portfolio = {"cash": 6000.0, "investment_strategy": {"style": "LONG_TERM"},
                     "stocks": {s: {"shares": 100, "avg_price": 70.0} for s in held}}
        passing = {"score": 5, "available": 5, "required": 5,
                   "checks": {k: True for k in ("earnings", "revenue_growth", "operating_cashflow",
                                                "leverage", "financial_freshness")},
                   "valuation": {}, "valuation_concern": None, "quality_warnings": [],
                   "quality_penalty": 0, "sector": None, "summary": "", "statement_age_days": 30}
        with patch.object(bot, "get_market_frames", return_value=frames), \
                patch.object(bot, "set100_universe_warning", return_value=None), \
                patch.object(bot, "analyze_fundamentals", return_value=passing), \
                patch.object(bot, "now_bkk", return_value=bot.datetime(2026, 11, 17, 9, 0)):
            text = bot.build_long_term_accumulation_plan(portfolio, [])
        self.assertIn("ถือหุ้น 6 ตัว เกินเพดาน 5 ตัว", text)
        self.assertIn("ถือครบ 5 ตัวแล้ว", text)
        self.assertNotIn("งบ/ล็อตยังไม่พอ", text)

    def test_small_stale_holding_is_excluded_with_a_warning(self):
        text = self._plan_for("2026-08-11")  # ~4% of cost, like BANPU
        self.assertNotIn("ประเมินหุ้นในพอร์ตได้ไม่ครบ", text)
        self.assertIn("ไม่นับในมูลค่าพอร์ต (ไม่มีราคาที่ใช้ได้", text)
        self.assertIn("BANPU.BK (ราคาล่าสุด 2026-08-11)", text)
        self.assertIn("🔎 ผ่านแนวโน้ม", text)

    def test_large_stale_holding_still_blocks_and_names_its_date(self):
        text = self._plan_for("2026-08-11", banpu_shares=2000)
        self.assertIn("ประเมินหุ้นในพอร์ตได้ไม่ครบ: BANPU.BK (ราคาล่าสุด 2026-08-11)", text)
        self.assertNotIn("🔎 ผ่านแนวโน้ม", text)

    def test_monthly_summary_sent_once_for_previous_month(self):
        with patch.object(bot, "line_bot_api") as api, \
                patch.object(bot, "now_bkk", return_value=bot.datetime(2026, 12, 2, 9, 25, tzinfo=bot.timezone.utc)):
            bot.send_monthly_summaries()
            bot.send_monthly_summaries()
            self.assertEqual(api.push_message.call_count, 1)
            self.assertIn("2026-11", api.push_message.call_args[0][1].text)


if __name__ == "__main__":
    unittest.main()
