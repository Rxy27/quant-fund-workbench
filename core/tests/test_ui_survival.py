"""Run the unchanged Streamlit app/presentation against new engine scenarios."""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from core import state
from core.analysis import load_fund
from core.allocation import UNIVERSE
from core.market_data import load_etf
from core.decision_rules import evaluate_conditions
from core.position_sizing import load_decision_config

ROOT = Path(__file__).resolve().parent.parent.parent


class SurvivalUI(unittest.TestCase):
    @unittest.skip("Requires private historical cache or local audit snapshot; excluded from public release")
    def test_real_2022_crash_in_generic_app(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            folder = Path(temp)
            portfolio, risk = state.load_state(folder)
            portfolio['holdings'] = [{'code': '510300', 'amount': portfolio['account_total'], 'cost_nav': None}]
            state.save_portfolio(portfolio, folder)
            state.write_json(folder/'ui_defaults.json', state.read_json(state.CONFIG_DIR/'ui_defaults.json'))
            stack.enter_context(patch.object(state, 'CONFIG_DIR', folder))
            def historical(code, offline=True):
                # Explicit historical test fixture; production single-fund
                # analysis no longer obtains data from the four-ETF cache.
                fund = deepcopy(load_etf(code, for_backtest=True) if code in UNIVERSE else load_fund(code, offline))
                fund.nav = fund.nav.loc[:'2022-04-26']
                return fund
            stack.enter_context(patch('core.analysis.load_fund', side_effect=historical))
            app = AppTest.from_file(str(ROOT/'app.py'), default_timeout=90).run()
            next(w for w in app.text_input if w.label.startswith('输入基金')).set_value('510300')
            next(b for b in app.button if b.label == '开始深度分析').click().run()
            app.session_state['analysis_tab'] = '决策依据清单'
            app.run()
            self.assertFalse(app.exception)
            visible = '\n'.join(w.value for widgets in (app.warning,app.info,app.success,app.error) for w in widgets)
            self.assertNotIn('熔断',visible)
            self.assertNotIn('暂缓加仓',visible)
            self.assertIn('非月度调仓日',visible)
            control = app.session_state.report['decision_portfolio']['_risk_controls']
            self.assertFalse(control['rebalance'])
            self.assertNotIn('defensive',control)
            self.assertEqual(set(control['risk_budgets']), {'510300'})
            self.assertTrue(all(w > 0 for w in control['target_weights'].values()))

    def render(self, fund, portfolio, config):
        # Call the actual unmodified presentation function with actual condition records.
        conditions = evaluate_conditions(fund, portfolio, config)
        app = AppTest.from_string('''
import streamlit as st
from ui.presentation import render_conditions
render_conditions(st.session_state.report)
''', default_timeout=60)
        app.session_state['report'] = {'conditions': conditions,
            'thresholds': {f'{side}{i}': '每月第一个已观察交易日' for side, n in [('B', 6), ('S', 5)] for i in range(1,n+1)}}
        app.run()
        self.assertFalse(app.exception)
        return app

    def test_recovery_has_no_delayed_warning(self):
        config = load_decision_config()
        nav = pd.Series(1., index=pd.bdate_range('2020-01-01', end='2022-04-01'))
        fund = {'code': '123456', 'nav': nav, 'role': '进攻型'}
        portfolio = {'holdings': [{'code':'123456','weight':.4}, {'code':'CASH','weight':.6}],
            'navs': {'123456':nav}}
        app = self.render(fund, portfolio, config)
        messages = [w.value for widgets in (app.warning,app.info,app.success) for w in widgets]
        self.assertFalse(any('暂缓加仓' in text or '熔断' in text for text in messages))
        self.assertTrue(any('B5' in text and '月度调仓日' in text for text in messages))

    def test_monthly_message_changes_without_renderer_edits(self):
        config = load_decision_config()
        for day, expected in (('2024-05-06','月度调仓日：按最新'),('2024-05-07','非月度调仓日')):
            nav = pd.Series([1.,1.,1.],index=pd.to_datetime(['2024-04-30','2024-05-06','2024-05-07'])).loc[:day]
            fund = {'code':'123456','nav':nav,'role':'进攻型'}
            portfolio = {'holdings':[{'code':'123456','weight':.10,'target_weight':.25}]}
            app = self.render(fund,portfolio,config)
            messages = [w.value for widgets in (app.info,app.success) for w in widgets]
            self.assertTrue(any('B5' in text and expected in text for text in messages))


if __name__ == '__main__':
    unittest.main()
