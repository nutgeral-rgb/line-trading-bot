"""Regression checks for incomplete evidence and concurrent screening."""
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import threading
import time
import unittest
from unittest.mock import patch

import pandas as pd
import fundamental_analysis as fa


class FundamentalEvidenceTests(unittest.TestCase):
    def test_missing_recent_quarter_does_not_fall_back_to_older_profit(self):
        frame = pd.DataFrame(
            [[10, float('nan'), 10, 10, 10]],
            index=['Net Income'],
            columns=pd.to_datetime(['2026-09-30', '2026-06-30', '2026-03-31',
                                    '2025-12-31', '2025-09-30']),
        )
        self.assertEqual(fa._latest_values(frame, ('Net Income',), 4), [])

    def test_complete_quarters_are_sorted_newest_first(self):
        frame = pd.DataFrame([[1, 3, 2]], index=['Total Revenue'],
                             columns=['2025-12-31', '2026-06-30', '2026-03-31'])
        self.assertEqual(fa._latest_values(frame, ('Total Revenue',), 3), [3, 2, 1])

    def test_future_statement_is_not_fresh_even_when_oldest_is_in_past(self):
        class Ticker:
            quarterly_income_stmt = pd.DataFrame([[1]], index=['Net Income'],
                                                 columns=['2026-09-30'])
            quarterly_cashflow = pd.DataFrame([[1]], index=['Operating Cash Flow'],
                                              columns=['2026-12-31'])
            quarterly_balance_sheet = pd.DataFrame([[1]], index=['Total Debt'],
                                                   columns=['2026-09-30'])
            info = {}
        with patch('yfinance.Ticker', return_value=Ticker()):
            result = fa._analyze_fundamentals_for_day.__wrapped__('TEST.BK', '2026-10-25')
        self.assertFalse(result['checks']['financial_freshness'])

    def test_parallel_requests_share_fetch_and_return_independent_results(self):
        calls = []
        barrier = threading.Barrier(4)

        @lru_cache(maxsize=200)
        def fetch(symbol, day):
            calls.append(symbol)
            time.sleep(0.03)
            return {'checks': {'earnings': True}}

        def request(_):
            barrier.wait(timeout=5)
            return fa.analyze_fundamentals(' test.bk ')

        with patch.object(fa, '_analyze_fundamentals_for_day', fetch):
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(request, range(4)))
            results[0]['checks']['earnings'] = False
            self.assertTrue(fa.analyze_fundamentals('TEST.BK')['checks']['earnings'])
        self.assertEqual(calls, ['TEST.BK'])
        self.assertTrue(results[1]['checks']['earnings'])
