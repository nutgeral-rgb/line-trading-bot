from datetime import date, datetime
import copy
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import pandas as pd
import bot
from dividend_goal import create_goal
from portfolio_manager import value_portfolio, format_manager_report


def frame(day='2026-11-13', close=10):
    return pd.DataFrame({'Close': [close]}, index=pd.to_datetime([day]))


class ValuationTests(unittest.TestCase):
    def test_unknown_values_never_use_cost_or_old_quotes(self):
        p = {'cash': 12000, 'stocks': {'AOT.BK': {'shares': 100, 'avg_price': 71},
                                      'BTS-W8.BK': {'shares': 60, 'avg_price': 0, 'status': 'SP', 'instrument_type': 'WARRANT'}}}
        val = value_portfolio(p, {'AOT.BK': frame(close=61.75), 'BTS-W8.BK': frame(close=.01)}, date(2026,11,13))
        self.assertEqual(val['known_total'], 18175)
        self.assertIsNone(val['total'])
        self.assertEqual(val['unpriced'], ['BTS-W8.BK'])
        self.assertIsNone(val['rows'][1]['weight'])
        for stale in ('2026-11-12', '2026-11-16'):
            self.assertIsNone(value_portfolio(p, {'AOT.BK': frame(stale)}, date(2026,11,13))['total'])

    def test_report_goal_and_partial_marking(self):
        p = {'cash':40816.8,'stocks':{},'history':[],
             'dividend_goal':create_goal(15000,10,date(2026,11,15)),
             'investment_strategy':{'monthly_contribution_baht':1000}}
        report = format_manager_report(p, {}, {}, date(2026,11,13), date(2026,11,15))
        for value in ('27,247', '235,570', '4,500,000', '2036-11-15', 'สุทธิ', 'สมมติฐาน'):
            self.assertIn(value,report)
        p['stocks']={'AOT.BK':{'shares':100,'avg_price':71}}
        report=format_manager_report(p, {}, {}, date(2026,11,13), date(2026,11,15))
        self.assertIn('มูลค่าไม่ครบ',report)
        self.assertNotIn('27,247',report)

    def test_complete_exposure_and_instrument_specific_review(self):
        p={'cash':12000,'stocks':{'QHBREIT.BK':{'shares':600,'avg_price':4.23,'instrument_type':'REIT'},
                                'AOT.BK':{'shares':100,'avg_price':71.12}}}
        val=value_portfolio(p,{'QHBREIT.BK':frame(close=3.58),'AOT.BK':frame(close=61.75)},date(2026,11,13))
        self.assertAlmostEqual(val['total'],20323)
        report=format_manager_report(p,{'QHBREIT.BK':frame(close=3.58),'AOT.BK':frame(close=61.75)}, {},date(2026,11,13),date(2026,11,15))
        self.assertIn('กระแสเงินสดและอายุสิทธิการเช่า',report)
        self.assertIn('ข้อมูลไม่พอ',report)


