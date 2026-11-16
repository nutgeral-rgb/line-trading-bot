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

    def test_monthly_summary_sent_once_for_previous_month(self):
        with patch.object(bot, "line_bot_api") as api, \
                patch.object(bot, "now_bkk", return_value=bot.datetime(2026, 12, 2, 9, 25, tzinfo=bot.timezone.utc)):
            bot.send_monthly_summaries()
            bot.send_monthly_summaries()
            self.assertEqual(api.push_message.call_count, 1)
            self.assertIn("2026-11", api.push_message.call_args[0][1].text)


if __name__ == "__main__":
    unittest.main()
