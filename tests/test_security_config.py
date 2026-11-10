"""Validate deploy defaults and keep dependency ranges aligned with the lock."""
from importlib.metadata import version
from pathlib import Path
import subprocess
import tomllib

from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parent.parent


def test_locked_environment_satisfies_direct_dependency_ranges():
    for line in (ROOT / 'requirements.txt').read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        requirement = Requirement(line)
        if requirement.marker is None or requirement.marker.evaluate():
            assert version(requirement.name) in requirement.specifier, requirement


def test_streamlit_defaults_only_expose_loopback_with_csrf_protection():
    settings = tomllib.loads((ROOT / '.streamlit/config.toml').read_text())['server']
    assert settings['address'] == '127.0.0.1'
    assert settings['enableCORS'] is True
    assert settings['enableXsrfProtection'] is True
    assert settings['enableStaticServing'] is False


def test_git_ignores_new_private_configs_but_keeps_public_templates():
    private = ['config/future-private.json', 'config/ai_settings.json', '.env',
               '.env.production', '.streamlit/secrets.toml']
    public = ['config/future.example.json', 'config/index_mapping.json',
              '.streamlit/secrets.example.toml']
    result = subprocess.run(['git', 'check-ignore', '--no-index', *private, *public],
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.splitlines()) == set(private)
