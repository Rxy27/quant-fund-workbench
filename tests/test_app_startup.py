"""Exercise the shipped entry point with the isolated public configuration."""
from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_empty_account_startup_and_sandbox_navigation():
    app = AppTest.from_file(str(Path(__file__).resolve().parent.parent / 'app.py'),
                            default_timeout=30).run()
    assert not app.exception
    assert not app.error
    assert app.number_input(key='account_total').value == 0
    app.radio(key='workspace_mode').set_value('历史沙盘推演').run()
    assert not app.exception
    assert not app.error
