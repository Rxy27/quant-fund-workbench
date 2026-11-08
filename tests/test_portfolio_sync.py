"""Valuation invariants: cumulative NAV, published dates, cents and safe writes."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import pandas as pd
from requests.exceptions import ProxyError

from core import portfolio_sync as sync, state
from core.network import network_budget


def sample_portfolio():
    return {'account_total': 120., 'holdings': [
        {'code': '110020', 'amount': 100., 'cost_nav': 1.2,
         'base_nav': 2., 'base_date': '2026-11-04', 'is_calibrated': True}]}


class PortfolioSyncTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(sync, 'today', return_value=date(2026, 11, 6))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        sync.get_fund_nav_history.clear()
        self.addCleanup(sync.get_fund_nav_history.clear)
        quotes_patch = patch.object(sync, 'get_realtime_quotes', return_value={})
        self.quotes = quotes_patch.start()
        self.addCleanup(quotes_patch.stop)
        estimates_patch = patch.object(sync.ak, 'fund_value_estimation_em', return_value=pd.DataFrame())
        self.estimates = estimates_patch.start()
        self.addCleanup(estimates_patch.stop)

    def test_sync_evicts_only_the_requested_code_and_reads_fresh_history(self):
        frame = pd.DataFrame({'净值日期': ['2026-11-04'], '累计净值': [2.]})
        with patch.object(sync.ak, 'fund_open_fund_info_em', return_value=frame):
            sync.get_fund_nav_history(code='110020')
            sync.get_fund_nav_history(code='002864')
        newer = pd.DataFrame({'净值日期': ['2026-11-05'], '累计净值': [2.2]})
        with patch.object(sync.ak, 'fund_open_fund_info_em', return_value=newer) as provider:
            updated, warnings, count = sync.sync_portfolio(sample_portfolio())
            other = sync.get_fund_nav_history(code='002864')
        self.assertEqual((warnings, count), ([], 1))
        self.assertEqual(updated['holdings'][0]['amount'], 110.)
        self.assertEqual(float(other.iloc[-1]['累计净值']), 2.)
        self.assertEqual(provider.call_count, 1)

    def test_manual_rebase_also_refreshes_a_previously_cached_nav(self):
        stale = pd.DataFrame({'净值日期': ['2026-11-04'], '累计净值': [2.]})
        with patch.object(sync.ak, 'fund_open_fund_info_em', return_value=stale):
            sync.get_fund_nav_history(code='110020')
        portfolio = sample_portfolio()
        portfolio['holdings'][0]['base_nav'] = None
        newer = pd.DataFrame({'净值日期': ['2026-11-05'], '累计净值': [2.2]})
        with patch.object(sync.ak, 'fund_open_fund_info_em', return_value=newer):
            updated = sync.initialize_baselines(portfolio)
        self.assertEqual(updated['holdings'][0]['base_nav'], 2.2)
        self.assertEqual(updated['holdings'][0]['amount'], 100.)

    def test_cumulative_nav_ignores_unit_nav_and_future_observations(self):
        # A cash dividend cuts unit NAV; cumulative NAV remains continuous.
        frame = pd.DataFrame({'净值日期': ['2026-11-07', '2026-11-06', '2026-11-05'],
                              '单位净值': [99., .8, 1.], '累计净值': [99., 2.01, 2.]})
        with patch.object(sync.ak, 'fund_open_fund_info_em', return_value=frame) as provider:
            nav, day = sync.fetch_latest_cumulative_nav('110020')
        self.assertEqual((nav, day), (2.01, '2026-11-06'))
        provider.assert_called_once_with(symbol='110020', indicator='累计净值走势', period='成立来')
        with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(nav, day)):
            updated, warnings, count = sync.sync_portfolio(sample_portfolio())
        self.assertEqual(updated['holdings'][0]['amount'], 100.5)
        self.assertEqual(updated['account_total'], 120.5)
        self.assertEqual((warnings, count), ([], 1))

    def test_unit_only_empty_and_invalid_nav_are_rejected(self):
        frames = [pd.DataFrame(), pd.DataFrame({'净值日期': ['2026-11-05'], '单位净值': [1.]}),
                  pd.DataFrame({'净值日期': ['bad', '2026-11-07', '2026-11-05', '2026-11-05'],
                                '累计净值': [1., 2., float('inf'), 0.]})]
        for frame in frames:
            sync.get_fund_nav_history.clear(code='110020')
            with self.subTest(frame=frame), patch.object(
                    sync.ak, 'fund_open_fund_info_em', return_value=frame):
                with self.assertRaises(ValueError):
                    sync.fetch_latest_cumulative_nav('110020')

    def test_hanging_provider_has_a_wall_clock_deadline(self):
        release = threading.Event()
        try:
            with patch.object(sync.ak, 'fund_open_fund_info_em', side_effect=lambda **_: release.wait(1)):
                with network_budget(.02), self.assertRaises(TimeoutError):
                    sync.fetch_latest_cumulative_nav('999998')
        finally:
            release.set()

    def test_provider_network_error_becomes_an_actionable_warning(self):
        with patch.object(sync.ak, 'fund_open_fund_info_em', side_effect=ProxyError('unreachable')):
            updated, warnings, count = sync.sync_portfolio(sample_portfolio())
        self.assertEqual(updated, sample_portfolio())
        self.assertEqual(count, 0)
        self.assertIn('网络连接失败，请检查网络或代理设置后重试', warnings[0])

    def test_legacy_upgrade_is_persisted_once_without_changing_amounts(self):
        portfolio = sample_portfolio()
        portfolio['holdings'][0].pop('base_nav')
        portfolio['holdings'][0].pop('base_date')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'user_portfolio.json'
            state.write_json(path, portfolio)
            with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2.5, '2026-11-05')) as fetch:
                first, _ = state.load_state(directory)
                second, _ = state.load_state(directory)
            self.assertEqual(first, second)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(first['holdings'][0]['amount'], 100.)
            self.assertEqual(first['holdings'][0]['base_nav'], 2.5)
            self.assertEqual(state.read_json(path), first)

    def test_legacy_offline_never_invents_an_official_baseline(self):
        portfolio = sample_portfolio()
        portfolio['holdings'][0].pop('base_nav')
        with tempfile.TemporaryDirectory() as directory, patch.object(
                sync, 'fetch_latest_cumulative_nav', side_effect=ConnectionError('断网')):
            state.write_json(Path(directory) / 'user_portfolio.json', portfolio)
            with self.assertLogs(sync.__name__, level='WARNING') as logs:
                upgraded, _ = state.load_state(directory)
            self.assertIn('断网', logs.output[0])
            holding = upgraded['holdings'][0]
            self.assertNotIn('base_nav', holding)
            self.assertEqual((holding['base_date'], holding['amount']), ('2026-11-04', 100.))
            self.assertEqual(state.read_json(Path(directory) / 'user_portfolio.json'), upgraded)

    def test_repeat_or_older_dates_never_apply_returns_twice(self):
        original = sample_portfolio()
        for day in ('2026-11-04', '2026-11-03'):
            with self.subTest(day=day), patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2., day)):
                updated, warnings, count = sync.sync_portfolio(original)
            self.assertEqual((updated, count), (original, 0))
            self.assertEqual(len(warnings), 1)

    def test_same_day_correction_updates_and_then_warns_once_unchanged(self):
        with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2.2, '2026-11-04')):
            updated, warnings, count = sync.sync_portfolio(sample_portfolio())
            repeated, repeat_warnings, repeat_count = sync.sync_portfolio(updated)
        self.assertEqual((updated['holdings'][0]['amount'], count, warnings), (110., 1, []))
        self.assertEqual((repeated, repeat_count), (updated, 0))
        self.assertIn('数据源尚未更新', repeat_warnings[0])

    def test_missing_or_zero_baseline_recovers_without_revaluing_amount(self):
        for base_nav in (None, 0.):
            original = sample_portfolio()
            original['holdings'][0]['base_nav'] = base_nav
            with self.subTest(base_nav=base_nav), patch.object(
                    sync, 'fetch_latest_cumulative_nav', return_value=(2.2, '2026-11-05')):
                updated, warnings, count = sync.sync_portfolio(original)
            self.assertEqual((count, warnings), (1, []))
            self.assertEqual(updated['holdings'][0]['base_nav'], 2.2)
            self.assertEqual(updated['holdings'][0]['amount'], 100.)
            self.assertEqual(updated['account_total'], 120.)

    def test_large_moves_are_blocked_on_same_and_new_dates(self):
        original = sample_portfolio()
        for nav in (2.4001, 1.5999):
            for day in ('2026-11-04', '2026-11-05'):
                with self.subTest(nav=nav, day=day), patch.object(
                        sync, 'fetch_latest_cumulative_nav', return_value=(nav, day)):
                    updated, warnings, count = sync.sync_portfolio(original)
                self.assertEqual((updated, count), (original, 0))
                self.assertIn('单日波动超过20%', warnings[0])
                self.assertIn('原油、黄金、跨境QDII', warnings[0])

    def test_exact_twenty_percent_is_allowed(self):
        for nav in (1.6, 2.4):
            with self.subTest(nav=nav), patch.object(
                    sync, 'fetch_latest_cumulative_nav', return_value=(nav, '2026-11-05')):
                updated, warnings, count = sync.sync_portfolio(sample_portfolio())
            self.assertEqual((warnings, count), ([], 1))
            self.assertEqual(updated['holdings'][0]['amount'], round(100 * nav / 2, 2))

    def test_invalid_latest_does_not_fall_back_to_older_positive_observation(self):
        frame = pd.DataFrame({'净值日期': ['2026-11-04', '2026-11-05'], '累计净值': [2., 0.]})
        with patch.object(sync.ak, 'fund_open_fund_info_em', return_value=frame):
            updated, warnings, count = sync.sync_portfolio(sample_portfolio())
        self.assertEqual((updated, count), (sample_portfolio(), 0))
        self.assertIn('净值', warnings[0])

    def test_rounding_rebases_each_sync_and_preserves_cash(self):
        original = sample_portfolio()
        with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2.123456, '2026-11-05')):
            updated, _, _ = sync.sync_portfolio(original)
        self.assertEqual(updated['holdings'][0]['amount'], 106.17)
        self.assertEqual(updated['holdings'][0]['base_nav'], 2.123456)
        with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2.234567, '2026-11-06')):
            latest, _, _ = sync.sync_portfolio(updated)
        expected = round(106.17 * (2.234567 / 2.123456), 2)
        self.assertEqual(latest['holdings'][0]['amount'], expected)
        self.assertAlmostEqual(latest['account_total'] - expected, 20.)
        self.assertEqual(original, sample_portfolio())

    def test_one_failed_fund_does_not_prevent_other_updates(self):
        portfolio = sample_portfolio()
        portfolio['holdings'].append(dict(portfolio['holdings'][0], code='002864', amount=20.))
        with patch.object(sync, 'fetch_latest_cumulative_nav', side_effect=[
                ConnectionError('断网'), (2.2, '2026-11-05')]):
            updated, warnings, count = sync.sync_portfolio(portfolio)
        self.assertEqual(updated['holdings'][0], portfolio['holdings'][0])
        self.assertEqual(updated['holdings'][1]['amount'], 22.)
        self.assertEqual(updated['account_total'], 122.)
        self.assertEqual(count, 1)
        self.assertIn('110020 同步失败', warnings[0])
        state.validate_portfolio(updated)

    def test_future_or_nonpositive_values_preserve_the_entire_holding(self):
        original = sample_portfolio()
        for nav, day in ((3., '2026-11-07'), (None, '2026-11-05'), (-1., '2026-11-05'),
                         (0., '2026-11-05'), (float('nan'), '2026-11-05')):
            with self.subTest(nav=nav, day=day), patch.object(
                    sync, 'fetch_latest_cumulative_nav', return_value=(nav, day)):
                updated, warnings, count = sync.sync_portfolio(original)
            self.assertEqual(updated, original)
            self.assertEqual(count, 0)
            self.assertEqual(len(warnings), 1)

    def test_manual_amount_or_code_edit_rebases_but_cost_edit_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            original = state.save_portfolio(sample_portfolio(), directory)
            edited = deepcopy(original)
            edited['holdings'][0]['cost_nav'] = 1.3
            with patch.object(sync, 'fetch_latest_cumulative_nav') as fetch:
                state.save_portfolio(edited, directory, rebase=True)
                fetch.assert_not_called()
            for changes in ({'amount': 80.}, {'code': '002864'}):
                edited['holdings'][0].update(changes)
                with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(3., '2026-11-06')):
                    saved = state.save_portfolio(edited, directory, rebase=True)
                self.assertEqual(saved['holdings'][0]['base_nav'], 3.)
                self.assertEqual(saved['holdings'][0]['base_date'], '2026-11-06')

    def test_atomic_write_failure_keeps_previous_json(self):
        with tempfile.TemporaryDirectory() as directory:
            state.save_portfolio(sample_portfolio(), directory)
            path = Path(directory) / 'user_portfolio.json'
            before = path.read_bytes()
            with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2.2, '2026-11-05')):
                updated, _, _ = sync.sync_portfolio(sample_portfolio())
            with patch('core.state.os.replace', side_effect=OSError('磁盘不可写')):
                with self.assertRaises(OSError):
                    state.save_portfolio(updated, directory)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(list(Path(directory).glob('*.tmp')))

    def test_fully_invested_cents_do_not_create_negative_cash(self):
        portfolio = sample_portfolio()
        portfolio['account_total'] = 80.28
        portfolio['holdings'][0]['amount'] = 31.27
        portfolio['holdings'].append(dict(portfolio['holdings'][0], code='002864', amount=49.01))
        for holding in portfolio['holdings']:
            holding['base_nav'] = .04
        with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(.036, '2026-11-05')):
            updated, warnings, count = sync.sync_portfolio(portfolio)
        self.assertEqual([h['amount'] for h in updated['holdings']], [28.14, 44.11])
        self.assertEqual(updated['account_total'], 72.25)
        state.validate_portfolio(updated)
        self.assertEqual(state.portfolio_cash(updated), 0.)
        self.assertEqual(state.portfolio_settings(updated)['holdings'][-1]['weight'], 0.)

    def test_intraday_both_routes_persist_without_polluting_official_baselines(self):
        original = sample_portfolio()
        original['holdings'].append(dict(original['holdings'][0], code='002864', amount=10.))
        original['holdings'].append(dict(original['holdings'][0], code='000001', amount=5.))
        self.quotes.return_value = {'510300': {'change_pct': 2.5}}
        self.estimates.return_value = pd.DataFrame({
            '基金代码': ['002864', '000001'],
            '2026-11-05-估算数据-估算值': [1.32, 1.08],
            '2026-11-04-单位净值': [1.2, 1.]})
        with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2., '2026-11-04')):
            updated, warnings, count = sync.sync_portfolio(original)
            repeated, _, _ = sync.sync_portfolio(updated)
        self.assertEqual((warnings, count), ([], 0))
        self.assertEqual(updated, repeated)  # no compounding on repeated clicks
        self.assertEqual([h['estimated_amount'] for h in updated['holdings']], [102.5, 11., 5.4])
        self.assertEqual(self.estimates.call_count, 2)  # once per sync, not per fund
        self.estimates.assert_called_with(symbol='全部')
        self.quotes.assert_called_with(['510300'])
        for before, after in zip(original['holdings'], updated['holdings']):
            self.assertFalse(after['is_calibrated'])
            for field in ('amount', 'base_nav', 'base_date'):
                self.assertEqual(before[field], after[field])
        self.assertEqual(updated['account_total'], original['account_total'])
        with tempfile.TemporaryDirectory() as directory:
            state.save_portfolio(updated, directory)
            fresh, _ = state.load_state(directory)
            self.assertEqual(fresh, updated)
            settings = state.portfolio_settings(fresh)
            self.assertFalse(any('estimated_amount' in h or 'is_calibrated' in h
                                 for h in settings['holdings']))

    def test_official_calibration_clears_estimate_and_uses_official_amount(self):
        original = sample_portfolio()
        original['holdings'][0].update(is_calibrated=False, estimated_amount=150.)
        with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2.2, '2026-11-05')):
            updated, warnings, count = sync.sync_portfolio(original)
        holding = updated['holdings'][0]
        self.assertEqual((holding['amount'], holding['base_nav'], holding['base_date']),
                         (110., 2.2, '2026-11-05'))
        self.assertTrue(holding['is_calibrated'])
        self.assertNotIn('estimated_amount', holding)
        self.assertEqual((warnings, count), ([], 1))

    def test_previous_session_etf_returns_are_not_reapplied_on_nontrading_days(self):
        original = sample_portfolio()
        self.quotes.return_value = {'510300': {'change_pct': 2.5, 'market_time': '2026-11-04 15:00:00'}}
        with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2., '2026-11-04')):
            updated, warnings, count = sync.sync_portfolio(original)
        self.assertEqual((updated, count), (original, 0))
        self.assertIn('行情日期', warnings[0])

    def test_weekend_latest_official_nav_is_not_an_estimation_failure(self):
        original = sample_portfolio()
        original['holdings'][0]['base_date'] = '2026-11-06'
        original['holdings'].append(dict(original['holdings'][0], code='002864', amount=10.))
        self.quotes.return_value = {'510300': {'change_pct': -1.69, 'market_time': '2026-11-06 16:14:52'}}
        with patch.object(sync, 'today', return_value=date(2026, 11, 7)), patch.object(
                sync, 'fetch_latest_cumulative_nav', return_value=(2., '2026-11-06')):
            updated, warnings, count = sync.sync_portfolio(original)
        self.assertEqual((updated, warnings, count), (original, [], 0))
        self.quotes.assert_not_called()
        self.estimates.assert_not_called()

    def test_invalid_or_failed_quotes_preserve_existing_pending_state(self):
        original = sample_portfolio()
        original['holdings'][0].update(is_calibrated=False, estimated_amount=101.)
        for quote in ({}, {'510300': {}}, {'510300': {'change_pct': float('nan')}},
                      {'510300': {'change_pct': -100}}):
            self.quotes.return_value = quote
            with self.subTest(quote=quote), patch.object(sync, 'fetch_latest_cumulative_nav',
                                                        return_value=(2., '2026-11-04')):
                updated, warnings, count = sync.sync_portfolio(original)
            self.assertEqual((updated, count), (original, 0))
            self.assertTrue(warnings)
        self.quotes.side_effect = ConnectionError('断网')
        with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2., '2026-11-04')):
            self.assertEqual(sync.sync_portfolio(original)[0], original)

    def test_active_fund_unit_nav_fallback_does_not_divide_by_cumulative_nav(self):
        original = sample_portfolio()
        original['holdings'][0]['code'] = '002864'
        self.estimates.return_value = pd.DataFrame({
            '基金代码': ['002864'], '2026-11-05-估算数据-估算值': [1.32]})
        unit_history = pd.DataFrame({'净值日期': ['2026-11-04'], '单位净值': [1.2]})
        with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2., '2026-11-04')), patch.object(
                sync.ak, 'fund_open_fund_info_em', return_value=unit_history) as unit:
            updated, warnings, _ = sync.sync_portfolio(original)
        self.assertEqual(warnings, [])
        self.assertEqual(updated['holdings'][0]['estimated_amount'], 110.)
        unit.assert_called_once_with(symbol='002864', indicator='单位净值走势', period='成立来')

    def test_failed_all_market_fetch_is_not_retried_for_each_fund(self):
        original = sample_portfolio()
        original['holdings'][0]['code'] = '002864'
        original['holdings'].append(dict(original['holdings'][0], code='000001', amount=10.))
        self.estimates.side_effect = ConnectionError('断网')
        with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2., '2026-11-04')):
            updated, warnings, count = sync.sync_portfolio(original)
        self.estimates.assert_called_once_with(symbol='全部')
        self.assertEqual((updated, count), (original, 0))
        self.assertEqual(len(warnings), 2)

    def test_stale_future_missing_and_nonfinite_estimates_are_rejected(self):
        original = sample_portfolio()
        original['holdings'][0]['code'] = '002864'
        frames = [pd.DataFrame({'基金代码': ['002864'], f'{day}-估算数据-估算值': [nav],
                               '2026-11-04-单位净值': [1.2]}) for day, nav in
                  [('2026-11-04', 1.32), ('2026-11-07', 1.32), ('2026-11-05', '--'),
                   ('2026-11-05', float('inf')), ('2026-11-05', 0.)]]
        frames.append(pd.DataFrame({'基金代码': ['999999']}))
        for frame in frames:
            self.estimates.return_value = frame
            with self.subTest(frame=frame), patch.object(sync, 'fetch_latest_cumulative_nav',
                                                        return_value=(2., '2026-11-04')):
                updated, warnings, count = sync.sync_portfolio(original)
            self.assertEqual((updated, count), (original, 0))
            self.assertTrue(warnings)

    def test_estimate_state_schema_rejects_invalid_values(self):
        for fields in ({'is_calibrated': 'false'}, {'is_calibrated': False},
                       {'is_calibrated': False, 'estimated_amount': float('nan')},
                       {'estimated_amount': True}, {'estimated_amount': -1}):
            portfolio = sample_portfolio()
            portfolio['holdings'][0].update(fields)
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                state.validate_portfolio(portfolio)

    def test_confirmed_shares_calibrate_same_date_using_unit_not_cumulative_nav(self):
        original = {'account_total': 80.29, 'holdings': [
            dict(code='110020', amount=20.32, shares=11.93, cost_nav=1.6764,
                 base_nav=1.6804, base_date='2026-11-06', is_calibrated=True),
            dict(code='002864', amount=59.97, shares=58.36, cost_nav=1.0281,
                 base_nav=1.3168, base_date='2026-11-06', is_calibrated=True)]}
        unit_navs = {'110020': 1.6804, '002864': 1.0277}
        with patch.object(sync, 'today', return_value=date(2026, 11, 7)), patch.object(
                sync, 'fetch_latest_cumulative_nav', side_effect=lambda code: (
                    1.6804 if code == '110020' else 1.3168, '2026-11-06')), patch.object(
                sync, 'fetch_official_unit_nav', side_effect=lambda code, day: unit_navs[code]):
            updated, warnings, count = sync.sync_portfolio(original)
            repeated, repeat_warnings, repeat_count = sync.sync_portfolio(updated)
        self.assertEqual((warnings, count), ([], 2))
        self.assertEqual([h['amount'] for h in updated['holdings']], [20.05, 59.98])
        self.assertEqual(updated['account_total'], 80.03)
        self.assertEqual([h['shares'] for h in updated['holdings']], [11.93, 58.36])
        self.assertEqual((repeated, repeat_warnings, repeat_count), (updated, [], 0))
        with tempfile.TemporaryDirectory() as directory:
            state.save_portfolio(updated, directory)
            fresh, _ = state.load_state(directory)
            self.assertEqual(fresh, updated)
            self.assertFalse(any('shares' in h or 'base_unit_nav' in h
                                 for h in state.portfolio_settings(fresh)['holdings']))

    def test_share_based_future_official_values_do_not_compound_rounded_amounts(self):
        original = sample_portfolio()
        original['holdings'][0].update(amount=20.05, shares=11.93, base_unit_nav=1.6804)
        with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2.2, '2026-11-05')), patch.object(
                sync, 'fetch_official_unit_nav', return_value=1.7):
            updated, warnings, count = sync.sync_portfolio(original)
        self.assertEqual((warnings, count), ([], 1))
        self.assertEqual(updated['holdings'][0]['amount'], 20.28)
        self.assertEqual(updated['holdings'][0]['shares'], 11.93)
        self.assertEqual(updated['holdings'][0]['base_unit_nav'], 1.7)

    def test_share_based_intraday_keeps_official_baseline_and_calibrates_later(self):
        original = sample_portfolio()
        holding = original['holdings'][0]
        holding.update(code='002864', shares=58.36, amount=59.98, base_unit_nav=1.0277)
        self.estimates.return_value = pd.DataFrame({'基金代码': ['002864'],
            '2026-11-05-估算数据-估算值': [1.028]})
        with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2., '2026-11-04')), patch.object(
                sync, 'fetch_official_unit_nav', return_value=1.0277):
            pending, warnings, count = sync.sync_portfolio(original)
            repeated, _, _ = sync.sync_portfolio(pending)
        self.assertEqual((warnings, count), ([], 0))
        self.assertEqual(pending['holdings'][0]['estimated_amount'], 59.99)
        self.assertFalse(pending['holdings'][0]['is_calibrated'])
        self.assertEqual(repeated, pending)
        for field in ('amount', 'base_nav', 'base_date', 'base_unit_nav', 'shares'):
            self.assertEqual(pending['holdings'][0][field], holding[field])
        with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2.001, '2026-11-05')), patch.object(
                sync, 'fetch_official_unit_nav', return_value=1.0281):
            final, warnings, count = sync.sync_portfolio(pending)
        self.assertEqual((warnings, count), ([], 1))
        self.assertTrue(final['holdings'][0]['is_calibrated'])
        self.assertNotIn('estimated_amount', final['holdings'][0])
        self.assertEqual(final['holdings'][0]['amount'], 60.)

    def test_etf_shares_estimation_and_missing_unit_nav_fail_safely(self):
        original = sample_portfolio()
        original['holdings'][0].update(shares=11.93, amount=20.05, base_unit_nav=1.6804)
        self.quotes.return_value = {'510300': {'change_pct': 2.5}}
        with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2., '2026-11-04')), patch.object(
                sync, 'fetch_official_unit_nav', return_value=1.6804):
            updated, warnings, count = sync.sync_portfolio(original)
        self.assertEqual((warnings, count), ([], 0))
        self.assertEqual(updated['holdings'][0]['estimated_amount'], 20.55)
        with patch.object(sync, 'fetch_latest_cumulative_nav', return_value=(2.2, '2026-11-05')), patch.object(
                sync, 'fetch_official_unit_nav', side_effect=ConnectionError('单位净值断网')):
            failed, warnings, count = sync.sync_portfolio(updated)
        self.assertEqual((failed, count), (updated, 0))
        self.assertIn('单位净值断网', warnings[0])

    def test_shares_must_be_nonnegative_finite_and_optional(self):
        for shares in (-1., float('nan'), float('inf'), True, '11.93'):
            original = sample_portfolio()
            original['holdings'][0]['shares'] = shares
            with self.subTest(shares=shares), self.assertRaises(ValueError):
                state.validate_portfolio(original)
        for shares in (None, 0., 11.93):
            original = sample_portfolio()
            original['holdings'][0]['shares'] = shares
            state.validate_portfolio(original)


if __name__ == '__main__':
    unittest.main()
