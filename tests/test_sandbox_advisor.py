"""AI sandbox transport, grounded data and distinct cache identity."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI
import pandas as pd

from core import sandbox_advisor as coach
from core.ai_advisor import DEFAULT_SETTINGS
from core.sandbox_engine import run_sandbox


def sample_result(end='2025-01-01'):
    dates = pd.bdate_range('2019-01-01', end)
    nav = pd.DataFrame({'date': dates, 'cumulative_nav': [2 - i / 2000 for i in range(len(dates))]})
    return run_sandbox(nav, None, '2021-01-01', end, 1000.)


def prose():
    return dict(zip(coach.TITLES, ['当时缺少估值数据，软件按每月金额的一半安排。',
        '三种方式都有亏损；一次投入要先备好全部资金，逐月投入不用。',
        '这段经历提醒我们按承受能力安排投入，纪律不能保证不亏钱。']))


class SandboxAdvisorTests(unittest.TestCase):
    def setUp(self):
        self.result = sample_result()
        self.context = coach.build_sandbox_context(self.result, '005827')
        self.settings = dict(DEFAULT_SETTINGS, enabled=True, api_key='private-test-key')

    def test_context_contains_three_strategy_facts_and_same_date_losses(self):
        json.dumps(self.context, allow_nan=False)
        self.assertEqual(self.context['period']['end'], '2025-01-01')
        self.assertIsNone(self.context['historical_state']['pe_percentile_pct'])
        for key, metrics in self.result.metrics.items():
            actual = self.context['strategies'][key]
            for field in ('principal', 'final_assets', 'profit'):
                self.assertEqual(actual[field], metrics[field])
            self.assertAlmostEqual(actual['max_drawdown_pct'], metrics['max_drawdown'] * 100)
        worst = self.context['comparisons']['at_regular_worst_loss']
        row = self.result.daily.loc[worst['date']]
        self.assertAlmostEqual(worst['regular_loss'], max(0, row.principal - row.regular))
        self.assertAlmostEqual(worst['smart_loss'], max(0, row.principal - row.smart))
        self.assertNotIn('api_key', json.dumps(self.context))
        short = coach.build_sandbox_context(sample_result('2021-08-01'), '005827')
        self.assertTrue(all(m['xirr_pct'] is None for m in short['strategies'].values()))

    def test_actual_sdk_sends_latest_hardcoded_prompt_and_strict_json(self):
        requests = []
        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant',
                'content': json.dumps(prose(), ensure_ascii=False)}}]})
        with patch('openai.OpenAI', side_effect=lambda **kw: OpenAI(**kw,
                http_client=httpx.Client(transport=httpx.MockTransport(handler)))):
            review = coach.generate_sandbox_review(self.context, self.settings)
        self.assertEqual(review['sections'], prose())
        self.assertEqual(review['error'], '')
        messages = requests[0]['messages']
        self.assertIn('极其接地气、一针见血', messages[0]['content'])
        self.assertIn('2021-01-01', messages[0]['content'])
        self.assertNotIn('{回溯日期}', messages[0]['content'])
        self.assertIn('缺失，不能判断高低', messages[0]['content'])
        self.assertIn('不是总账户半仓', messages[1]['content'])
        self.assertEqual(json.loads(messages[2]['content']), self.context)
        self.assertNotIn('private-test-key', json.dumps(requests))

    def test_disabled_never_calls_model_and_errors_are_not_fake_prose(self):
        with patch.object(coach, '_chat') as chat:
            result = coach.generate_sandbox_review(self.context, DEFAULT_SETTINGS)
        chat.assert_not_called()
        self.assertEqual(result['source'], 'AI 未启用')
        with patch.object(coach, '_chat', side_effect=TimeoutError('private-test-key')):
            error = coach.generate_sandbox_review(self.context, self.settings)
        self.assertEqual(error['sections'], {})
        self.assertIn('超时', error['error'])
        self.assertNotIn('private-test-key', str(error))

    def test_jargon_invalid_json_and_empty_sections_are_rejected(self):
        for raw in ['bad JSON', '{}', json.dumps(dict(prose(), 给你的启示='')),
                    json.dumps(dict(prose(), 给你的启示='最大回撤降低了。')),
                    json.dumps(dict(prose(), 给你的启示='xIrR 更高。'))]:
            with self.subTest(raw=raw), patch.object(coach, '_chat', return_value=raw):
                result = coach.generate_sandbox_review(self.context, self.settings)
                self.assertTrue(result['error'])
                self.assertEqual(result['sections'], {})

    def test_fingerprint_changes_with_result_settings_and_prompt(self):
        original = coach.review_fingerprint(self.context, self.settings)
        different = deepcopy(self.context)
        different['strategies']['smart']['final_assets'] += 1
        self.assertNotEqual(original, coach.review_fingerprint(different, self.settings))
        self.assertNotEqual(original, coach.review_fingerprint(self.context, dict(self.settings, model_id='other')))
        with patch.object(coach, 'SYSTEM_PROMPT', 'new prompt'):
            self.assertNotEqual(original, coach.review_fingerprint(self.context, self.settings))


if __name__ == '__main__':
    unittest.main()
