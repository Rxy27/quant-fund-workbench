"""Mathematical and point-in-time regressions for the historical sandbox."""
from datetime import date
import math
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from core import sandbox_engine as engine


def history(start='2019-01-01', end='2022-12-30', price=1.):
    dates = pd.bdate_range(start, end)
    return pd.DataFrame({'date': dates, 'cumulative_nav': price})


class XirrTests(unittest.TestCase):
    def test_known_positive_and_negative_annual_roots(self):
        dates = [date(2021, 1, 1), date(2022, 1, 1)]
        for terminal, expected in [(110., .1), (80., -.2), (1., -.99), (100., 0.)]:
            self.assertAlmostEqual(engine.calculate_xirr([-100., terminal], dates), expected, places=8)

    def test_irregular_cashflows_have_zero_discounted_npv(self):
        dates = pd.to_datetime(['2020-01-03', '2020-04-06', '2021-02-01', '2023-01-01'])
        amounts = [-1000., -700., -500., 2700.]
        rate = engine.calculate_xirr(amounts, dates)
        self.assertIsNotNone(rate)
        npv = sum(c / (1 + rate) ** ((d - dates[0]).days / 365.) for c, d in zip(amounts, dates))
        self.assertAlmostEqual(npv, 0., places=6)

    def test_invalid_no_solution_and_same_day_flows_return_none(self):
        for amounts, dates in [([], []), ([1., 2.], ['2021-01-01', '2022-01-01']),
            ([-100., 100.], ['2021-01-01'] * 2), ([-100., 110.], ['bad', '2022-01-01']),
            ([-100., math.inf], ['2021-01-01', '2022-01-01']),
            ([-100., 300., -250.], ['2021-01-01', '2022-01-01', '2023-01-01']),
            ([-100., 100.], ['2021-01-01'])]:
            self.assertIsNone(engine.calculate_xirr(amounts, dates))

    def test_newton_never_exceeds_100_steps_and_catches_zero_derivative(self):
        with patch.object(engine.math, 'exp', wraps=math.exp) as exp:
            self.assertIsNone(engine.calculate_xirr([-1., 1e100], ['2021-01-01', '2022-01-01']))
        self.assertEqual(exp.call_count, 200)  # two cashflows * 100 iterations
        with patch.object(engine.math, 'fsum', return_value=0.):
            self.assertIsNone(engine.calculate_xirr([-100., 110.], ['2021-01-01', '2022-01-01']))


