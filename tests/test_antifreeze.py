"""Deterministic regressions for UI-blocking provider calls (no live network)."""
import threading
import time
import unittest
from unittest.mock import patch

import pandas as pd
import numpy as np
from pathlib import Path
from tempfile import TemporaryDirectory

from core import fees
from core.engine import load
from core.network import bounded_call, network_budget
from core import ai_advisor as ai


class BlockingCalls(unittest.TestCase):
    def setUp(self):
        fees._get_fund_info_and_fees.clear()
        self.addCleanup(fees._get_fund_info_and_fees.clear)
        fees._fetch_fee_table.clear()
        fees._fetch_fund_catalog.clear()
        self.addCleanup(fees._fetch_fee_table.clear)
        self.addCleanup(fees._fetch_fund_catalog.clear)

    def test_single_fund_fees_returns_when_provider_never_responds(self):
        release = threading.Event()
        finished = threading.Event()

        def blocked(*args, **kwargs):
            release.wait(1)
            return pd.DataFrame()

        def fetch():
            try:
                fees.get_fund_info_and_fees('654321')
            finally:
                finished.set()

        with patch.object(fees.ak, 'fund_info_ths', side_effect=blocked), \
             patch.object(fees.ak, 'fund_fee_em', side_effect=blocked), \
             patch.object(fees, 'FEE_TIMEOUT_SEC', .03, create=True):
            worker = threading.Thread(target=fetch, daemon=True)
            worker.start()
            try:
                self.assertTrue(finished.wait(.3), '基金费率同步请求阻塞主调用，未在硬超时后返回')
            finally:
                release.set()
                worker.join(2)

    def test_whole_analysis_budget_does_not_restart_per_request(self):
        release = threading.Event()
        started = time.monotonic()
        try:
            with network_budget(.04):
                for i in range(8):
                    with self.assertRaisesRegex(TimeoutError, '网络超时'):
                        bounded_call(lambda: release.wait(1), key=('deadline-test', i))
            self.assertLess(time.monotonic() - started, .2)
        finally:
            release.set()

    def test_timed_out_work_is_reused_and_only_daemons_are_started(self):
        release = threading.Event()
        calls = []
        def blocked():
            calls.append(threading.current_thread().daemon)
            release.wait(1)
        try:
            for _ in range(3):
                with self.assertRaises(TimeoutError):
                    bounded_call(blocked, timeout=.01, key='single-flight-test')
            self.assertEqual(calls, [True])
        finally:
            release.set()

    def test_fee_success_is_fetched_once_and_returns_independent_copies(self):
        frame = pd.DataFrame([['管理费', '0.15%'], ['托管费', '0.05%']])
        with patch.object(fees.ak, 'fund_info_ths', return_value=frame) as fetch:
            first = fees.get_fund_info_and_fees('654320')
            first['mgmt'] = 1
            second = fees.get_fund_info_and_fees('654320')
        self.assertEqual(fetch.call_count, 1)
        self.assertAlmostEqual(second['mgmt'], .0015)

    def test_raw_net_timeout_falls_back_to_verified_cache_and_stops_more_calls(self):
        loader_module = load('data_loader')
        with TemporaryDirectory() as folder:
            def provider(command, **kwargs):
                Path(command[-1]).write_text(pd.DataFrame({'value': [42]}).to_json(orient='split'), encoding='utf-8')
                from types import SimpleNamespace
                return SimpleNamespace(returncode=0)
            with patch('subprocess.run', side_effect=provider):
                loader_module.DataLoader(folder).api('fund_overview_em', symbol='654319')
            loader_module.fetch_api_payload.clear()
            loader = loader_module.DataLoader(folder, refresh=True, timeout=.04)
            def stalled(*args, **kwargs):
                import subprocess
                time.sleep(kwargs['timeout'])
                raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
            started = time.monotonic()
            with patch('subprocess.run', side_effect=stalled) as fetch:
                data = loader.api('fund_overview_em', symbol='654319')
                with self.assertRaisesRegex(TimeoutError, '网络超时'):
                    loader.api('fund_name_em')
            self.assertEqual(data.value.iloc[0], 42)
            self.assertTrue(loader.calls[-1]['refresh_failed'])
            self.assertEqual(fetch.call_count, 1)
            self.assertLess(time.monotonic() - started, .3)

    def test_ai_wall_clock_timeout_returns_safe_error(self):
        release = threading.Event()
        try:
            with patch.object(ai, 'AI_TIMEOUT_SEC', .02), \
                 patch.object(ai, '_chat_request', side_effect=lambda *args: release.wait(1)):
                with self.assertRaises(TimeoutError) as raised:
                    ai._chat({}, [], 1)
            self.assertIn('超时', ai.summary_error(raised.exception, {}))
            self.assertEqual(ai.error_code(raised.exception), 'TIMEOUT')
        finally:
            release.set()

    def test_sdk_timeout_and_retry_policy_reach_http_transport(self):
        import httpx
        from openai import OpenAI
        def handler(request):
            self.assertTrue(all(value == 30.0 for value in request.extensions['timeout'].values()))
            return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})
        client_type = httpx.Client
        with patch('openai.OpenAI', side_effect=lambda **kw: OpenAI(**kw,
                http_client=client_type(transport=httpx.MockTransport(handler)))) as constructor:
            result = ai._chat({**ai.DEFAULT_SETTINGS, 'api_key': 'fake-key'}, [], 1)
        self.assertEqual(result, 'OK')
        self.assertEqual(constructor.call_args.kwargs['timeout'], 30.0)
        self.assertEqual(constructor.call_args.kwargs['max_retries'], 0)

    def test_vectorized_scan_matches_scalar_reference_without_network(self):
        rng = np.random.default_rng(41)
        returns = rng.normal(.0001, .01, (450, 3))
        covariance = np.cov(returns.T) * 252
        base_weights = np.array([.5, .25, .1])
        base = returns @ base_weights
        grid = [round(i * .001, 12) for i in range(501)]
        with patch('subprocess.run', side_effect=AssertionError('scan attempted network')):
            rows = load('position_sizing').scan_budget_paths(base, returns[:, 1], covariance,
                base_weights, 1, grid, .2, .16)
        for weight, row in zip(grid, rows):
            path = np.r_[1., np.cumprod(1 + (1-weight)*base + weight*returns[:, 1])]
            dd = -np.min(path / np.maximum.accumulate(path) - 1)
            exposure = base_weights * (1-weight)
            exposure[1] += weight
            vol = np.sqrt(exposure @ covariance @ exposure)
            self.assertAlmostEqual(row['portfolio_max_drawdown'], dd, places=12)
            self.assertAlmostEqual(row['portfolio_volatility'], vol, places=12)
            self.assertEqual(row['within_budget'], dd <= .2 and vol <= .16)


if __name__ == '__main__':
    unittest.main()
