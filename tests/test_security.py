"""Security regressions: credentials stay off disk/browser and paths stay bounded."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest

from core import ai_advisor as ai, state, valuation
from core.sandbox_engine import load_cumulative_nav


class CredentialSecurity(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {name: '' for name in (
            'QUANT_AI_API_KEY', 'OPENAI_API_KEY', 'QUANT_AI_BASE_URL', 'QUANT_AI_MODEL_ID')})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.secrets = patch.object(st, 'secrets', {})
        self.secrets.start()
        self.addCleanup(self.secrets.stop)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.config = patch.object(state, 'CONFIG_DIR', self.root)
        self.config.start()
        self.addCleanup(self.config.stop)

    def app(self):
        app = AppTest.from_string(
            "from ui.ai_advisor import render_settings\n"
            "import streamlit as st\n"
            "st.session_state['result'] = render_settings()", default_timeout=10).run()
        self.assertFalse(app.exception)
        return app

    def test_save_never_persists_key_and_legacy_key_is_ignored(self):
        saved = ai.save_settings({**ai.DEFAULT_SETTINGS, 'api_key': 'test-session-credential'})
        self.assertEqual(saved['api_key'], 'test-session-credential')
        content = (self.root / 'ai_settings.json').read_text(encoding='utf-8')
        self.assertNotIn('test-session-credential', content)
        self.assertEqual(json.loads(content)['api_key'], '')
        state.write_json(self.root / 'ai_settings.json', saved)
        self.assertEqual(ai.load_settings()['api_key'], '')

    def test_environment_precedence_over_secrets_and_legacy_config(self):
        state.write_json(self.root / 'ai_settings.json',
                         {**ai.DEFAULT_SETTINGS, 'api_key': 'test-legacy'})
        with patch.object(st, 'secrets', {'ai': {'api_key': 'test-secret',
                'base_url': 'https://secret.example/v1'}}):
            self.assertEqual(ai.load_settings()['api_key'], 'test-secret')
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-openai'}):
                self.assertEqual(ai.load_settings()['api_key'], 'test-openai')
                with patch.dict(os.environ, {'QUANT_AI_API_KEY': 'test-quant',
                        'QUANT_AI_BASE_URL': 'https://environment.example/v1'}):
                    settings = ai.load_settings()
                    self.assertEqual(settings['api_key'], 'test-quant')
                    self.assertEqual(settings['base_url'], 'https://environment.example/v1')

    def test_missing_secrets_file_is_optional(self):
        with patch.object(st, 'secrets') as secrets:
            secrets.get.side_effect = FileNotFoundError('no secrets')
            self.assertEqual(ai.load_settings(), ai.DEFAULT_SETTINGS)

    def test_server_key_stays_off_browser_and_destination_cannot_be_changed(self):
        with patch.dict(os.environ, {'QUANT_AI_API_KEY': 'test-server-only',
                'QUANT_AI_BASE_URL': 'https://trusted.example/v1'}):
            app = self.app()
            self.assertEqual(app.text_input(key='ai_input_api_key').value, '')
            self.assertTrue(app.text_input(key='ai_input_api_key').disabled)
            self.assertTrue(app.text_input(key='ai_input_base_url').disabled)
            # Even a forged widget state cannot redirect the managed key.
            app.session_state['ai_input_base_url'] = 'https://untrusted.example/v1'
            with patch('ui.ai_advisor.test_connection', return_value={
                    'success': False, 'msg': 'simulated'}) as connection:
                app.button(key='ai_test_button').click().run()
                connection.assert_called_once_with('https://trusted.example/v1',
                                                   'test-server-only', 'gpt-4o-mini')
            app.button(key='btn_save_ai').click().run()
            self.assertNotIn('test-server-only', (self.root / 'ai_settings.json').read_text())
        app.run()
        self.assertEqual(app.session_state['result']['api_key'], '')

    def test_temporary_key_survives_rerun_but_isolated_from_another_session(self):
        app = self.app()
        app.text_input(key='ai_input_api_key').set_value('test-session-only').run()
        app.button(key='btn_save_ai').click().run()
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['result']['api_key'], 'test-session-only')
        self.assertEqual(ai.load_settings()['api_key'], '')
        other = self.app()
        self.assertEqual(other.text_input(key='ai_input_api_key').value, '')
        self.assertEqual(other.session_state['result']['api_key'], '')

    def test_invalid_url_never_reaches_network(self):
        for url in ('http://example.com/v1', 'https://user:password@example.com',
                    'file:///etc/passwd', 'https://example.com/?api_key=private'):
            with self.subTest(url=url), patch.object(ai.httpx, 'Client') as client:
                self.assertFalse(ai.test_connection(url, 'test-only')['success'])
                client.assert_not_called()


class FileSecurity(unittest.TestCase):
    def test_traversal_rejected_before_filesystem_or_network_access(self):
        for code in ('../123456', '000001/../../private', '..\\private', '/etc/passwd',
                     '１２３４５６', '123456\n', None):
            with self.subTest(code=code), patch.object(valuation.pd, 'read_parquet') as read, patch.object(
                    valuation, 'bounded_call') as fetch, patch.object(Path, 'mkdir') as mkdir:
                with self.assertRaises(ValueError):
                    valuation.get_valuation_data('index', code, offline=True)
                with self.assertRaises(ValueError):
                    load_cumulative_nav(code, offline=True)
                read.assert_not_called()
                fetch.assert_not_called()
                mkdir.assert_not_called()


if __name__ == '__main__':
    unittest.main()
