"""Export fidelity, numeric JSON values, CSV encoding and historical scope."""
import codecs
import csv
from datetime import date
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from core.trade_instructions import (EXPORT_FORMATS, build_instruction_export,
    build_trade_instruction_data, build_sandbox_instruction_data, serialize_instructions,
    instruction_filename)
from tests.test_trade_instructions import sample_report


class InstructionFormatTests(unittest.TestCase):
    def setUp(self):
        self.report = sample_report()
        self.report['user_portfolio']['holdings'][0]['amount'] = 2000.
        self.report['decision_portfolio']['holdings'][0]['weight'] = .2
        self.report['decision_portfolio']['holdings'][2]['weight'] = .1
        self.today = date(2026, 11, 6)
        with patch('core.trade_instructions.derive_position_range', return_value={'scan_table': [
                dict(weight=0., within_budget=True, portfolio_max_drawdown=.1, portfolio_volatility=.06)]}):
            self.data = build_trade_instruction_data(self.report, today=self.today)

    def test_json_contains_numeric_amount_and_nested_conditions_and_trade(self):
        text = serialize_instructions(self.data, 'json')
        parsed = json.loads(text)
        self.assertEqual(parsed['fund_code'], '110020')
        self.assertEqual(parsed['action'], 'buy')
        self.assertEqual(parsed['amount'], 1000)
        self.assertIsInstance(parsed['amount'], (int, float))
        self.assertEqual(parsed['trade']['amount'], parsed['amount'])
        self.assertEqual(parsed['conditions']['B4'], '满足')
        self.assertIsInstance(parsed['reasons'], list)
        self.assertIn('从本月闲置资金池中扣除', parsed['trade']['funding_note'])
        self.assertNotIn('\\u57fa', text)

    def test_csv_is_two_columns_with_bom_and_roundtrips_chinese_and_quoted_values(self):
        self.data['fund_name'] = '中文基金,甲"乙\n多行'
        payload = serialize_instructions(self.data, 'csv')
        self.assertTrue(payload.startswith(codecs.BOM_UTF8))
        rows = list(csv.reader(io.StringIO(payload.decode('utf-8-sig'), newline='')))
        self.assertTrue(all(len(row) == 2 for row in rows))
        self.assertEqual(rows[0], ['字段名', '具体数值'])
        fields = dict(rows[1:])
        self.assertEqual(fields['基金名称'], self.data['fund_name'])
        self.assertEqual(fields['基金代码'], '110020')
        self.assertEqual(fields['建议金额'], '1000.00 元')
        self.assertEqual(fields['交易方向'], '买入')
        self.assertEqual(fields['免责声明'], '本指令仅供参考，不构成投资建议。')

    def test_plain_text_has_no_markdown_markers_but_keeps_values_and_indentation(self):
        text = serialize_instructions(self.data, 'txt')
        for marker in '#*|':
            self.assertNotIn(marker, text)
        self.assertIn('  建议金额：1000.00 元', text)
        self.assertIn('操作结论\n\n建议买入', text)
        self.assertIn('不构成投资建议', text)
        self.assertIn('# 本月调仓指令单', serialize_instructions(self.data, 'md'))

    def test_hold_has_null_amount_not_a_fictitious_zero_or_buy(self):
        self.report['conditions']['buy'][0]['status'] = '不满足'
        text = build_instruction_export(self.report, today=self.today, export_format='json')
        data = json.loads(text)
        self.assertEqual(data['action'], 'hold')
        self.assertIsNone(data['amount'])
        self.assertIsNone(data['trade'])

    def test_historical_amount_is_only_inside_simulation_for_all_formats(self):
        result = SimpleNamespace(start_date=pd.Timestamp('2021-01-01'),
            end_date=pd.Timestamp('2024-01-01'), monthly_amount=1000., orders=pd.DataFrame([
                dict(date=pd.Timestamp('2023-12-01'), signal_cutoff=pd.Timestamp('2023-11-30'),
                     pe_percentile=None, ma_deviation=None, multiplier=1.,
                     smart_purchase=1000., smart_cash=0.)]))
        payload = dict(code='002864', name='历史测试基金', result=result)
        data = build_sandbox_instruction_data(payload, today=self.today)
        for extension in EXPORT_FORMATS:
            with self.subTest(extension=extension):
                exported = build_instruction_export(payload, sandbox=True, today=self.today, export_format=extension)
                self.assertEqual(exported, serialize_instructions(data, extension))
        parsed = json.loads(serialize_instructions(data, 'json'))
        self.assertEqual(parsed['fund_code'], '002864')
        self.assertEqual(parsed['action'], 'hold')
        self.assertIsNone(parsed['amount'])
        self.assertEqual(parsed['simulation']['amount'], 1000)
        self.assertEqual(parsed['conditions']['B4'], '未评估')

    def test_each_format_has_expected_extension_and_mime(self):
        expected = {'md': 'text/markdown', 'txt': 'text/plain',
                    'csv': 'text/csv', 'json': 'application/json'}
        for extension, mime in expected.items():
            self.assertEqual(EXPORT_FORMATS[extension][1], mime)
            self.assertEqual(instruction_filename('002864', self.today, extension),
                             f'交易指令单_002864_20261106.{extension}')
        with self.assertRaises(ValueError):
            serialize_instructions(self.data, 'pdf')


if __name__ == '__main__':
    unittest.main()