class HistoricalTests(unittest.TestCase):
    def test_weekend_accounting_nav_is_not_a_trading_session(self):
        raw = pd.DataFrame({'date': pd.date_range('2023-12-28', '2024-01-02'), 'cumulative_nav': 1.})
        cleaned = engine.prepare_nav_history(raw)
        self.assertTrue((cleaned.date.dt.day_of_week < 5).all())
        self.assertNotIn(pd.Timestamp('2023-12-31'), set(cleaned.date))

    def test_ma_uses_warmup_before_start_and_never_backfills_nan(self):
        nav = history()
        nav['cumulative_nav'] = [1. + i / 1000 for i in range(len(nav))]
        snapshot = engine.replay_decision(nav, None, '2021-01-01')
        past = nav.loc[nav.date <= '2021-01-01']
        self.assertAlmostEqual(snapshot['ma200'], past.cumulative_nav.tail(200).mean())
        short = engine.replay_decision(nav, None, nav.date.iloc[198])
        self.assertIsNone(short['ma200'])
        self.assertIsNone(short['ma_deviation'])
        self.assertEqual(short['multiplier'], 1.)
        self.assertEqual(short['trend'], '历史不足')

    def test_mean_reversion_is_buy_more_below_ma(self):
        for price, expected in [(.5, 1.5), (2., .5), (1., 1.)]:
            nav = history()
            nav.loc[nav.index[-1], 'cumulative_nav'] = price
            snapshot = engine.replay_decision(nav, None, nav.date.iloc[-1])
            self.assertEqual(snapshot['multiplier'], expected)

    def test_snapshot_unchanged_by_future_prices_and_valuations(self):
        nav = history()
        pe = pd.DataFrame({'date': nav.date, 'pe': [10. + i % 30 for i in range(len(nav))]})
        original = engine.replay_decision(nav, pe, '2021-01-01')
        nav.loc[nav.date > '2021-01-01', 'cumulative_nav'] = 1000000.
        pe.loc[pe.date > '2021-01-01', 'pe'] = .00001
        self.assertEqual(original, engine.replay_decision(nav, pe, '2021-01-01'))
        expected_pe = pe.loc[pe.date <= '2021-01-01'].tail(1250)
        self.assertAlmostEqual(original['pe_percentile'], (expected_pe.pe < expected_pe.pe.iloc[-1]).mean())

    def test_pe_boundaries_and_invalid_stale_cache_fallback(self):
        nav = history()
        dates = nav.date.tail(100).reset_index(drop=True)
        for rank, expected in [(30, 1.5), (50, 1.), (51, .5)]:
            pe = pd.DataFrame({'date': dates, 'pe': [v + 1 for v in range(100) if v != rank] + [rank + 1]})
            self.assertEqual(engine.replay_decision(nav, pe, dates.iloc[-1])['multiplier'], expected)
        self.assertIsNone(engine.replay_decision(nav, pe.head(99), dates.iloc[-1])['pe_percentile'])
        self.assertIsNone(engine.replay_decision(nav, pe, '2023-04-01')['pe_percentile'])

    def test_unit_nav_is_rejected_and_only_cumulative_api_is_called(self):
        with self.assertRaises(ValueError):
            engine.prepare_nav_history(pd.DataFrame({'净值日期': ['2020-01-01'], '单位净值': [1.]}))
        with TemporaryDirectory() as temp, patch.object(engine.ak, 'fund_open_fund_info_em', return_value=history()) as fetch:
            frame, _ = engine.load_cumulative_nav('110020', data_dir=temp)
            fetch.assert_called_once_with(symbol='110020', indicator='累计净值走势', period='成立来')
            self.assertGreater((pd.Timestamp('2021-01-01') - frame.date.min()).days, 365)
            engine.load_cumulative_nav('110020', data_dir=temp, offline=True)
            self.assertEqual(fetch.call_count, 1)
            with self.assertRaises(ValueError):
                engine.load_cumulative_nav('005827', data_dir=temp, offline=True)

    def test_no_mapping_never_calls_live_valuation(self):
        with patch('core.valuation.evaluate_b2_condition', side_effect=AssertionError('live call')):
            pe, note = engine.load_valuation_history('005827')
        self.assertTrue(pe.empty)
        self.assertIn('降级', note)


