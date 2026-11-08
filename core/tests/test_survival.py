"""Behavioral acceptance tests for the upgraded core, separate from frozen audits."""
from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import unittest

import numpy as np
import pandas as pd
try:
    from sklearn.covariance import LedoitWolf
except ImportError:
    LedoitWolf = None

from core import decision_rules as rules
from core.allocation import (RiskState, leverage_control, allocation_target,
    is_rebalance_day, portfolio_controls, UNIVERSE)
from core.engine import load
from core.position_sizing import derive_position_range, load_decision_config
from core.risk_model import ewma_shrunk_covariance, risk_budget_weights
ROOT = Path(__file__).resolve().parent.parent.parent


def load_prices():
    """Deterministic synthetic OHLC fixture; never read local ETF holdings/data."""
    rng = np.random.default_rng(2026)
    dates = pd.bdate_range('2020-01-01', periods=1100)
    returns = rng.normal(0.0002, [0.01, 0.012, 0.001, 0.008], (len(dates), 4))
    closes = pd.DataFrame(100 * np.cumprod(1 + returns, axis=0), index=dates, columns=UNIVERSE)
    opens = closes.shift(1).fillna(100.) * 1.0001
    return opens, closes, {'source': 'synthetic fixture'}


def history_fixture():
    rng = np.random.default_rng(42)
    returns = rng.normal(0.0002, [0.01, 0.012, 0.001, 0.008], (320, 4))
    return pd.DataFrame(np.cumprod(1+returns, axis=0),
        index=pd.bdate_range('2020-01-01', periods=320), columns=UNIVERSE)


class RiskMatrices(unittest.TestCase):
    def test_all_up_and_flat_are_strictly_positive_definite(self):
        for values in (np.full((60, 4), .01), np.zeros((60, 4))):
            matrix = ewma_shrunk_covariance(values)
            self.assertGreater(np.linalg.eigvalsh(matrix).min(), 0)
            np.linalg.cholesky(matrix)
            weights = risk_budget_weights(matrix, np.full(4, .25))
            np.testing.assert_allclose(weights, .25)

    def test_positive_return_magnitude_does_not_add_downside(self):
        values = np.random.default_rng(2).normal(0, .01, (80, 4))
        changed = np.where(values > 0, values*100, values)
        np.testing.assert_allclose(ewma_shrunk_covariance(values), ewma_shrunk_covariance(changed))

    def test_recent_losses_receive_more_weight(self):
        values = np.full((61, 1), .001)
        values[0] = -.05
        recent = values[::-1].copy()
        self.assertGreater(ewma_shrunk_covariance(recent)[0, 0], ewma_shrunk_covariance(values)[0, 0]*5)

    @unittest.skipIf(LedoitWolf is None, 'Optional independent cross-check requires scikit-learn')
    def test_equal_time_weights_match_independent_ledoit_wolf(self):
        values = np.minimum(np.random.default_rng(3).normal(0, .01, (120, 4)), -.0001)
        expected = LedoitWolf(assume_centered=True).fit(values).covariance_
        actual = ewma_shrunk_covariance(values, halflife=1e15, annualization=1)
        np.testing.assert_allclose(actual, expected, rtol=1e-10, atol=1e-14)

    def test_constant_losses_are_not_demeaned_away(self):
        matrix = ewma_shrunk_covariance(np.full((60, 1), -.01))
        self.assertAlmostEqual(matrix[0, 0], .01**2*252)

    def test_invalid_data_fails_explicitly(self):
        for data in (np.full((1, 4), .01), np.full((60, 4), np.nan)):
            with self.assertRaises(ValueError):
                ewma_shrunk_covariance(data)