class ManagerCommands(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for key,value in [('DATABASE_URL',''),('ON_RENDER',False),('PORTFOLIO_FILE',Path(self.temp.name)/'p.json')]:
            patcher=patch.object(bot,key,value);patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(bot,'now_bkk',return_value=datetime(2026,11,15,12));patcher.start();self.addCleanup(patcher.stop)
        self.u='goal-test'

    def test_unlimited_actual_contributions_and_plans_survive_reenable(self):
        bot.command_reply(self.u,'ตั้งแผนลงทุนระยะยาว')
        bot.command_reply(self.u,'ตั้งเงินเติมรายเดือน 30000')
        bot.command_reply(self.u,'บันทึกเงินเติม 1000')
        bot.command_reply(self.u,'บันทึกเงินเติม 1500')
        bot.command_reply(self.u,'ตั้งแผนลงทุนระยะยาว')
        p=bot.get_portfolio(self.u)
        self.assertEqual(p['cash'],2500)
        self.assertEqual(p['investment_strategy']['monthly_contribution_baht'],30000)
        restored=bot.validate_portfolio_backup(bot.export_portfolio_backup(self.u))
        self.assertEqual(restored['investment_strategy']['monthly_contribution_baht'],30000)

    def test_goal_is_additive_and_reports_queue(self):
        bot.command_reply(self.u,'ตั้งเงินสด 12000')
        before=copy.deepcopy(bot.get_portfolio(self.u))
        reply=bot.command_reply(self.u,'ตั้งเป้าปันผล 15000 10')
        self.assertIn('2036-11-15',reply)
        after=bot.get_portfolio(self.u)
        self.assertEqual(after['cash'],before['cash'])
        self.assertEqual(after['stocks'],before['stocks'])
        for command in ('เป้าปันผล','วิเคราะห์พอร์ต'):
            with patch.object(bot,'queue_portfolio_manager',return_value=True) as queue, patch.object(bot,'get_market_frames') as fetch:
                self.assertIn('กำลัง',bot.command_reply(self.u,command))
                queue.assert_called_once_with(self.u);fetch.assert_not_called()
        restored=bot.validate_portfolio_backup(bot.export_portfolio_backup(self.u))
        self.assertEqual(restored['dividend_goal'],after['dividend_goal'])

    def test_warrant_and_metadata_roundtrip(self):
        self.assertIn('บันทึก',bot.command_reply(self.u,'ตั้งหุ้น BTS-W8 60 0'))
        self.assertIn('บันทึก',bot.command_reply(self.u,'ตั้งหลักทรัพย์ BTS-W8 WARRANT SP'))
        bot.command_reply(self.u,'ตั้งหุ้น BTS-W8 60 0')
        restored=bot.validate_portfolio_backup(bot.export_portfolio_backup(self.u))
        self.assertEqual(restored['stocks']['BTS-W8.BK']['status'],'SP')
        self.assertEqual(restored['stocks']['BTS-W8.BK']['instrument_type'],'WARRANT')

    def test_income_command_duplicate_and_user_isolation(self):
        cmd='บันทึกปันผล AOT 100 10 2026-11-01 ref1'
        self.assertIn('90.00',bot.command_reply(self.u,cmd))
        bot.command_reply(self.u,cmd)
        self.assertEqual(bot.get_portfolio(self.u)['cash'],90)
        self.assertEqual(bot.get_portfolio('other')['cash'],0)
        self.assertIn('90.00',bot.command_reply(self.u,'รายได้ปันผล'))
        restored=bot.validate_portfolio_backup(bot.export_portfolio_backup(self.u))
        self.assertEqual(restored['history'][-1]['reference'],'ref1')
        bot.command_reply(self.u,'ยกเลิกเงินรับ ref1')
        self.assertEqual(bot.get_portfolio(self.u)['cash'],0)
        self.assertIn('สุทธิ',bot.command_reply(self.u,'รายได้ปันผล'))

    def test_review_alias_queues_and_weekly_report_keeps_accuracy(self):
        with patch.object(bot,'queue_portfolio_manager',return_value=True) as queue:
            self.assertIn('กำลัง',bot.command_reply(self.u,'ตรวจพอร์ตระยะยาว'))
            queue.assert_called_once_with(self.u)
        p={'dividend_goal':create_goal(15000,10,date(2026,11,15))}
        with patch.object(bot,'build_portfolio_manager_report',return_value='goal') as manager, \
             patch.object(bot,'build_recommendation_accuracy',return_value='accuracy'), \
             patch.object(bot,'build_long_term_review') as legacy:
            self.assertEqual(bot.build_closed_day_report(p),'goal\n\naccuracy')
            manager.assert_called_once_with(p);legacy.assert_not_called()

    def test_worker_releases_lock_and_splits_long_message(self):
        bot._portfolio_manager_users.add(self.u)
        with patch.object(bot,'get_portfolio',return_value={}), \
             patch.object(bot,'build_portfolio_manager_report',return_value='a'*9000), \
             patch.object(bot.line_bot_api,'push_message') as push:
            bot._send_portfolio_manager(self.u)
            messages=push.call_args.args[1]
            self.assertEqual(''.join(m.text for m in messages),'a'*9000)
            self.assertTrue(all(len(m.text)<=4000 for m in messages))
        self.assertNotIn(self.u,bot._portfolio_manager_users)

    def test_intraday_quotes_do_not_displace_completed_close(self):
        p={'cash':12000,'stocks':{'AOT.BK':{'shares':100,'avg_price':71.12}}}
        prices=pd.DataFrame({'Close':[61.75,100]},index=pd.to_datetime(['2026-11-13','2026-11-16']))
        with patch.object(bot,'get_market_frames',return_value={'AOT.BK':prices}), \
             patch.object(bot,'analyze_fundamentals',return_value={}), \
             patch.object(bot,'latest_completed_set_session',return_value=date(2026,11,13)):
            report=bot.build_portfolio_manager_report(p)
        self.assertIn('18,175.00',report)
        self.assertNotIn('22,000.00',report)

    def test_restore_rejects_inconsistent_income_without_mutation(self):
        bot.command_reply(self.u,'ตั้งเงินสด 12000')
        bot.command_reply(self.u,'บันทึกปันผล AOT 100 10 2026-11-01 r1')
        original=json.loads(bot.export_portfolio_backup(self.u))
        malformed=[]
        for key, value in [('amount',900),('amount',True),('gross','100'),('tax',101),('paid_date','2026-11-30'),('reference',[])]:
            payload=copy.deepcopy(original)
            payload['portfolio']['history'][-1][key]=value
            malformed.append(payload)
        duplicate=copy.deepcopy(original)
        duplicate['portfolio']['history'].append(copy.deepcopy(duplicate['portfolio']['history'][-1]))
        malformed.append(duplicate)
        for ref, amount in [('unknown',90),('r1',900),('r1',True)]:
            payload=copy.deepcopy(original)
            payload['portfolio']['history'].append({'type':'DISTRIBUTION_REVERSAL','reference':ref,'amount':amount,'recorded_at':'2026-11-15'})
            malformed.append(payload)
        before=copy.deepcopy(bot.get_portfolio(self.u))
        for payload in malformed:
            with self.subTest(payload=payload),self.assertRaises(ValueError):
                bot.validate_portfolio_backup(json.dumps(payload))
            self.assertEqual(bot.get_portfolio(self.u),before)
        bot.command_reply(self.u,'ยกเลิกเงินรับ r1')
        restored=bot.validate_portfolio_backup(bot.export_portfolio_backup(self.u))
        self.assertEqual(restored['cash'],12000)

    def test_last_month_is_not_expired_before_target_date(self):
        p={'cash':1000,'stocks':{},'dividend_goal':create_goal(15000,10,date(2026,11,15))}
        report=format_manager_report(p,{}, {},date(2036,10,31),date(2036,11,1))
        self.assertNotIn('ถึงวันเป้าหมายแล้ว',report)
        self.assertIn('เหลือไม่ถึงหนึ่งรอบเติมเงิน',report)
