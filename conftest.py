"""Keep pytest independent of private runtime configuration and local caches."""
from contextlib import ExitStack
from pathlib import Path
import shutil
from unittest.mock import patch

import pytest


@pytest.fixture(scope='session', autouse=True)
def isolated_public_config(tmp_path_factory):
    from core import analysis, engine, portfolio_sync, position_sizing, state

    project_root = Path(__file__).resolve().parent
    sandbox = tmp_path_factory.mktemp('public-config')
    config = sandbox / 'config'
    config.mkdir()
    for template in (project_root / 'config').glob('*.example.json'):
        shutil.copy2(template, config / template.name.replace('.example.json', '.json'))
    shutil.copy2(project_root / 'config/index_mapping.json', config / 'index_mapping.json')

    # engine.ROOT continues to locate actual Python sources. Only configuration
    # and data roots are redirected; examples are never loaded by business code.
    modules = [analysis, position_sizing, *(engine.load(name) for name in sorted(engine._NAMES))]
    with ExitStack() as stack:
        stack.enter_context(patch.object(state, 'CONFIG_DIR', config))
        stack.enter_context(patch.object(state, 'ENGINE_ROOT', sandbox))
        stack.enter_context(patch.object(portfolio_sync, 'ETF_MAPPING_PATH', config / 'intraday_etf_mapping.json'))
        for module in modules:
            if getattr(module, 'ROOT', None) == project_root:
                stack.enter_context(patch.object(module, 'ROOT', sandbox))
        yield
