"""Network diagnostics, isolated from the analysis engine and real credentials."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import os
from pathlib import Path
import socket
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

import httpx
from streamlit.testing.v1 import AppTest

from core import ai_advisor as ai
from core import state
from core import network_diagnostic as diagnostic


class NetworkDiagnostics(unittest.TestCase):
    def connection(self, handler):
        client_type = httpx.Client
        return patch.object(ai.httpx, 'Client', side_effect=lambda **kw: client_type(
            **kw, transport=httpx.MockTransport(handler)))

    def test_503_retries_twice_then_returns_guidance_without_chat(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(503, text='gateway overloaded')
        with self.connection(handler), patch('core.ai_advisor.time.sleep') as sleep, patch(
                'sys.stdout', new_callable=io.StringIO) as output:
            result = ai.test_connection('https://api.foxapi.cn/v1', 'fake-key', 'my-model')
        self.assertFalse(result['success'])
        self.assertEqual([(r.method, r.url.path) for r in calls], [('GET', '/v1/models')] * 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(call.args == (1.5,) for call in sleep.call_args_list))
        self.assertEqual(output.getvalue().count('gateway overloaded'), 3)
        self.assertIn('接口网关暂时不可用 (503)', result['msg'])
        self.assertIn('直接手动填写 Model ID 并保存', result['msg'])
        self.assertIn('HTTP 503: gateway overloaded', result['msg'])

    def test_503_can_recover_on_either_retry(self):
        for failures in (1, 2):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(503, text='busy') if len(calls) <= failures else httpx.Response(
                    200, json={'data': [{'id': 'my-model'}]})
            with self.subTest(failures=failures), self.connection(handler), patch(
                    'core.ai_advisor.time.sleep') as sleep:
                result = ai.test_connection('https://example.test/v1', 'fake-key')
            self.assertTrue(result['success'])
            self.assertEqual(result['models'], ['my-model'])
            self.assertEqual(len(calls), failures + 1)
            self.assertEqual(sleep.call_count, failures)

    def test_other_http_errors_do_not_retry_or_generate(self):
        for status in (401, 403, 404, 429, 500, 502, 504):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, text='request rejected')
            with self.subTest(status=status), self.connection(handler), patch(
                    'core.ai_advisor.time.sleep') as sleep:
                result = ai.test_connection('https://example.test/v1', 'fake-key', 'my-model')
            self.assertFalse(result['success'])
            self.assertIn(f'HTTP {status}', result['msg'])
            self.assertEqual([r.method for r in calls], ['GET'])
            sleep.assert_not_called()

    def test_proxy_environment_is_logged_with_credentials_redacted(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://proxy-user:proxy-password@127.0.0.1:7890',
                'HTTP_PROXY': 'http://127.0.0.1:7891'}), self.connection(
                lambda _: httpx.Response(200, json={'data': [{'id': 'my-model'}]})), patch(
                'sys.stdout', new_callable=io.StringIO) as output:
            self.assertTrue(ai.test_connection('https://example.test/v1', 'fake-key')['success'])
        for value in ('HTTPS_PROXY=', 'HTTP_PROXY=', '127.0.0.1:7890', '127.0.0.1:7891'):
            self.assertIn(value, output.getvalue())
        for secret in ('proxy-user', 'proxy-password', 'fake-key'):
            self.assertNotIn(secret, output.getvalue())

    def test_ui_can_save_manual_model_without_testing_and_after_503(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(state, 'CONFIG_DIR', Path(directory)):
            app = AppTest.from_string('from ui.ai_advisor import render_settings\nrender_settings()',
                                     default_timeout=10).run()
            app.text_input(key='ai_input_api_key').set_value('fake-key').run()
            app.text_input(key='ai_input_model_id').set_value('manual-model').run()
            with patch('ui.ai_advisor.test_connection') as connection:
                next(w for w in app.button if w.label == '保存配置').click().run()
                connection.assert_not_called()
            self.assertEqual(ai.load_settings(directory)['model_id'], 'manual-model')
            with self.connection(lambda _: httpx.Response(503, text='busy')), patch(
                    'core.ai_advisor.time.sleep'):
                app.button(key='ai_test_button').click().run()
            self.assertFalse(app.exception)
            self.assertIn('接口网关暂时不可用 (503)', app.error[0].value)
            app.text_input(key='ai_input_model_id').set_value('another-model').run()
            with patch('ui.ai_advisor.test_connection') as connection:
                next(w for w in app.button if w.label == '保存配置').click().run()
                connection.assert_not_called()
            self.assertEqual(ai.load_settings(directory)['model_id'], 'another-model')

    def test_timeout_types_and_proxy_guidance(self):
        for error in (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            def handler(request):
                raise error('private-detail fake-key', request=request)
            with self.subTest(error=error), self.connection(handler), patch(
                    'sys.stdout', new_callable=io.StringIO) as output:
                result = ai.test_connection('https://example.test/v1', 'fake-key')
            self.assertFalse(result['success'])
            self.assertIn(error.__name__, output.getvalue())
            self.assertIn('网络连接超时', result['msg'])
            self.assertIn('HTTP_PROXY', result['msg'])
            self.assertNotIn('fake-key', output.getvalue() + result['msg'])
            self.assertNotIn('private-detail', output.getvalue() + result['msg'])

    def test_dns_cause_is_logged_without_private_text(self):
        def handler(request):
            try:
                raise socket.gaierror(11001, 'private DNS details')
            except socket.gaierror as exc:
                raise httpx.ConnectError('private proxy details', request=request) from exc
        with self.connection(handler), patch('sys.stdout', new_callable=io.StringIO) as output:
            result = ai.test_connection('https://example.test/v1', 'fake-key')
        self.assertFalse(result['success'])
        for value in ('ConnectError', 'socket.gaierror', '11001'):
            self.assertIn(value, output.getvalue())
        self.assertNotIn('private', output.getvalue())

    def test_https_proxy_environment_is_used(self):
        targets = []
        class Proxy(BaseHTTPRequestHandler):
            def do_CONNECT(self):
                targets.append(self.path)
                self.send_error(502, 'simulated proxy failure')

            def log_message(self, *args):
                pass
        with HTTPServer(('127.0.0.1', 0), Proxy) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                proxy = f'http://127.0.0.1:{server.server_port}'
                with patch.dict(os.environ, {'HTTPS_PROXY': proxy, 'https_proxy': proxy,
                        'NO_PROXY': '', 'no_proxy': ''}), patch(
                        'sys.stdout', new_callable=io.StringIO) as output:
                    result = ai.test_connection('https://api.foxapi.cn/v1', 'fake-key')
                self.assertFalse(result['success'])
                self.assertEqual(targets, ['api.foxapi.cn:443'])
                self.assertIn('ProxyError', output.getvalue())
            finally:
                server.shutdown()
                thread.join(timeout=2)

    def test_30_second_timeout_reaches_transport_and_models_still_work(self):
        def handler(request):
            self.assertTrue(all(value == 30.0 for value in request.extensions['timeout'].values()))
            return httpx.Response(200, json={'data': [{'id': 'my-model'}]})
        with self.connection(handler):
            result = ai.test_connection('https://example.test/v1', 'fake-key')
        self.assertTrue(result['success'])
        self.assertEqual(result['models'], ['my-model'])

    def test_ui_timeout_displays_proxy_guidance_and_resets_loading(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(state, 'CONFIG_DIR', Path(directory)):
            app = AppTest.from_string('from ui.ai_advisor import render_settings\nrender_settings()',
                                     default_timeout=10).run()
            app.text_input(key='ai_input_api_key').set_value('fake-key').run()
            with patch('ui.ai_advisor.test_connection', side_effect=httpx.ConnectTimeout('private-detail')):
                app.button(key='ai_test_button').click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.session_state['is_testing'])
            self.assertIn('网络连接超时', app.error[0].value)
            self.assertIn('HTTP_PROXY', app.error[0].value)
            self.assertNotIn('private-detail', app.error[0].value)

    def test_standalone_script_response_timeout_dns_and_proxy_configuration(self):
        def timeout(request):
            raise httpx.ReadTimeout('simulated timeout', request=request)
        def dns_error(request):
            raise httpx.ConnectError('DNS failed', request=request) from socket.gaierror(11001, 'no host')
        cases = (
            (lambda _: httpx.Response(200, json={'data': []}), 0, 'HTTP 200'),
            (lambda _: httpx.Response(401, text='invalid key: fake-key'), 2, 'HTTP 401'),
            (timeout, 1, 'ReadTimeout'),
            (dns_error, 1, 'socket.gaierror'),
        )
        for handler, expected_exit, expected_output in cases:
            with self.subTest(expected=expected_output), patch.dict(
                    os.environ, {'FOXAPI_API_KEY': 'fake-key'}), self.connection(handler), patch(
                    'sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(diagnostic.main(), expected_exit)
            self.assertIn(expected_output, output.getvalue())
            self.assertNotIn('fake-key', output.getvalue())
        with patch.object(diagnostic.httpx, 'Client', side_effect=ValueError('invalid proxy')), patch(
                'sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(diagnostic.main(), 1)
        self.assertIn('客户端配置失败', output.getvalue())

    def test_standalone_exception_redacts_proxy_credentials(self):
        proxy = 'http://proxy-user:proxy-password@127.0.0.1:7890'
        with patch.dict(os.environ, {'HTTPS_PROXY': proxy}), patch(
                'sys.stdout', new_callable=io.StringIO) as output:
            diagnostic.report_exception(httpx.ProxyError(f'{proxy} fake-key'), 'fake-key')
        self.assertIn('ProxyError', output.getvalue())
        for secret in ('fake-key', 'proxy-user', 'proxy-password'):
            self.assertNotIn(secret, output.getvalue())
