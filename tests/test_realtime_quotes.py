"""Deterministic provider, cooldown and concurrency contracts; no live requests."""
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from core import market_data as market


def em_frame(codes, *, rows=100):
    codes = list(codes)
    return pd.DataFrame({'代码': codes + ['999999'] * max(0, rows - len(codes)),
                         '名称': ['测试标的'] * max(rows, len(codes)),
                         '最新价': [10.] * max(rows, len(codes)),
                         '涨跌幅': [1.5] * max(rows, len(codes))})


def xq_frame(price=10.):
    return pd.DataFrame({'item': ['current', 'percent', 'name'],
                         'value': [price, 1.5, '测试股票']})


class RealtimeQuotesTests(unittest.TestCase):
    def setUp(self):
        market._last_request_time.clear()
        market._inflight_codes.clear()
        self.clock = SimpleNamespace(time=Mock(return_value=1000.), sleep=Mock())
        self.time_patch = patch.object(market, 'time', self.clock)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.provider = Mock()
        self.provider_patch = patch.object(market, 'ak', self.provider)
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)
        self.direct_original = market._fetch_etf_quotes
        self.direct_patch = patch.object(market, '_fetch_etf_quotes', return_value={})
        self.direct = self.direct_patch.start()
        self.addCleanup(self.direct_patch.stop)

    def test_targeted_etfs_skip_full_market_download_and_keep_cooldown(self):
        self.direct.return_value = {'510300': market._quote_record('ETF', 4., -1.69)}
        self.assertEqual(market.get_realtime_quotes(['510300'])['510300']['change_pct'], -1.69)
        self.assertEqual(market.get_realtime_quotes(['510300']), {})
        self.direct.assert_called_once_with(['510300'])
        self.provider.fund_etf_spot_em.assert_not_called()
        self.provider.stock_zh_a_spot_em.assert_not_called()

    def test_targeted_etf_parser_uses_percent_and_rejects_dirty_ticks(self):
        fields = [''] * 33
        fields[1], fields[2], fields[3], fields[30], fields[32] = '测试ETF', '510300', '4.124', '20261106150000', '-1.69'
        response = Mock(text='v_sh510300="' + '~'.join(fields) + '";')
        with patch.object(market.requests, 'get', return_value=response) as fetch:
            quotes = self.direct_original(['510300'])
        self.assertEqual(quotes['510300']['price'], 4.124)
        self.assertEqual(quotes['510300']['change_pct'], -1.69)
        self.assertEqual(quotes['510300']['market_time'], '2026-11-06 15:00:00')
        self.assertEqual(fetch.call_args.kwargs['timeout'], (1.5, 2.5))
        fields[3] = 'nan'
        response.text = 'v_sh510300="' + '~'.join(fields) + '";'
        with patch.object(market.requests, 'get', return_value=response):
            self.assertEqual(self.direct_original(['510300']), {})

    def test_etfs_batch_once_and_cooldown_for_three_minutes(self):
        codes = ['510300', '159915']
        self.provider.fund_etf_spot_em.return_value = em_frame(codes)
        requested = []
        for second in range(0, 181, 30):
            self.clock.time.return_value = 1000. + second
            result = market.get_realtime_quotes(codes + codes)
            if result:
                requested.append(second)
                self.assertEqual(set(result), set(codes))
                self.assertEqual(set(result[codes[0]]), {'name', 'price', 'change_pct', 'update_time'})
        self.assertEqual(requested, [0, 60, 120, 180])
        self.assertEqual(self.provider.fund_etf_spot_em.call_count, 4)
        self.provider.stock_individual_spot_xq.assert_not_called()
        self.provider.stock_zh_a_spot_em.assert_not_called()

    def test_truncation_and_all_failed_stocks_share_one_fallback(self):
        codes = ['510300', '159915', '688981', '600000', '000001']
        self.provider.fund_etf_spot_em.return_value = em_frame(['510300'], rows=1)
        self.provider.stock_individual_spot_xq.side_effect = RuntimeError('blocked')
        self.provider.stock_zh_a_spot_em.return_value = em_frame(codes)
        result = market.get_realtime_quotes(codes)
        self.assertEqual(set(result), set(codes))
        self.assertEqual(result['159915']['price'], 10.)
        self.provider.fund_etf_spot_em.assert_called_once()
        self.provider.stock_zh_a_spot_em.assert_called_once()
        self.assertEqual(self.provider.stock_individual_spot_xq.call_count, 3)

    def test_stocks_run_concurrently_with_at_most_two_workers(self):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active, peak, threads = 0, 0, set()

        def provider(**kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                threads.add(threading.get_ident())
            try:
                barrier.wait(timeout=2)
                return xq_frame()
            finally:
                with lock:
                    active -= 1

        self.provider.stock_individual_spot_xq.side_effect = provider
        result = market.get_realtime_quotes(['688981', '600000', '000001', '300750'])
        self.assertEqual(len(result), 4)
        self.assertEqual(peak, 2)
        self.assertEqual(len(threads), 2)
        self.assertEqual(self.clock.sleep.call_args_list, [((0.2,),)] * 4)
        self.provider.stock_zh_a_spot_em.assert_not_called()

    def test_exact_symbol_prefixes(self):
        expected = {'600000': 'SH600000', '688981': 'SH688981', '510300': 'SH510300',
                    '588000': 'SH588000', '000001': 'SZ000001', '300750': 'SZ300750',
                    '159915': 'SZ159915', '830799': 'BJ830799', '920001': '920001',
                    'SH600000': 'SH600000'}
        for code, symbol in expected.items():
            self.assertEqual(market._xq_symbol(code), symbol)
        self.provider.stock_individual_spot_xq.return_value = xq_frame()
        result = market.get_realtime_quotes(['688981'])
        self.assertEqual(result['688981']['change_pct'], 1.5)
        self.provider.stock_individual_spot_xq.assert_called_once_with(symbol='SH688981')

    def test_xueqiu_english_wide_and_localized_item_adapters(self):
        for frame in [xq_frame(), pd.DataFrame([dict(current=10., percent=1.5, name='测试股票')]),
                      pd.DataFrame({'item': ['现价', '涨幅', '名称'], 'value': [10., 1.5, '测试股票']})]:
            market._last_request_time.clear()
            self.provider.stock_individual_spot_xq.return_value = frame
            self.assertEqual(market.get_realtime_quotes(['688981'])['688981']['price'], 10.)
        self.provider.stock_zh_a_spot_em.assert_not_called()

    def test_partial_success_survives_failed_fallback_and_malformed_rows(self):
        self.provider.stock_individual_spot_xq.side_effect = [xq_frame(), pd.DataFrame()]
        self.provider.stock_zh_a_spot_em.side_effect = OSError('offline')
        self.assertEqual(set(market.get_realtime_quotes(['688981', '600000'])), {'688981'})
        self.provider.stock_zh_a_spot_em.assert_called_once()
        self.assertEqual(market.get_realtime_quotes(['600000']), {})

    def test_invalid_rows_and_missing_columns_never_raise(self):
        codes = ['510300', '159915']
        frame = em_frame(codes)
        frame.loc[0, '最新价'] = float('nan')
        self.provider.fund_etf_spot_em.return_value = frame
        self.provider.stock_zh_a_spot_em.return_value = pd.DataFrame({'unexpected': []})
        self.assertEqual(set(market.get_realtime_quotes(codes)), {'159915'})
        self.assertEqual(market.get_realtime_quotes(None), {})
        self.assertEqual(market.get_realtime_quotes([]), {})

    def test_overlapping_callers_skip_inflight_and_do_not_stamp_queued_codes(self):
        entered, release = threading.Event(), threading.Event()

        def provider(**kwargs):
            entered.set()
            release.wait(3)
            return xq_frame()

        self.provider.stock_individual_spot_xq.side_effect = provider
        codes = ['688981', '600000', '000001']
        with ThreadPoolExecutor(max_workers=1) as callers:
            first = callers.submit(market.get_realtime_quotes, codes)
            try:
                self.assertTrue(entered.wait(2))
                self.assertNotIn('000001', market._last_request_time)
                self.assertEqual(market.get_realtime_quotes(codes), {})
            finally:
                release.set()
            self.assertEqual(len(first.result(timeout=3)), 3)
        self.assertEqual(self.provider.stock_individual_spot_xq.call_count, 3)


if __name__ == '__main__':
    unittest.main()
