"""Instruction amounts, gates, account invariants and historical provenance."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import pandas as pd

from core.trade_instructions import (build_trade_instructions, build_sandbox_instructions,
                                      instruction_filename)


def sample_report():
    return dict(code='110020', fund=SimpleNamespace(info={'基金名称': '测试基金'}),
        metrics={'end': '2026-11-02'}, config={'single_fund_max_weight': .4},
        user_portfolio={'account_total': 10000., 'holdings': [
            {'code': '110020', 'amount': 1000.}, {'code': '002864', 'amount': 7000.}]},
        decision_fund={'code': '110020'}, decision_portfolio={'holdings': [
            {'code': '110020', 'weight': .1}, {'code': '002864', 'weight': .7},
            {'code': 'CASH', 'weight': .2}]},
        conditions={'buy': [dict(id=f'B{i}', label=f'条件{i}', status='满足',
            reason='测试条件通过', evidence={'target': .4} if i == 5 else {}) for i in range(1, 7)],
            'sell': [dict(id='S3', label='月度超配', status='未触发', reason='未超配', evidence={'target': .4})]})


class TradeInstructionTests(unittest.TestCase):
    def setUp(self):
        self.report = sample_report()
        self.today = date(2026, 11, 6)

    def build(self, **kwargs):
        return build_trade_instructions(self.report, today=self.today, **kwargs)

    def test_buy_uses_available_cash_and_rechecks_transfer_without_mutation(self):
        before = deepcopy(self.report)
        with patch('core.trade_instructions.derive_position_range', return_value={'scan_table': [
                dict(weight=0., within_budget=True, portfolio_max_drawdown=.1, portfolio_volatility=.06)]}) as scan:
            text = self.build()
        self.assertIn('建议买入', text)
        self.assertIn('建议金额：2000.00 元', text)
        self.assertIn('从本月闲置资金池中扣除', text)
        weights = scan.call_args.args[1]['holdings']
        self.assertAlmostEqual(sum(h['weight'] for h in weights), 1.)
        self.assertAlmostEqual(next(h['weight'] for h in weights if h['code'] == '110020'), .3)
        self.assertAlmostEqual(next(h['weight'] for h in weights if h['code'] == '002864'), .7)
        self.assertEqual(self.report, before)
        for field in ('2026-11-06', '110020', '测试基金', 'B2', 'B3', 'B4', '不构成投资建议'):
            self.assertIn(field, text)

    def test_specific_amount_failing_risk_check_never_exports_a_buy(self):
        for rows in ([], [dict(weight=0., within_budget=False)]):
            with self.subTest(rows=rows), patch('core.trade_instructions.derive_position_range',
                                               return_value={'scan_table': rows}):
                text = self.build()
            self.assertIn('建议观望', text)
            self.assertNotIn('交易方向：买入', text)

    def test_missing_or_failed_conditions_and_already_bought_do_not_buy(self):
        for index in range(6):
            for status in ('不满足', '无法判定'):
                report = deepcopy(self.report)
                report['conditions']['buy'][index]['status'] = status
                with self.subTest(index=index, status=status):
                    text = build_trade_instructions(report, today=self.today)
                    self.assertIn('建议观望', text)
                    self.assertNotIn('交易方向：买入', text)
        self.assertIn('不重复生成买入指令', self.build(has_invested_this_month=True))

    def test_old_snapshot_and_missing_date_do_not_generate_current_orders(self):
        self.report['conditions']['sell'][0]['status'] = '触发'
        for end in ('2025-11-02', None):
            self.report['metrics']['end'] = end
            text = self.build()
            self.assertIn('建议观望', text)
            self.assertNotIn('交易方向', text)

    def test_sell_takes_priority_and_only_reduces_existing_overweight(self):
        self.report['user_portfolio']['holdings'][0]['amount'] = 5000.
        self.report['user_portfolio']['holdings'][1]['amount'] = 3000.
        self.report['conditions']['sell'][0]['status'] = '触发'
        text = self.build(has_invested_this_month=True)
        self.assertIn('建议减仓', text)
        self.assertIn('交易方向：卖出', text)
        self.assertIn('建议金额：1000.00 元', text)
        self.assertNotIn('交易方向：买入', text)

    def test_unquantified_sell_does_not_invent_amount(self):
        self.report['conditions']['sell'][0].update(id='S2', status='触发', evidence={})
        text = self.build()
        self.assertIn('建议减仓', text)
        self.assertIn('没有可量化的减仓目标', text)
        self.assertNotIn('建议金额', text)

    def test_sandbox_exports_last_simulated_order_with_its_own_budget(self):
        result = SimpleNamespace(start_date=pd.Timestamp('2021-01-01'),
            end_date=pd.Timestamp('2024-01-01'), monthly_amount=1000., orders=pd.DataFrame([
                dict(date=pd.Timestamp('2023-12-01'), signal_cutoff=pd.Timestamp('2023-11-30'),
                     pe_percentile=.535, ma_deviation=.02, multiplier=.5,
                     smart_purchase=500., smart_cash=1500.)]))
        text = build_sandbox_instructions(dict(code='110020', name='测试基金', result=result), today=self.today)
        for field in ('测试基金', '2023-12-01', '2023-11-30', '53.5%', '500.00 元', '资金池 2000.00 元',
                      'B4 未评估', '无本月真实买卖指令', '历史模拟交易'):
            self.assertIn(field, text)
        self.assertNotIn('建议金额：', text)

    def test_filename_preserves_fund_code_and_generation_date(self):
        self.assertEqual(instruction_filename('110020', date(2023, 11, 6)), '交易指令单_110020_20231106.md')
        self.assertEqual(instruction_filename('002864', self.today), '交易指令单_002864_20261106.md')


if __name__ == '__main__':
    unittest.main()
