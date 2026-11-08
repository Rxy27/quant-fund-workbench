"""The standalone ledger tests referenced private seeded transactions.

They are outside the public Streamlit workbench. Financial regressions for this
release live in test_sandbox_engine.py and test_portfolio_sync.py, using
synthetic cashflows and temporary storage.
"""
import unittest

raise unittest.SkipTest("Private standalone-ledger fixtures are excluded from the public release")
