"""Load the bundled engine in a private namespace.

The legacy scripts use absolute sibling imports (including ``statistics`` and
``data_loader``, names also used by Python/the old ledger). A module-local import
adapter resolves just those siblings, leaving third-party imports and sys.path
untouched. All runtime sources live in core; configuration lives at project root.
"""
import builtins
import importlib.util
from pathlib import Path
import sys
from threading import RLock

ROOT = Path(__file__).resolve().parent.parent
_NAMES = frozenset(('data_loader', 'fund_classifier', 'statistics',
                    'portfolio_analysis', 'stress_test', 'decision_data',
                    'decision_rules', 'position_sizing', 'report_generator'))
_LOCK = RLock()


def _import(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name in _NAMES:
        return load(name)
    return builtins.__import__(name, globals, locals, fromlist, level)


def load(name):
    if name not in _NAMES:
        raise ValueError(f'Unknown engine module: {name}')
    qualified = f'core._audited_{name}'
    with _LOCK:
        if qualified in sys.modules:
            return sys.modules[qualified]
        spec = importlib.util.spec_from_file_location(
            qualified, ROOT / 'core' / f'_engine_{name}.py')
        module = importlib.util.module_from_spec(spec)
        module.__dict__['__builtins__'] = {**vars(builtins), '__import__': _import}
        sys.modules[qualified] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(qualified, None)
            raise
        return module
