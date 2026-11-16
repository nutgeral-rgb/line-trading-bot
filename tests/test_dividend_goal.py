import copy
from datetime import date
import unittest

from dividend_goal import (create_goal, project_goal, record_distribution,
                           reverse_distribution, income_summary)


class GoalTests(unittest.TestCase):
    def test_known_ten_year_example_and_fixed_deadline(self):
        goal = create_goal(15000, 10, date(2026, 11, 15))
        result = project_goal(goal, 40816.8, 1000, date(2026, 11, 15))
        self.assertEqual(result['months'], 120)
        central = result['scenarios'][1]
        self.assertAlmostEqual(central['projected'], 235570.114688, places=4)
        self.assertAlmostEqual(central['required_monthly'], 27246.9350165, places=4)
        self.assertEqual(result['capital_targets'], [6000000, 4500000, 3600000])
        self.assertEqual(project_goal(goal, 40816.8, 1000, date(2027, 11, 15))['months'], 108)
        self.assertEqual(create_goal(15000, 10, date(2027, 1, 1), goal), goal)

    def test_expired_and_missing_capital(self):
        goal = create_goal(15000, 1, date(2026, 11, 15))
        expired = project_goal(goal, 1000, 1000, date(2027, 11, 15))
        self.assertEqual(expired['months'], 0)
        self.assertIsNone(expired['scenarios'][0]['required_monthly'])
        self.assertEqual(project_goal(goal, None, 1000, date(2026, 11, 15))['scenarios'], [])

    def test_leap_date_and_invalid_inputs(self):
        self.assertEqual(create_goal(15000, 1, date(2024, 2, 29))['target_date'], '2025-02-28')
        for bad in (float('nan'), float('inf'), -1, True, 0):
            with self.assertRaises(ValueError):
                create_goal(bad, 10, date(2026, 11, 15))
        with self.assertRaises(ValueError):
            create_goal(15000, 1.5, date(2026, 11, 15))


class IncomeTests(unittest.TestCase):
    def setUp(self):
        self.p = {'cash': 12000., 'history': []}
        self.today = date(2026, 11, 15)

    def test_net_cash_duplicate_reference_and_capital_return(self):
        args = ('AOT.BK', 100, 10, '2026-11-01', 'receipt1', 'DIVIDEND', self.today)
        self.assertTrue(record_distribution(self.p, *args))
        self.assertFalse(record_distribution(self.p, *args))
        self.assertEqual(self.p['cash'], 12090)
        record_distribution(self.p, 'QHBREIT.BK', 78, 0, '2026-11-01', 'receipt2', 'CAPITAL_RETURN', self.today)
        summary = income_summary(self.p, self.today)
        self.assertEqual(summary['net_dividends'], 90)
        self.assertEqual(summary['capital_returns'], 78)
        self.assertEqual(summary['monthly_average'], 7.5)
        self.assertEqual(self.p['cash'], 12168)
        with self.assertRaises(ValueError):
            record_distribution(self.p, 'AOT.BK', 200, 10, '2026-11-01', 'receipt1', 'DIVIDEND', self.today)

    def test_reversal_once_and_validation_before_mutation(self):
        record_distribution(self.p, 'AOT.BK', 100, 10, '2026-11-01', 'r1', 'DIVIDEND', self.today)
        self.assertTrue(reverse_distribution(self.p, 'r1', self.today))
        self.assertFalse(reverse_distribution(self.p, 'r1', self.today))
        self.assertEqual(self.p['cash'], 12000)
        self.assertEqual(income_summary(self.p, self.today)['net_dividends'], 0)
        before = copy.deepcopy(self.p)
        for gross, tax, day in [(10, 11, '2026-11-01'), (100, 0, '2026-11-16'), (float('nan'), 0, '2026-11-01')]:
            with self.assertRaises(ValueError):
                record_distribution(self.p, 'AOT.BK', gross, tax, day, 'r2', 'DIVIDEND', self.today)
            self.assertEqual(self.p, before)

    def test_old_payments_and_reversal_insufficient_cash(self):
        record_distribution(self.p, 'AOT.BK', 100, 10, '2025-01-01', 'old', 'DIVIDEND', self.today)
        self.assertEqual(income_summary(self.p, self.today)['net_dividends'], 0)
        self.p['cash'] = 1
        before = copy.deepcopy(self.p)
        with self.assertRaises(ValueError):
            reverse_distribution(self.p, 'old', self.today)
        self.assertEqual(self.p, before)
