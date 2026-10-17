"""Deterministic tests for prospective recommendation accounting."""

import unittest

import pandas as pd

from long_term_review import assess_holding, evaluate_recommendations


def prices(count=230, first="2025-01-02", price=100.0):
    index = pd.bdate_range(first, periods=count)
    return pd.DataFrame(
        {"Open": [price] * count, "Close": [price] * count,
         "Adj Close": [price] * count},
        index=index,
    )


class RecommendationAccountingTests(unittest.TestCase):
    def test_entry_is_next_session_and_costs_are_deducted(self):
        frame = prices(count=23)
        frame.iloc[1, frame.columns.get_loc("Open")] = 110.0
        frame.iloc[20, frame.columns.get_loc("Close")] = 121.0
        frame.iloc[20, frame.columns.get_loc("Adj Close")] = 121.0
        signal = [{"symbol": "AAA.BK", "signal_date": "2025-01-02",
                   "delivery_status": "SENT"}]
        observations, summary = evaluate_recommendations(
            signal, {"AAA.BK": frame}, prices(count=23), horizons=(20, 60)
        )
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["entry_date"], "2025-01-03")
        self.assertAlmostEqual(
            observations[0]["net_return"], 121 * .996 / (110 * 1.004) - 1
        )
        self.assertEqual(summary[20]["beat_benchmark_count"], 1)
        self.assertNotIn(60, summary)

    def test_unsent_and_immature_signals_are_not_scored(self):
        frame = prices(count=10)
        records = [
            {"symbol": "AAA.BK", "signal_date": "2025-01-02",
             "delivery_status": "PENDING"},
            {"symbol": "AAA.BK", "signal_date": "2025-01-03",
             "delivery_status": "SENT"},
        ]
        observations, summary = evaluate_recommendations(
            records, {"AAA.BK": frame}, horizons=(20,)
        )
        self.assertEqual(observations, [])
        self.assertEqual(summary, {})


class HoldingReviewTests(unittest.TestCase):
    def setUp(self):
        self.good = {"available": 5, "required": 5,
                     "checks": {"financial_freshness": True,
                                "earnings": True, "operating_cashflow": True,
                                "revenue_growth": True, "leverage": True}}

    def test_sell_review_needs_bad_financials_and_price_trend(self):
        frame = prices()
        frame.loc[frame.index[-60:], "Close"] = 60.0
        frame.loc[frame.index[-1], "Close"] = 55.0
        poor = {**self.good, "checks": {**self.good["checks"],
                                        "earnings": False}}
        self.assertEqual(
            assess_holding(frame, poor, 100)["status"], "พิจารณาขาย/ลด"
        )
        self.assertEqual(
            assess_holding(frame, self.good, 100)["status"], "ทบทวนใกล้ชิด"
        )

    def test_stale_or_incomplete_statements_never_trigger_sell(self):
        frame = prices()
        stale = {**self.good, "checks": {**self.good["checks"],
                                         "financial_freshness": False}}
        self.assertEqual(assess_holding(frame, stale, 100)["status"], "ข้อมูลไม่พอ")
        incomplete = {**self.good, "available": 4}
        self.assertEqual(
            assess_holding(frame, incomplete, 100)["status"], "ข้อมูลไม่พอ"
        )


if __name__ == "__main__":
    unittest.main()
