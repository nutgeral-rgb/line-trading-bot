"""Portfolio ledger commands must match the user's actual LINE message form."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


class PortfolioCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for name, value in (
            ("PORTFOLIO_FILE", Path(self.directory.name) / "portfolio.json"),
            ("DATABASE_URL", ""),
            ("ON_RENDER", False),
        ):
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.user = "test-user"

    def test_contribution_and_trade_ledger(self):
        bot.command_reply(self.user, "ตั้งแผนลงทุนระยะยาว")
        self.assertEqual(bot.get_portfolio(self.user)["cash"], 0)
        bot.command_reply(self.user, "ตั้งเงินสด 6000")
        contribution = bot.command_reply(self.user, "บันทึกเงินเติม 500")
        self.assertIn("6,500.00", contribution)
        self.assertIn("เงินเติมที่บันทึกสะสม: 500.00 บาท", bot.command_reply(self.user, "ดูพอร์ต"))
        purchase = bot.command_reply(self.user, "บันทึกซื้อ AOT 100 35.50 10")
        self.assertIn("100 หุ้น", purchase)
        portfolio = bot.get_portfolio(self.user)
        self.assertEqual(portfolio["stocks"]["AOT.BK"]["shares"], 100)
        self.assertAlmostEqual(portfolio["stocks"]["AOT.BK"]["avg_price"], 35.6)
        self.assertAlmostEqual(portfolio["cash"], 2940)
        sale = bot.command_reply(self.user, "บันทึกขาย AOT 40 40 4")
        self.assertIn("กำไร/ขาดทุนตามต้นทุน", sale)
        portfolio = bot.get_portfolio(self.user)
        self.assertEqual(portfolio["stocks"]["AOT.BK"]["shares"], 60)
        self.assertAlmostEqual(portfolio["cash"], 4536)
        self.assertAlmostEqual(portfolio["history"][-1]["realized_pnl"], 172)
        self.assertEqual(len([row for row in portfolio["history"]
                              if row["type"] == "MONTHLY_CONTRIBUTION"]), 1)

    def test_overspending_does_not_change_portfolio(self):
        bot.command_reply(self.user, "ตั้งเงินสด 500")
        result = bot.command_reply(self.user, "บันทึกซื้อ AOT 100 35.50")
        self.assertIn("ไม่พอ", result)
        self.assertEqual(bot.get_portfolio(self.user)["cash"], 500)
        self.assertEqual(bot.get_portfolio(self.user)["stocks"], {})

    def test_render_without_database_refuses_ephemeral_portfolio(self):
        with patch.object(bot, "ON_RENDER", True):
            result = bot.command_reply(self.user, "ดูพอร์ต")
        self.assertIn("ที่เก็บพอร์ตถาวร", result)


if __name__ == "__main__":
    unittest.main()
