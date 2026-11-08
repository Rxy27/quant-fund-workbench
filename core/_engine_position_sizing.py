"""Historical fixed-weight combination scan; no return objective or trading engine."""
import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd

from data_loader import ROOT
from statistics import aligned_returns


def load_decision_config(risk_path=None, parameters_path=None):
    risk_path = Path(risk_path or ROOT / 'config/risk_budget.json')
    parameters_path = Path(parameters_path or ROOT / 'config/rule_parameters.json')
    config = json.loads(risk_path.read_text(encoding='utf-8-sig'))
    parameters = json.loads(parameters_path.read_text(encoding='utf-8-sig'))
    # Only a missing user tolerance falls back to the canonical default file.
    used_default = 'portfolio_max_drawdown_tolerance' not in config
    if used_default:
        defaults = json.loads((ROOT / 'config/risk_budget.json').read_text(encoding='utf-8'))
        config['portfolio_max_drawdown_tolerance'] = defaults['portfolio_max_drawdown_tolerance']
    config.update(parameters)
    config['used_default_drawdown_tolerance'] = used_default
    validate_decision_config(config)
    return config


def validate_decision_config(config):
    if not re.fullmatch(r'[0-9]{6}', str(config.get('reference_fund', '110020'))):
        raise ValueError('reference_fund 参照基金代码必须为6位数字字符串')
    for name, value in config.items():
        if isinstance(value, (int, float)) and (not math.isfinite(value) or value < 0):
            raise ValueError(f'{name}必须为非负有限数')
    for name in ('trend_ma_days', 'correlation_window_days', 'annualization_days', 'valuation_history_years', 'min_data_days_required', 'min_ma_days_required', 'active_fund_manager_change_months'):
        if not isinstance(config[name], int) or config[name] < 1:
            raise ValueError(f'{name}必须为正整数')
    for name in ('scan_step', 'scan_upper_limit', 'single_fund_max_weight'):
        if not 0 < config[name] <= 1:
            raise ValueError(f'{name}必须在(0,1]内')
    if config['correlation_window_days'] < 2 or config['min_data_days_required'] < 2:
        raise ValueError('相关性与共同收益至少需要两个观测')
    if any(not math.isfinite(w) or not 0 <= w <= 1 for w in config['role_target_weights'].values()):
        raise ValueError('角色权重必须为[0,1]有限数')


def portfolio_panel(fund_data, portfolio):
    holdings = portfolio.get('holdings', [])
    if not holdings:
        return None, None, '组合配置为空'
    if any(not np.isfinite(h['weight']) or h['weight'] < 0 for h in holdings) or not np.isclose(sum(h['weight'] for h in holdings), 1):
        return None, None, '组合权重不合法，未自动归一化'
    series = {}
    missing = []
    for holding in holdings:
        code = holding['code']
        if code == 'CASH' or holding['weight'] == 0:
            continue
        nav = portfolio.get('navs', {}).get(code)
        if nav is None or len(nav) < 2:
            missing.append(code)
        else:
            series[code] = nav
    if missing:
        return None, None, f'组合持仓数据缺失：{", ".join(missing)}'
    nav = fund_data.get('nav')
    if nav is None or len(nav) < 2:
        return None, None, '候选基金净值数据不可得'
    series[fund_data['code']] = nav
    panel = aligned_returns(series, calendar_days=portfolio.get('calendar_days'))
    base = sum((panel[h['code']] * h['weight'] for h in holdings if h['code'] != 'CASH' and h['weight'] > 0), start=pd.Series(0., index=panel.index))
    return panel, base, ''


def scan_budget_paths(base, candidate, covariance, base_weights, candidate_index,
                      weights, dd_limit, vol_limit):
    """Pure NumPy paths in bounded batches; no provider or DataFrame operations."""
    if len(weights) > 10001:
        raise ValueError('仓位扫描步长过小，扫描点数不能超过10001')
    base = np.asarray(base, dtype=float)
    candidate = np.asarray(candidate, dtype=float)
    rows = []
    for start in range(0, len(weights), 128):
        grid = np.asarray(weights[start:start + 128], dtype=float)
        returns = base[:, None] * (1 - grid) + candidate[:, None] * grid
        wealth = np.vstack([np.ones(len(grid)), np.cumprod(1 + returns, axis=0)])
        drawdowns = -np.min(wealth / np.maximum.accumulate(wealth, axis=0) - 1, axis=0)
        exposure = (1 - grid[:, None]) * base_weights
        exposure[:, candidate_index] += grid
        variance = np.sum((exposure @ covariance) * exposure, axis=1)
        volatility = np.sqrt(np.maximum(0., variance))
        rows.extend({'weight': float(w), 'portfolio_max_drawdown': float(dd),
                     'portfolio_volatility': float(vol),
                     'within_budget': bool(dd <= dd_limit and vol <= vol_limit)}
                    for w, dd, vol in zip(grid, drawdowns, volatility))
    return rows