class CashPoolTests(unittest.TestCase):
    def test_month_midpoint_does_not_create_fake_month_first(self):
        result = engine.run_sandbox(history(), None, '2021-01-15', '2021-04-01', 1000.)
        self.assertEqual(result.orders.date.iloc[0], pd.Timestamp('2021-02-01'))
        self.assertEqual(result.metrics['smart']['contributions'], 3)
        self.assertEqual(result.metrics['lump_sum']['principal'], 3000.)
        weekend = engine.run_sandbox(history(), None, '2021-01-01', '2021-02-15', 1000.)
        self.assertEqual(weekend.orders.date.iloc[0], pd.Timestamp('2021-01-01'))
        # Real calendar fixture: New Year is absent, so first trade is Jan 4.
        nav = history().loc[lambda d: d.date != pd.Timestamp('2021-01-01')]
        weekend = engine.run_sandbox(nav, None, '2021-01-01', '2021-02-15', 1000.)
        self.assertEqual(weekend.orders.date.iloc[0], pd.Timestamp('2021-01-04'))
        on_first = engine.run_sandbox(nav, None, '2021-01-04', '2021-02-15', 1000.)
        self.assertEqual(on_first.orders.date.iloc[0], pd.Timestamp('2021-01-04'))

    def test_daily_interest_compounds_and_budget_is_never_overdrawn(self):
        nav = history()
        nav['cumulative_nav'] = [1. + i / 1000 for i in range(len(nav))]
        result = engine.run_sandbox(nav, None, '2021-01-01', '2021-04-01', 1000.)
        balance = 0.
        purchases = result.orders.set_index('date').smart_purchase
        for day, row in result.daily.iterrows():
            balance *= (1 + .015 / 252)
            balance += row.injection
            purchase = purchases.get(day, 0.)
            self.assertLessEqual(purchase, balance)
            balance -= purchase
            self.assertAlmostEqual(row.smart_cash, balance, places=9)
            self.assertAlmostEqual(row.smart, row.smart_cash + row.smart_shares * row.cumulative_nav)
        self.assertGreater(balance, 2000.)
        self.assertEqual(result.metrics['smart']['principal'], result.metrics['regular']['principal'])
        self.assertEqual(result.metrics['smart']['principal'], result.metrics['lump_sum']['principal'])

    def test_below_ma_is_capped_at_available_cash(self):
        nav = history()
        nav.loc[nav.date >= '2020-12-30', 'cumulative_nav'] = .5
        result = engine.run_sandbox(nav, None, '2021-01-01', '2021-03-01', 1000.)
        self.assertEqual(result.orders.multiplier.iloc[0], 1.5)
        self.assertEqual(result.orders.smart_purchase.iloc[0], 1000.)
        self.assertTrue((result.daily.smart_cash >= 0).all())

    def test_execution_day_price_and_pe_never_determine_same_day_order(self):
        nav = history()
        pe = pd.DataFrame({'date': nav.date, 'pe': 10.})
        original = engine.run_sandbox(nav, pe, '2021-01-01', '2021-03-01', 1000.)
        nav.loc[nav.date == '2021-01-01', 'cumulative_nav'] = 10000.
        pe.loc[pe.date == '2021-01-01', 'pe'] = 10000.
        altered = engine.run_sandbox(nav, pe, '2021-01-01', '2021-03-01', 1000.)
        self.assertEqual(original.orders.smart_purchase.iloc[0], altered.orders.smart_purchase.iloc[0])
        self.assertEqual(original.orders.multiplier.iloc[0], altered.orders.multiplier.iloc[0])
        self.assertTrue((altered.orders.signal_nav_date < altered.orders.date).all())
        self.assertTrue((altered.orders.signal_pe_date < altered.orders.date).all())

    def test_monthly_injection_does_not_hide_drawdown(self):
        nav = history()
        nav.loc[nav.date >= '2021-02-01', 'cumulative_nav'] = .5
        result = engine.run_sandbox(nav, None, '2021-01-01', '2021-03-01', 1000.)
        # Absolute regular assets rise from 1000 to 1500 at the injection date,
        # but unitized performance must still recognize a 50% loss.
        self.assertAlmostEqual(result.metrics['regular']['max_drawdown'], .5)
        self.assertAlmostEqual(result.metrics['lump_sum']['max_drawdown'], .5)

    def test_xirr_uses_external_contributions_not_internal_purchases(self):
        result = engine.run_sandbox(history(), None, '2021-01-01', '2022-01-01', 1000.)
        self.assertAlmostEqual(result.metrics['regular']['xirr'], 0., places=8)
        orders = result.orders
        actual = result.metrics['smart']['xirr']
        expected = engine.calculate_xirr([-1000.] * len(orders) + [result.metrics['smart']['final_assets']],
                                         list(orders.date) + [result.end_date])
        self.assertEqual(actual, expected)

    def test_invalid_period_amount_and_missing_month_first_are_rejected(self):
        for start, end, amount in [('2021-01-01', '2021-01-01', 1000.),
                                   ('2021-01-01', '2022-01-01', math.nan),
                                   ('2021-01-15', '2021-01-29', 1000.)]:
            with self.assertRaises(ValueError):
                engine.run_sandbox(history(), None, start, end, amount)

    def test_conclusion_does_not_invent_bear_market_outperformance(self):
        nav = history()
        nav['cumulative_nav'] = [2. - i / 1500 for i in range(len(nav))]
        result = engine.run_sandbox(nav, None, '2021-01-01', '2022-01-01', 1000.)
        result.metrics['smart']['profit'] = -2000.
        result.metrics['regular']['profit'] = -1000.
        result.metrics['smart']['final_assets'] = 10000.
        result.metrics['regular']['final_assets'] = 11000.
        self.assertIn('扩大亏损', engine.sandbox_conclusion(result))
        self.assertNotIn('少亏', engine.sandbox_conclusion(result))


if __name__ == '__main__':
    unittest.main()
