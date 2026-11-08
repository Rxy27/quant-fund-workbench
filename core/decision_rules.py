"""Compatible condition records with risk budgets and monthly rebalancing."""
import pandas as pd

from .engine import load
from .allocation import is_rebalance_day, portfolio_controls
from .valuation import evaluate_b2_condition
from .fees import buy_condition_6
from .alternatives import sell_condition_5

_legacy = load('decision_rules')
buy_condition_3 = _legacy.buy_condition_3
sell_condition_1 = _legacy.sell_condition_1
sell_condition_2 = _legacy.sell_condition_2
sell_condition_4 = _legacy.sell_condition_4


def buy_condition_2(fund_data, portfolio, config) -> dict:
    if fund_data.get('category') != '指数型':
        return _legacy.buy_condition_2(fund_data, portfolio, config)
    evaluation = evaluate_b2_condition(
        fund_data['code'], offline=config.get('_valuation_offline', False))
    evidence = {key: evaluation[key] for key in
                ('percentile', 'current_pe', 'data_date', 'observations') if key in evaluation}
    evidence.update(threshold=.3, neutral_threshold=.5, window_observations=1250,
                    summary=evaluation.get('evidence', ''))
    return _legacy.result(evaluation['status'], evaluation['reason'], evidence,
                          evaluation.get('data_source', 'AkShare:乐咕指数滚动市盈率'))


def rotation_budgets(history, config):
    """All assets stay eligible; scores are preferences, not capital-weight caps."""
    momentum_days = config.get('momentum_days', 126)
    trend_days = config.get('trend_days', 200)
    if len(history) < max(momentum_days+1, trend_days):
        raise ValueError('动量/均线历史不足')
    momentum = history.iloc[-1]/history.iloc[-momentum_days-1]-1
    above = history.iloc[-1] >= history.iloc[-trend_days:].mean()
    scores = pd.Series(25., index=history.columns)
    scores.loc[(momentum > 0) & above] = 32.5
    scores.loc[~above] = 7.5
    return scores, scores/scores.sum()


def _controls(fund_data, portfolio, config):
    if '_risk_controls' in portfolio:
        return portfolio['_risk_controls']
    return portfolio_controls(fund_data, portfolio, config)


def _with_strategy(record, fund_data, portfolio, config):
    controls = _controls(fund_data, portfolio, config)
    if controls:
        record['evidence'] = {**record['evidence'], 'strategy': controls}
    return record


def buy_condition_1(fund_data, portfolio, config) -> dict:
    return _with_strategy(_legacy.buy_condition_1(fund_data, portfolio, config), fund_data, portfolio, config)


def buy_condition_4(fund_data, portfolio, config) -> dict:
    return _with_strategy(_legacy.buy_condition_4(fund_data, portfolio, config), fund_data, portfolio, config)


def _rebalance(fund_data, portfolio, config, selling):
    holdings = portfolio.get('holdings', [])
    controls = _controls(fund_data, portfolio, config)
    code = fund_data['code']
    held = next((h for h in holdings if h['code'] == code), {})
    role = fund_data.get('exposure_role') or fund_data.get('role')
    target = (controls or {}).get('target_weights', {}).get(code,
        held.get('target_weight', config['role_target_weights'].get(role)))
    actual = sum(h['weight'] for h in holdings if h['code'] == code)
    evidence = {'schedule': '每月第一个已观察交易日', 'actual': actual, 'target': target, 'role': role}
    if not selling:
        evidence['candidate_role'] = fund_data.get('role')
    if not holdings or not _legacy.finite(target):
        return _legacy.result('无法判定', '实际持仓或目标权重不可得', evidence)
    nav = fund_data.get('nav')
    if controls:
        scheduled = controls['scheduled_rebalance']
    elif nav is not None and len(nav) >= 2:
        scheduled = is_rebalance_day(nav.index)
    else:
        return _legacy.result('无法判定', '缺少已观察交易日期，无法确认月度调仓日', evidence)
    gap = float(target)-actual
    triggered = scheduled and (-gap if selling else gap) > 1e-12
    evidence.update(gap=gap, boundary=float(target), scheduled_rebalance=bool(scheduled))
    if controls:
        evidence['strategy'] = controls
    status = ('触发' if triggered else '未触发') if selling else ('满足' if triggered else '不满足')
    reason = ('非月度调仓日，不生成订单；等待每月第一个已观察交易日' if not scheduled else
              '月度调仓日：按最新目标权重调仓，下一交易日开盘执行' if triggered else
              '月度调仓日：该方向无需调整仓位')
    return _legacy.result(status, reason, evidence)


def buy_condition_5(fund_data, portfolio, config) -> dict:
    return _rebalance(fund_data, portfolio, config, False)


def sell_condition_3(fund_data, portfolio, config) -> dict:
    return _rebalance(fund_data, portfolio, config, True)


BUY_FUNCTIONS = (buy_condition_1, buy_condition_2, buy_condition_3, buy_condition_4, buy_condition_5, buy_condition_6)
SELL_FUNCTIONS = (sell_condition_1, sell_condition_2, sell_condition_3, sell_condition_4, sell_condition_5)


def evaluate_conditions(fund_data, portfolio, config):
    portfolio = {**portfolio, '_risk_controls': _controls(fund_data, portfolio, config)}
    def rows(functions, labels, prefix):
        return [{'id': f'{prefix}{i}', 'label': label, **function(fund_data, portfolio, config)}
                for i, (function, label) in enumerate(zip(functions, labels), 1)]
    return {'buy': rows(BUY_FUNCTIONS, _legacy.BUY_LABELS, 'B'),
            'sell': rows(SELL_FUNCTIONS, _legacy.SELL_LABELS, 'S')}