def derive_position_range(fund_data, portfolio, config) -> dict:
    result = {'lower': 0., 'upper': 0., 'scan_table': [], 'binding_constraint': '数据不足', 'note': '', 'feasible': False}
    minimum = config['min_data_days_required']
    nav = fund_data.get('nav')
    if nav is None or len(nav) < minimum:
        result['note'] = f'该基金历史不足 {minimum} 天；数据不足，无法推导'
        return result
    panel, base, error = portfolio_panel(fund_data, portfolio)
    if error:
        result['note'] = error + '；数据不足，无法推导'
        return result
    if len(panel) < minimum:
        result['note'] = f'共同区间有效日收益仅{len(panel)}条，不足{minimum}条；数据不足，无法推导'
        return result
    cap = min(config['scan_upper_limit'], config['single_fund_max_weight'])
    step = config['scan_step']
    if cap / step > 10000:
        raise ValueError('仓位扫描步长过小，扫描点数不能超过10001')
    weights = [round(i * step, 12) for i in range(math.floor(round(cap / step, 12)) + 1)]
    covariance = panel.cov().to_numpy() * config['annualization_days']
    columns = list(panel.columns)
    base_weights = np.array([sum(h['weight'] for h in portfolio['holdings'] if h['code'] == code) for code in columns])
    dd_limit = config['portfolio_max_drawdown_tolerance']
    vol_limit = config['portfolio_target_volatility'] + config['volatility_buffer']
    result['scan_table'] = scan_budget_paths(base, panel[fund_data['code']], covariance,
        base_weights, columns.index(fund_data['code']), weights, dd_limit, vol_limit)
    rows = result['scan_table']
    dd_feasible = [r['weight'] for r in rows if r['portfolio_max_drawdown'] <= dd_limit]
    vol_feasible = [r['weight'] for r in rows if r['portfolio_volatility'] <= vol_limit]
    feasible = [r['weight'] for r in rows if r['within_budget']]
    dd_cap = max(dd_feasible, default=0.)
    vol_cap = max(vol_feasible, default=0.)
    result['binding_constraint'] = '单标的上限'
    if not dd_feasible or dd_cap < cap:
        result['binding_constraint'] = '回撤约束'
    if (not vol_feasible and dd_feasible) or vol_cap < dd_cap:
        result['binding_constraint'] = '波动约束'
    result['upper'] = max(feasible, default=0.)
    observation = config['observation_position_threshold']
    result['lower'] = 0. if result['upper'] <= observation else min(observation, result['upper'])
    result['feasible'] = bool(feasible)
    candidate_dd = float(-(nav / nav.cummax() - 1).min())
    result.update({'observations': len(panel), 'start': str(panel.index[0].date()), 'end': str(panel.index[-1].date()), 'candidate_max_drawdown': candidate_dd, 'covariance': {'columns': columns, 'annual_matrix': covariance.tolist()}, 'drawdown_feasible_max': dd_cap, 'volatility_feasible_max': vol_cap})
    notes = ['基于历史协方差，不是最优仓位。回撤按共同区间历史固定权重净值路径计算；不是交易策略回测，未模拟交易或预测。']
    if result['upper'] <= observation:
        notes.append('仅作观察仓。')
    if not feasible:
        notes.append('所有扫描权重均未同时满足预算；0—0仅记录无可行解的边界，不表示零权重满足预算，也不表示交易指令。')
    else:
        notes.append('扫描可能存在不连续可行区；区间内各点仍须查阅扫描表，不能假设均满足预算。')
    if candidate_dd > dd_limit:
        notes.append('该基金自身回撤超过你的容忍度，重仓会突破风险预算。该基金自身回撤较大，重仓会突破组合回撤预算；这是历史预算警示，不保证将来损失边界。')
    if config.get('used_default_drawdown_tolerance'):
        notes.append(f'使用默认回撤容忍 {dd_limit:.0%}，可在 risk_budget.json 修改。')
    result['note'] = ''.join(notes)
    return result