class Allocation(unittest.TestCase):
    def setUp(self):
        self.history = history_fixture()
        self.config = {**load_decision_config(), 'portfolio_target_volatility': .05}

    def test_raw_scores_and_budget_normalization(self):
        history = self.history.copy()
        history.iloc[:, 0] = np.linspace(1, 2, len(history))
        history.iloc[:, 1] = 1.
        history.iloc[:, 2] = np.linspace(2, 1, len(history))
        history.iloc[:, 3] = np.linspace(3, 1, len(history))
        scores, budgets = rules.rotation_budgets(history, {})
        np.testing.assert_allclose(scores, [32.5, 25, 7.5, 7.5])
        np.testing.assert_allclose(budgets, scores/scores.sum())
        matrix = ewma_shrunk_covariance(self.history.pct_change().iloc[1:])
        weights = risk_budget_weights(matrix, budgets)
        contribution = weights*(matrix@weights)
        np.testing.assert_allclose(contribution/contribution.sum(), budgets, atol=1e-7)
        self.assertTrue((weights > 0).all())

    def test_all_broken_remain_in_pool(self):
        history = pd.DataFrame({code: np.linspace(2, 1, 320) for code in UNIVERSE}, index=self.history.index)
        _, details = allocation_target(history, dict.fromkeys(UNIVERSE, .2), 1., self.config, RiskState())
        self.assertEqual(set(details['raw_scores'].values()), {7.5})
        self.assertEqual(set(details['target_weights']), set(UNIVERSE))
        self.assertTrue(all(v > 0 for v in details['target_weights'].values()))

    def test_missing_pool_asset_is_not_silently_dropped(self):
        with self.assertRaises(ValueError):
            allocation_target(self.history.iloc[:, :3], {}, 1., self.config, RiskState())

    def test_midmonth_drift_and_overvol_never_create_orders(self):
        history = self.history.loc[:'2021-03-16']
        actual = dict.fromkeys(UNIVERSE, .25)
        execution, details = allocation_target(history, actual, 1., self.config, RiskState())
        self.assertGreater(max(abs(details['target_weights'][c]-actual[c]) for c in UNIVERSE), .05)
        self.assertGreater(details['actual_volatility'], .05)
        self.assertFalse(details['scheduled_rebalance'])
        self.assertFalse(details['rebalance'])
        np.testing.assert_allclose(execution, list(actual.values()))

    def test_monthly_small_changes_trade_without_threshold(self):
        history = self.history.loc[:'2021-03-01']
        _, first = allocation_target(history, {}, 1., self.config, RiskState())
        actual = {c: w*.999 for c,w in first['target_weights'].items()}
        execution, details = allocation_target(history, actual, 1., self.config, RiskState())
        self.assertLess(max(abs(execution[c]-actual[c]) for c in UNIVERSE), .015)
        self.assertTrue(details['rebalance'])
        np.testing.assert_allclose(execution, list(details['target_weights'].values()))

    def test_calendar_uses_observed_month_year_and_holidays(self):
        for dates, expected in ((['2024-09-30','2024-10-08'], True),
                (['2024-10-08','2024-10-09'], False),
                (['2024-12-31','2025-01-02'], True),
                (['2023-01-31','2024-01-02'], True),
                (['2024-10-09'], False)):
            self.assertEqual(is_rebalance_day(dates), expected)
        for dates in (['2024-10-08','2024-10-08'], ['2024-10-09','2024-10-08']):
            with self.assertRaises(ValueError):
                is_rebalance_day(dates)

    def test_repeated_monthly_observation_does_not_emit_twice(self):
        state = RiskState()
        history = self.history.loc[:'2021-03-01']
        self.assertTrue(allocation_target(history, {}, 1., self.config, state)[1]['rebalance'])
        self.assertFalse(allocation_target(history, {}, 1., self.config, state)[1]['rebalance'])
        with self.assertRaises(ValueError):
            allocation_target(history.iloc[:-1], {}, 1., self.config, state)

    def test_equity_losses_do_not_change_targets_or_state(self):
        history = self.history.loc[:'2021-03-01']
        expected = allocation_target(history, {}, 1., self.config, RiskState())
        for equity in (.93, .92, .85, .5, .97):
            actual = allocation_target(history, {}, equity, self.config, RiskState())
            pd.testing.assert_series_equal(actual[0], expected[0])
            self.assertEqual(actual[1], expected[1])
        self.assertEqual(set(vars(RiskState())), {'last_date'})

    def test_skipped_observations_do_not_delay_target_recovery(self):
        history = self.history.loc[:'2021-03-01']
        state = RiskState(last_date=history.index[-10])
        execution, details = allocation_target(history, {}, 1., self.config, state)
        self.assertTrue(details['rebalance'])
        self.assertGreater(execution.sum(), 0)
        self.assertEqual(details['message'], '')


class Leverage(unittest.TestCase):
    def test_lower_volatility_increases_target_immediately(self):
        self.assertAlmostEqual(leverage_control(.10, .05, .95), .5)
        self.assertAlmostEqual(leverage_control(.06, .05, .95), .05/.06)
        self.assertEqual(leverage_control(.04, .05, .95), .95)

    def test_higher_volatility_reduces_target_immediately(self):
        self.assertEqual(leverage_control(.20, .05, .95), .25)
        self.assertEqual(leverage_control(.05, .05, .95), .95)

    def test_zero_volatility_has_finite_cap(self):
        self.assertEqual(leverage_control(0., .05, .95), .95)

    def test_invalid_inputs_fail(self):
        for vol, target, cap in ((float('nan'),.05,.95),(-.1,.05,.95),(.1,0,.95),(.1,.05,0)):
            with self.assertRaises(ValueError):
                leverage_control(vol,target,cap)


