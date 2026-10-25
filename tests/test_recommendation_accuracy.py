"""Accuracy command and reporting must use delivered, completed observations."""
import unittest
from unittest.mock import patch, Mock

import pandas as pd
import bot
from long_term_review import evaluate_recommendations


def frame(count=23):
    return pd.DataFrame({'Open': [100.] * count, 'Close': [100.] * count,
                         'Adj Close': [100.] * count},
                        index=pd.bdate_range('2025-01-02', periods=count))


class AccuracyTests(unittest.TestCase):
    def test_drawdown_and_overlapping_samples(self):
        prices = frame(50)
        prices.iloc[5, prices.columns.get_loc('Adj Close')] = 80
        records = [{'symbol': 'AAA.BK', 'signal_date': date,
                    'delivery_status': 'SENT'}
                   for date in ('2025-01-02', '2025-01-03')]
        observations, summaries = evaluate_recommendations(
            records, {'AAA.BK': prices}, frame(50), horizons=(20,), cost_rate=0)
        self.assertEqual(len(observations), 2)
        self.assertEqual(summaries[20]['count'], 1)
        self.assertAlmostEqual(summaries[20]['max_drawdown'], -.2)

    def test_empty_history_avoids_market_requests(self):
        with patch.object(bot, 'get_market_frames') as fetch:
            report = bot.build_recommendation_accuracy({'long_term_recommendations': [
                {'symbol': 'AAA.BK', 'signal_date': '2025-01-02', 'delivery_status': 'PENDING'}]})
        self.assertIn('ยังไม่มีบันทึก', report)
        fetch.assert_not_called()

    def test_report_excludes_uncompleted_bars_and_exposes_missing_benchmark(self):
        records = [{'symbol': 'AAA.BK', 'signal_date': '2025-01-02',
                    'delivery_status': 'SENT'}]
        prices = frame(300)
        with patch.object(bot, 'get_market_frames', return_value={'AAA.BK': prices}), \
             patch.object(bot, 'latest_completed_set_session', return_value=pd.Timestamp('2025-02-03').date()):
            report = bot.build_recommendation_accuracy({'long_term_recommendations': records})
        self.assertIn('20 วันซื้อขาย | ตัวอย่าง 1 ช่วง', report)
        self.assertIn('60 วันซื้อขาย: ยังไม่ครบระยะ', report)
        self.assertIn('ชนะ SET: ข้อมูลดัชนีไม่พอ', report)
        self.assertIn('Drawdown ต่ำสุดรายคำแนะนำ', report)

    def test_command_aliases_queue_without_fetching_in_webhook(self):
        for command in ('ความแม่นยำคำแนะนำ', 'สถิติคำแนะนำ', 'ทดสอบความแม่นยำ'):
            with self.subTest(command=command), \
                 patch.object(bot, 'get_portfolio', return_value={}), \
                 patch.object(bot, 'queue_long_term_review', return_value=True) as queue:
                self.assertIn('กำลังประเมิน', bot.command_reply('user', command))
                queue.assert_called_once_with('user', accuracy_only=True)
        self.assertIn('ความแม่นยำคำแนะนำ', bot.help_text())

    def test_worker_sends_accuracy_report_without_long_term_enrollment(self):
        with patch.object(bot, 'get_portfolio', return_value={}), \
             patch.object(bot, 'build_recommendation_accuracy', return_value='report') as build, \
             patch.object(bot.line_bot_api, 'push_message') as push:
            bot._send_long_term_review('user', accuracy_only=True)
        build.assert_called_once_with({})
        self.assertEqual(push.call_args.args[1].text, 'report')

    def test_queue_preserves_requested_report_mode(self):
        scheduler = Mock()
        with patch.object(bot, 'ensure_scheduler'), patch.object(bot, 'scheduler', scheduler):
            try:
                self.assertTrue(bot.queue_long_term_review('accuracy-test', accuracy_only=True))
                self.assertFalse(bot.queue_long_term_review('accuracy-test', accuracy_only=True))
                self.assertEqual(scheduler.add_job.call_args.kwargs['args'], ['accuracy-test', True])
            finally:
                bot._long_term_review_users.discard('accuracy-test')
