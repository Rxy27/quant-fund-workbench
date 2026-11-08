"""Validated JSON persistence. Amounts are account inputs, never rule parameters."""
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
import tempfile

from .engine import ROOT as ENGINE_ROOT, load
from .portfolio_sync import initialize_baselines

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / 'config'


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    path = Path(path)
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         suffix='.tmp', delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def default_portfolio():
    settings = read_json(ENGINE_ROOT / 'config/portfolio.json')
    total = read_json(CONFIG_DIR / 'ui_defaults.json')['initial_account_total']
    costs = read_json(ENGINE_ROOT / 'config/holdings.json').get('cost_nav', {})
    return {'account_total': total, 'holdings': [
        {**{k: v for k, v in holding.items() if k != 'weight'}, 'amount': total * holding['weight'],
         'cost_nav': costs.get(holding['code'])}
        for holding in settings['holdings'] if holding['code'] != 'CASH']}


def portfolio_cash(value):
    """Subtract decimal monetary inputs without inventing a negative cent."""
    return float(Decimal(str(value['account_total'])) - sum(
        (Decimal(str(h['amount'])) for h in value['holdings']), Decimal(0)))


def validate_portfolio(value):
    total = value['account_total']
    if not math.isfinite(total) or total < 0:
        raise ValueError('账户总额必须为非负的有限金额')
    codes = []
    for holding in value['holdings']:
        code, amount = holding['code'], holding['amount']
        if not re.fullmatch(r'\d{6}', code):
            raise ValueError('基金代码必须为6位数字，现金按账户剩余金额计算')
        if not math.isfinite(amount) or amount < 0:
            raise ValueError('持仓金额必须为非负有限数')
        cost = holding.get('cost_nav')
        if cost is not None and (not math.isfinite(cost) or cost <= 0):
            raise ValueError('买入成本净值须为正数；未填写请留空')
        shares = holding.get('shares')
        if shares is not None and (isinstance(shares, bool) or
                not isinstance(shares, (int, float)) or not math.isfinite(shares) or shares < 0):
            raise ValueError('持有份额必须为非负有限数；未填写请留空')
        calibrated = holding.get('is_calibrated', True)
        if not isinstance(calibrated, bool):
            raise ValueError('校准状态必须为布尔值')
        estimate = holding.get('estimated_amount')
        if estimate is not None and (isinstance(estimate, bool) or
                not isinstance(estimate, (int, float)) or not math.isfinite(estimate) or estimate < 0):
            raise ValueError('预估金额必须为非负有限数')
        if not calibrated and estimate is None:
            raise ValueError('待校准持仓必须包含预估金额')
        codes.append(code)
    if len(set(codes)) != len(codes):
        raise ValueError('基金代码不能重复')
    if portfolio_cash(value) < 0:
        raise ValueError('持仓金额合计不能超过账户总额，请先调整账户总额')


def portfolio_settings(value):
    validate_portfolio(value)
    settings = read_json(ENGINE_ROOT / 'config/portfolio.json')
    total = value['account_total']
    settings['holdings'] = [
        {k: v for k, v in {**h, 'weight': h['amount'] / total if total else 0.0}.items()
         if k not in ('amount', 'cost_nav', 'shares', 'base_unit_nav',
                      'base_nav', 'base_date', 'is_calibrated', 'estimated_amount')}
        for h in value['holdings']]
    settings['holdings'].append({'code': 'CASH', 'weight': portfolio_cash(value) / total if total else 1.0})
    load('portfolio_analysis').validate_portfolio(settings)
    return settings


def save_portfolio(value, directory=None, *, rebase=False):
    validate_portfolio(value)
    path = Path(directory or CONFIG_DIR) / 'user_portfolio.json'
    # Persist entered amounts only. Weights are derived from the current account
    # total on every analysis, so an old migrated weight must never go stale.
    value = {**value, 'holdings': [{k: v for k, v in holding.items()
                                  if k != 'weight' and not (k == 'shares' and v is None)}
                                 for holding in value['holdings']]}
    if rebase:
        previous = read_json(path) if path.exists() else {'holdings': []}
        old = {h['code']: h for h in previous['holdings']}
        for holding in value['holdings']:
            original = old.get(holding['code'])
            if original is None or original['amount'] != holding['amount']:
                holding.pop('base_nav', None)
                holding.pop('base_date', None)
                holding.pop('base_unit_nav', None)
                holding['is_calibrated'] = True
                holding.pop('estimated_amount', None)
    value = initialize_baselines(value)
    for holding in value['holdings']:
        holding['amount'] = round(holding['amount'], 2)
    validate_portfolio(value)
    write_json(path, value)
    return value


def save_risk(value, directory=None):
    parameters = read_json(ENGINE_ROOT / 'config/rule_parameters.json')
    load('position_sizing').validate_decision_config({**value, **parameters})
    write_json(Path(directory or CONFIG_DIR) / 'risk_budget.json', value)


def load_state(directory=None):
    directory = Path(directory or CONFIG_DIR)
    if not (directory / 'user_portfolio.json').exists():
        save_portfolio(default_portfolio(), directory)
    if not (directory / 'risk_budget.json').exists():
        save_risk(read_json(ENGINE_ROOT / 'config/risk_budget.json'), directory)
    portfolio = read_json(directory / 'user_portfolio.json')
    validate_portfolio(portfolio)
    upgraded = initialize_baselines(portfolio)
    if upgraded != portfolio:
        validate_portfolio(upgraded)
        write_json(directory / 'user_portfolio.json', upgraded)
        portfolio = upgraded
    risk = read_json(directory / 'risk_budget.json')
    load('position_sizing').load_decision_config(directory / 'risk_budget.json')
    return portfolio, risk


def reset_state(directory=None):
    directory = Path(directory or CONFIG_DIR)
    save_portfolio(default_portfolio(), directory)
    save_risk(read_json(ENGINE_ROOT / 'config/risk_budget.json'), directory)
    return load_state(directory)