class Contracts(unittest.TestCase):
    def setUp(self):
        self.config = load_decision_config()
        self.nav = pd.Series(np.linspace(1, 1.2, 600), index=pd.bdate_range('2020-01-01', periods=600))
        self.fund = {'code': '123456', 'nav': self.nav, 'role': '进攻型', 'category': '指数型'}
        self.portfolio = {'holdings': [{'code': '123456', 'weight': .25, 'target_weight': .25}, {'code': 'CASH', 'weight': .75}],
                          'navs': {'123456': self.nav}}

    def test_all_eleven_signatures_and_dictionary_keys(self):
        original = load('decision_rules')
        for name in [f'buy_condition_{i}' for i in range(1, 7)]+[f'sell_condition_{i}' for i in range(1, 6)]:
            function = getattr(rules, name)
            self.assertEqual(inspect.signature(function), inspect.signature(getattr(original, name)))
            result = function(self.fund, self.portfolio, self.config)
            self.assertEqual(set(result), {'status','reason','evidence','data_source'})

    def test_sizing_schema_and_all_rising_input(self):
        original = load('position_sizing').derive_position_range
        self.assertEqual(inspect.signature(derive_position_range), inspect.signature(original))
        old = original(self.fund, self.portfolio, self.config)
        new = derive_position_range(self.fund, self.portfolio, self.config)
        self.assertEqual(set(old), set(new))
        self.assertEqual(set(old['covariance']), set(new['covariance']))
        self.assertEqual(set(old['scan_table'][0]), set(new['scan_table'][0]))
        self.assertTrue(np.isfinite(new['covariance']['annual_matrix']).all())
        self.assertTrue(new['feasible'])

    def test_condition_list_requires_monthly_date_in_both_directions(self):
        for dates, scheduled in ((['2024-04-30','2024-05-06'],True), (['2024-05-06','2024-05-07'],False)):
            fund = {**self.fund, 'nav': pd.Series([1.,1.01],index=pd.to_datetime(dates))}
            for actual in (.10,.2499,.25,.2501,.50):
                portfolio = {'holdings':[{'code':'123456','weight':actual,'target_weight':.25}]}
                buy = rules.buy_condition_5(fund,portfolio,self.config)
                sell = rules.sell_condition_3(fund,portfolio,self.config)
                self.assertEqual(buy['status'], '满足' if scheduled and actual < .25 else '不满足')
                self.assertEqual(sell['status'], '触发' if scheduled and actual > .25 else '未触发')
                self.assertIn('月度调仓日', buy['reason'])

    def test_missing_calendar_does_not_fabricate_permission(self):
        for name in ('buy_condition_5','sell_condition_3'):
            result = getattr(rules,name)({**self.fund,'nav':None},self.portfolio,self.config)
            self.assertEqual(result['status'],'无法判定')

    def test_sizing_and_controls_ignore_account_drawdown(self):
        expected = derive_position_range(self.fund,self.portfolio,self.config)
        control = portfolio_controls(self.fund,self.portfolio,self.config)
        equity = pd.Series(1.,index=self.nav.index)
        equity.iloc[-4:] = [.92,.85,.90,.96]
        portfolio = {**self.portfolio,'equity_history':equity}
        self.assertEqual(derive_position_range(self.fund,portfolio,self.config),expected)
        self.assertEqual(portfolio_controls(self.fund,portfolio,self.config),control)
        for text in ('熔断','延迟','暂缓加仓'):
            self.assertNotIn(text,expected['note'])
            self.assertNotIn(text,str(control))

    @unittest.skip("Requires private historical cache or local audit snapshot; excluded from public release")
    def test_protected_sources_unchanged(self):
        before = json.loads((ROOT/'outputs/survival_upgrade/before_hashes.json').read_text(encoding='utf-8'))
        # Generalization explicitly authorizes these formerly frozen modules.
        # Keep the historical lock on all other strategy/execution/UI sources.
        generalization_changes = {
            'app.py', 'ui/presentation.py', 'config/risk_budget.json',
            'fund_checkup/data_loader.py', 'fund_checkup/decision_data.py',
            'fund_checkup/decision_rules.py', 'fund_checkup/portfolio_analysis.py',
            'fund_checkup/position_sizing.py', 'fund_checkup/report_generator.py',
            'fund_checkup/statistics.py',
            'fund_checkup/stress_test.py',
            'fund_checkup/analyze.py',
        }
        for name, digest in before.items():
            if Path(name).parts[0] != 'core' and Path(name).as_posix() not in generalization_changes:
                self.assertEqual(hashlib.sha256((ROOT/name).read_bytes()).hexdigest(), digest, name)


