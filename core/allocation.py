"""Monthly order eligibility and immediate volatility-scaled risk budgets.

Account drawdown belongs to the independent execution RiskGate. This module
keeps no account peaks, recovery counters, or emergency trading overrides.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .risk_model import ewma_shrunk_covariance, risk_budget_weights

UNIVERSE = ('510300', '510500', '511010', '518880')


@dataclass
class RiskState:
    last_date: object = None


def is_rebalance_day(observed_dates):
    """First observed close of a new month, established without future dates.

    Two observations are required; starting mid-month is not an entry exception.
    """
    dates = pd.DatetimeIndex(observed_dates)
    if dates.has_duplicates or not dates.is_monotonic_increasing or dates.hasnans:
        raise ValueError('行情必须按唯一交易日期递增')
    return len(dates) >= 2 and dates[-1].to_period('M') != dates[-2].to_period('M')


def leverage_control(unit_volatility, target_volatility, max_gross):
    """Symmetric, immediate scaling of the newly computed unit portfolio."""
    numbers = (unit_volatility, target_volatility, max_gross)
    if not np.isfinite(numbers).all() or unit_volatility < 0 or min(target_volatility, max_gross) <= 0:
        raise ValueError('波动率须为有效非负数，目标与仓位上限须为正')
    return float(min(max_gross, target_volatility/max(unit_volatility, 1e-12)))


def allocation_target(history, actual_weights, equity, config, state):
    """Compute targets at close; allow orders only on the monthly signal date.

    The equity argument remains for caller compatibility but never changes weights.
    The caller executes eligible signals at the next observed open.
    """
    from .decision_rules import rotation_budgets
    if config.get('require_full_etf_pool', True) and set(history.columns).intersection(UNIVERSE) and not set(UNIVERSE).issubset(history.columns):
        raise ValueError('四资产池不完整；不得移除破位或零持仓资产')
    scheduled = is_rebalance_day(history.index)
    if not np.isfinite(history.to_numpy()).all() or (history <= 0).any().any():
        raise ValueError('资产池行情不完整；不得补零或移除资产')
    lookback = config.get('covariance_days', 60)
    need = max(config.get('trend_days', 200), config.get('momentum_days', 126)+1, lookback+1)
    if len(history) < need:
        raise ValueError(f'策略至少需要{need}个共同交易日')
    date = history.index[-1]
    if state.last_date is not None and date < state.last_date:
        raise ValueError('交易日期不可倒退')
    actual = pd.Series(actual_weights, dtype=float).reindex(history.columns, fill_value=0.)
    if not np.isfinite(actual).all() or (actual < 0).any():
        raise ValueError('实际权重须为非负有限数')
    returns = history.iloc[-lookback-1:].pct_change(fill_method=None).iloc[1:]
    downside = ewma_shrunk_covariance(returns, annualization=config.get('annualization_days', 252))
    total = ewma_shrunk_covariance(returns, downside=False, annualization=config.get('annualization_days', 252))
    scores, budgets = rotation_budgets(history, config)
    weights = risk_budget_weights(downside, budgets.to_numpy())
    unit_volatility = float(np.sqrt(weights@total@weights))
    actual_volatility = float(np.sqrt(actual.to_numpy()@total@actual.to_numpy()))
    gross = leverage_control(unit_volatility, config['portfolio_target_volatility'],
                             config.get('max_gross', .95))
    target = pd.Series(weights*gross, index=history.columns)
    eligible = scheduled and state.last_date != date
    execution = target.copy() if eligible else actual.copy()
    state.last_date = date
    contributions = weights*(downside@weights)
    return execution, {'gross': gross, 'actual_volatility': actual_volatility,
        'unit_volatility': unit_volatility, 'message': '',
        'signal_date': str(date.date()), 'scheduled_rebalance': bool(scheduled),
        'target_weights': target.to_dict(), 'execution_weights': execution.to_dict(),
        'raw_scores': scores.to_dict(), 'risk_budgets': budgets.to_dict(),
        'risk_contribution_fraction': dict(zip(history.columns, (contributions/contributions.sum()).tolist())),
        'rebalance': bool(eligible and (execution-actual).abs().max() > 1e-12)}


def portfolio_controls(fund_data, portfolio, config):
    """Stateless display of current targets and the observed monthly calendar."""
    from .position_sizing import portfolio_panel
    panel, _, error = portfolio_panel(fund_data, portfolio)
    if error or len(panel) < config['min_data_days_required']:
        return None
    codes = list(dict.fromkeys([h['code'] for h in portfolio['holdings'] if h['code'] != 'CASH' and h['weight'] > 0] + [fund_data['code']]))
    navs = {**portfolio.get('navs', {}), fund_data['code']: fund_data['nav']}
    if any(code not in navs or len(navs[code]) < 2 for code in codes):
        return None
    from .engine import load
    history = load('statistics').aligned_navs({c: navs[c] for c in codes}, portfolio.get('calendar_days', 252))
    actual = {c: sum(h['weight'] for h in portfolio['holdings'] if h['code'] == c) for c in codes}
    settings = {**config, 'trend_days': config['trend_ma_days'], 'require_full_etf_pool': False}
    need = max(settings['trend_days'], settings.get('momentum_days', 126)+1, settings.get('covariance_days', 60)+1)
    if len(history) < need:
        return None
    _, details = allocation_target(history, actual, None, settings, RiskState())
    return {**details, 'source': '共同已观察交易日行情；每月首个交易日收盘决策，下一交易日开盘执行'}
