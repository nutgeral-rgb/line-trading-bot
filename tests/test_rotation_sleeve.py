"""Rotation sleeve ledger, safety limits, commands and plan."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import bot
import rotation_sleeve as rs


def base():
    return {"cash": 12000.0, "stocks": {}, "history": []}


class LedgerTests(unittest.TestCase):
    def test_transfer_moves_cash_and_rejects_overdraw(self):
        p = base()
        rs.transfer_in(p, 12000, "t")
        self.assertEqual((p["cash"], p["rotation_sleeve"]["cash"], p["rotation_sleeve"]["net_in"]), (0, 12000, 12000))
        with self.assertRaises(ValueError):
            rs.transfer_in(p, 1, "t")
        rs.transfer_out(p, 2000, "t")
        self.assertEqual((p["cash"], p["rotation_sleeve"]["cash"], p["rotation_sleeve"]["net_in"]), (2000, 10000, 10000))
        with self.assertRaises(ValueError):
            rs.transfer_out(p, 10001, "t")
        for bad in (0, -5, float("nan"), True):
            with self.assertRaises(ValueError):
                rs.transfer_in(base(), bad, "t")

    def test_buy_sell_cost_basis_fees_and_pnl(self):
        p = base()
        rs.transfer_in(p, 12000, "t")
        buy = rs.record_buy(p, "ABC.BK", 100, 50.0, 20.0, "2026-11-17", "t", 0.004)
        self.assertEqual(buy["cash"], 12000 - 5020)
        self.assertAlmostEqual(p["rotation_sleeve"]["positions"]["ABC.BK"]["avg_price"], 50.2)
        with self.assertRaises(ValueError):
            rs.record_buy(p, "XYZ.BK", 1000, 50.0, None, "2026-11-17", "t", 0.004)
        self.assertEqual(p["rotation_sleeve"]["cash"], 6980)  # failed buy changed nothing
        sell = rs.record_sell(p, "ABC.BK", 100, 55.0, 22.0, "t", 0.004)
        self.assertAlmostEqual(sell["pnl"], 5500 - 22 - 5020)
        self.assertEqual(p["rotation_sleeve"]["positions"], {})
        with self.assertRaises(ValueError):
            rs.record_sell(p, "ABC.BK", 1, 55.0, None, "t", 0.004)

    def test_cap_warning_and_halt_threshold(self):
        p = base()
        rs.transfer_in(p, 12000, "t")
        rs.record_buy(p, "A.BK", 100, 10.0, 0, "d", "t", 0.004)
        rs.record_buy(p, "B.BK", 100, 10.0, 0, "d", "t", 0.004)
        self.assertTrue(rs.record_buy(p, "C.BK", 100, 10.0, 0, "d", "t", 0.004)["over_cap"])
        sleeve = p["rotation_sleeve"]
        self.assertIsNone(rs.equity(sleeve, {"A.BK": 10.0}))  # missing price -> unknown
        self.assertFalse(rs.is_halted(sleeve, 10800.5))
        self.assertTrue(rs.is_halted(sleeve, 10800.0))
        self.assertFalse(rs.is_halted(sleeve, None))

    def test_validate_roundtrip_and_rejects_garbage(self):
        p = base()
        rs.transfer_in(p, 12000, "t")
        rs.record_buy(p, "A.BK", 100, 10.0, 0, "2026-11-17", "t", 0.004)
        self.assertEqual(rs.validate_sleeve(p["rotation_sleeve"])["positions"]["A.BK"]["shares"], 100)
        for mutate in (lambda s: s.update(version=2), lambda s: s.update(cash=-1),
                       lambda s: s.update(risk_per_trade=0.5),
                       lambda s: s["positions"]["A.BK"].update(shares=0)):
            broken = rs.validate_sleeve(p["rotation_sleeve"])
            mutate(broken)
            with self.assertRaises(ValueError):
                rs.validate_sleeve(broken)


class CommandTests(unittest.TestCase):
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
        bot.command_reply(self.user, "ตั้งเงินสด 12000")

    def test_commands_keep_sleeve_separate_from_main_portfolio(self):
        self.assertIn("โอนเข้าถุงหมุนเวียน 12,000.00", bot.command_reply(self.user, "ตั้งถุงหมุนเวียน 12000"))
        self.assertIn("มีถุงหมุนเวียนอยู่แล้ว", bot.command_reply(self.user, "ตั้งถุงหมุนเวียน 500"))
        self.assertIn("ไม่พอ", bot.command_reply(self.user, "โอนเข้าถุงหมุนเวียน 1"))
        self.assertIn("บันทึกซื้อในถุง", bot.command_reply(self.user, "ซื้อหมุนเวียน ABC 100 12.5"))
        p = bot.get_portfolio(self.user)
        self.assertEqual(p["stocks"], {})  # long-term holdings untouched
        self.assertEqual(p["cash"], 0)
        self.assertEqual(p["rotation_sleeve"]["positions"]["ABC.BK"]["shares"], 100)
        self.assertIn("ถุงหมุนเวียน (แยกบัญชี", bot.command_reply(self.user, "ดูพอร์ต"))
        self.assertIn("กำไร/ขาดทุนตามต้นทุน", bot.command_reply(self.user, "ขายหมุนเวียน ABC 100 13"))
        self.assertIn("ไม่พอ", bot.command_reply(self.user, "ซื้อหมุนเวียน ABC 100000 12.5"))

    def test_commands_need_a_sleeve_and_valid_numbers(self):
        self.assertIn("ยังไม่ได้ตั้งถุง", bot.command_reply(self.user, "ดูถุงหมุนเวียน"))
        self.assertIn("ยังไม่ได้ตั้งถุง", bot.command_reply(self.user, "แผนหมุนเวียน"))
        self.assertIn("ยังไม่ได้ตั้งถุง", bot.command_reply(self.user, "ซื้อหมุนเวียน ABC 100 12.5"))
        self.assertIn("รูปแบบ", bot.command_reply(self.user, "ตั้งถุงหมุนเวียน"))
        self.assertIn("ตัวเลข", bot.command_reply(self.user, "ตั้งถุงหมุนเวียน abc"))

    def test_backup_roundtrip_keeps_sleeve(self):
        bot.command_reply(self.user, "ตั้งถุงหมุนเวียน 5000")
        bot.command_reply(self.user, "ซื้อหมุนเวียน ABC 100 12.5")
        restored = bot.validate_portfolio_backup(bot.export_portfolio_backup(self.user))
        self.assertEqual(restored["rotation_sleeve"]["positions"]["ABC.BK"]["shares"], 100)


class PlanTests(unittest.TestCase):
    def _frames(self, drop_held=False):
        days = pd.bdate_range(end="2026-11-17", periods=260)
        up = np.linspace(50, 80, 260)
        weak = np.concatenate([np.linspace(50, 80, 240), np.linspace(80, 60, 20)])

        def frame(px):
            return pd.DataFrame({"Open": px, "High": px * 1.01, "Low": px * .99, "Close": px,
                                 "Adj Close": px, "Volume": [5e6] * 260}, index=days)
        frames = {s: frame(up) for s in bot.UNIVERSE}
        frames["HELD.BK"] = frame(weak)
        return frames

    def _plan(self, sleeve):
        portfolio = {"cash": 0.0, "stocks": {}, "rotation_sleeve": sleeve}
        passing = {"summary": "ผ่าน", "score": 5, "available": 5, "required": 5, "checks": {},
                   "valuation": {}, "quality_warnings": []}
        with patch.object(bot, "get_market_frames", return_value=self._frames()), \
                patch.object(bot, "set100_universe_warning", return_value=None), \
                patch.object(bot, "analyze_fundamentals", return_value=passing), \
                patch.object(bot, "now_bkk", return_value=bot.datetime(2026, 11, 17, 11, 0)):
            return bot.build_rotation_plan(portfolio)

    def _sleeve(self, positions, net_in=12000.0, cash=12000.0):
        s = rs.new_sleeve()
        s.update(cash=cash, net_in=net_in, positions=positions)
        return s

    def test_sell_flags_for_weak_position_and_blocks_new_entries_when_full(self):
        held = {"HELD.BK": {"shares": 100, "avg_price": 80.0, "opened": "2026-10-01"},
                "OTHER.BK": {"shares": 10, "avg_price": 10.0, "opened": "2026-11-10"}}
        text = self._plan(self._sleeve(held, cash=100.0))
        self.assertIn("ไม่มีราคาล่าสุดของ: OTHER", text)
        self.assertIn("HELD: ⚠️ พิจารณาขาย", text)
        self.assertIn("ปิดต่ำกว่า SMA20", text)
        self.assertIn("ถุงเต็ม 2/2", text)
        self.assertNotIn("✅ หุ้นที่ผ่านเกณฑ์", text)

    def test_halt_after_ten_percent_fall_blocks_new_entries(self):
        held = {"HELD.BK": {"shares": 100, "avg_price": 80.0, "opened": "2026-11-10"}}
        text = self._plan(self._sleeve(held, net_in=20000.0, cash=100.0))  # equity ~6k vs 20k in
        self.assertIn("⛔", text)
        self.assertIn("งดเสนอหุ้นใหม่", text)
        self.assertNotIn("✅ หุ้นที่ผ่านเกณฑ์", text)

    def test_empty_sleeve_suggests_candidates_with_sleeve_risk_limit(self):
        text = self._plan(self._sleeve({}))
        self.assertIn("แผนถุงหมุนเวียน", text)
        self.assertIn("✅ หุ้นที่ผ่านเกณฑ์ (2 ตัว)", text)  # max_positions=2 from the sleeve
        self.assertIn("Stop ประมาณ 4%", text)


if __name__ == "__main__":
    unittest.main()