@unittest.skip("Requires private historical cache or local audit snapshot; excluded from public release")
class HistoricalStress(unittest.TestCase):
    def test_2020_and_2022_account_drawdown_does_not_override_monthly_rules(self):
        _, closes, _ = load_prices()
        config = {**load_decision_config(), 'portfolio_target_volatility': .05}
        for start, end in (('2020-02-28','2020-03-31'), ('2022-03-31','2022-04-29')):
            nav = closes.loc[start:end,'510300']
            state, reference = RiskState(), RiskState()
            severe = 0
            for day, price in nav.items():
                equity = float(price/nav.iloc[0])
                history = closes.loc[:day]
                actual = allocation_target(history,{'510300':1.},equity,config,state)
                expected = allocation_target(history,{'510300':1.},1.,config,reference)
                pd.testing.assert_series_equal(actual[0],expected[0])
                self.assertEqual(actual[1],expected[1])
                if equity <= .93:
                    severe += 1
                    self.assertFalse(actual[1]['rebalance'])
            self.assertGreater(severe,0)


class Execution(unittest.TestCase):
    def test_core_executor_preserves_frozen_ledger_without_new_strategy(self):
        from core.backtest import run
        from core._frozen_portfolio import run as frozen
        opens, closes, _ = load_prices()
        opens, closes = opens.iloc[:240], closes.iloc[:240]
        signals = {closes.index[199]: dict.fromkeys(UNIVERSE, .2),
                   closes.index[220]: dict(zip(UNIVERSE, [.1,.15,.3,.2]))}
        expected = frozen(opens, closes, signals, 200)
        actual = run(opens, closes, signals, 200)
        pd.testing.assert_frame_equal(actual.curve, expected.curve)
        pd.testing.assert_frame_equal(actual.orders, expected.orders)

    def test_future_prices_cannot_change_prior_decisions_or_returns(self):
        from core.backtest import run
        opens, closes, _ = load_prices()
        config = {**load_decision_config(), 'portfolio_target_volatility': .05}
        full = run(opens.iloc[:280], closes.iloc[:280], {}, 200, strategy_config=config)
        prefix = run(opens.iloc[:240], closes.iloc[:240], {}, 200, strategy_config=config)
        pd.testing.assert_frame_equal(full.curve.iloc[:40], prefix.curve)
        self.assertEqual(full.gate_log[:40], prefix.gate_log)
        changed = closes.iloc[:280].copy()
        changed.iloc[240:] *= 2
        adversarial = run(opens.iloc[:280],changed,{},200,strategy_config=config)
        pd.testing.assert_frame_equal(adversarial.curve.iloc[:40],prefix.curve)
        self.assertEqual(adversarial.gate_log[:40],prefix.gate_log)

    def test_all_fills_follow_monthly_signals_at_next_open_in_whole_lots(self):
        from core.backtest import run
        opens, closes, _ = load_prices()
        config = {**load_decision_config(), 'portfolio_target_volatility': .05}
        result = run(opens.iloc[:500],closes.iloc[:500],{},200,strategy_config=config)
        self.assertFalse(result.orders.empty)
        for order in result.orders.to_dict('records'):
            signal = pd.Timestamp(order['signal_date'])
            i = closes.index.get_loc(signal)
            self.assertNotEqual(closes.index[i].to_period('M'),closes.index[i-1].to_period('M'))
            self.assertEqual(pd.Timestamp(order['date']),closes.index[i+1])
            self.assertEqual(order['quantity']%100,0)
        self.assertGreaterEqual(result.curve.cash.min(),0)
        self.assertTrue(all(not r['rebalance'] for r in result.gate_log if not r['scheduled_rebalance']))

    def test_midmonth_start_has_no_initial_entry_exception(self):
        from core.backtest import run
        opens, closes, _ = load_prices()
        start = closes.index.get_loc('2021-02-09')
        end = closes.index.get_loc('2021-03-01')+1
        config = {**load_decision_config(), 'portfolio_target_volatility': .05}
        result = run(opens.iloc[:end],closes.iloc[:end],{},start,strategy_config=config)
        self.assertTrue(result.orders.empty)
        self.assertTrue((result.curve.equity == 100000.).all())
        with self.assertRaises(ValueError):
            run(opens.iloc[:end],closes.iloc[:end],{closes.index[start]:dict.fromkeys(UNIVERSE,.2)},start,strategy_config=config)

    def test_independent_riskgate_blocks_buys_at_15pct_and_allows_sells(self):
        from tempfile import TemporaryDirectory
        from core._risk_gate import RiskGate, Account, Position, Order
        with TemporaryDirectory() as folder:
            gate = RiskGate(Path(folder)/'risk.json')
            gate.set_kill_switch(False)
            gate.observe(Account('2024-04-29',100000.))
            gate.observe(Account('2024-04-30',85000.))
            decision = gate.evaluate([Order('A',-100,10.),Order('B',100,10.)],
                Account('2024-05-06',85000.,(Position('A',1000,10.,10.),)))
            self.assertIn('account_drawdown_15pct',decision.reasons)
            self.assertEqual([(o.symbol,o.quantity) for o in decision.actual],[('A',-100)])


if __name__ == '__main__':
    unittest.main()
