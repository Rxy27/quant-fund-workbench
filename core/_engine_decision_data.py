"""Adapt verified snapshots into explicit rule inputs; never fabricate missing fields."""
import re

import numpy as np
import pandas as pd

from fund_classifier import classify
from portfolio_analysis import correlation_pair, exposure_role
from statistics import aligned_returns


def fee_value(value):
    if value is None:
        return None
    text = str(value).strip()
    match = re.fullmatch(r'(\d+(?:\.\d+)?)%\s*(?:（每年）|\(每年\))?', text)
    if not match:
        return None
    return float(match[1]) / 100


def fee_total(data):
    a, b = fee_value(data.info.get('管理费率')), fee_value(data.info.get('托管费率'))
    return a + b if a is not None and b is not None else None


def tracking_error(data, benchmark, settings):
    # Only the verified CSI300 price-index comparison already supported by this product.
    if '沪深300' not in str(data.info.get('跟踪标的', '')) or benchmark.empty or not data.available:
        return None
    panel = aligned_returns({'fund': data.nav, 'benchmark': benchmark}, calendar_days=settings.get('calendar_days'))
    if len(panel) < settings['minimum_overlap']:
        return None
    return float((panel.fund - panel.benchmark).std(ddof=1) * np.sqrt(settings['annualization_days']))


def role_weights(funds, settings, config):
    weights = {role: 0. for role in config['role_target_weights']}
    core = funds.get(settings['equity_reference'])
    if not core or not core.available:
        return None, {}
    roles = {}
    for holding in settings['holdings']:
        code = holding['code']
        if code == 'CASH' or holding['weight'] == 0:
            continue
        fund = funds.get(code)
        if not fund or not fund.available:
            return None, roles
        corr, n = correlation_pair(fund.nav, core.nav, settings['minimum_overlap'], settings.get('calendar_days'))
        common = aligned_returns({'fund': fund.nav, 'core': core.nav}, calendar_days=settings.get('calendar_days'))
        vol = float(common.fund.std(ddof=1) * np.sqrt(settings['annualization_days'])) if n >= settings['minimum_overlap'] else float('nan')
        role = holding.get('role') or exposure_role(vol, corr, corr, settings['role_rules'])
        roles[code] = role
        if role not in weights:
            return None, roles
        weights[role] += holding['weight']
    return weights, roles


def build_decision_inputs(data, classification, portfolio_result, funds, settings, config, benchmark, costs):
    weights, roles = role_weights(funds, settings, config)
    portfolio = {'holdings': settings['holdings'], 'navs': {code: fund.nav for code, fund in funds.items()}, 'role_weights': weights, 'holding_roles': roles, 'calendar_days': settings.get('calendar_days')}
    fund = {'code': data.code, 'nav': data.nav, 'category': classification.category, 'active': classification.active, 'role': portfolio_result['role'], 'exposure_role': portfolio_result.get('exposure_role'), 'cost_nav': costs.get('cost_nav', {}).get(data.code), 'cost_basis_nav': pd.Series(dtype=float), 'total_fee': fee_total(data), 'tracking_index': data.info.get('跟踪标的'), 'tracking_error': tracking_error(data, benchmark, settings), 'same_index_peers': None, 'peer_list_complete': False}
    unit = data.tables.get('单位净值', pd.DataFrame())
    if {'净值日期', '单位净值'}.issubset(unit):
        from data_loader import clean_nav
        fund['cost_basis_nav'], _ = clean_nav(unit, '单位净值')
    reference = config.get('reference_fund', '110020')
    fund['reference_fund'] = reference
    portfolio_result['reference_note'] = f'基于 {reference} 作为参照系'
    core = funds.get(reference)
    if data.available and core and core.available:
        panel = aligned_returns({'fund': data.nav, 'core': core.nav}, calendar_days=settings.get('calendar_days'))
        window = config['correlation_window_days']
        rolling = panel.fund.rolling(window, min_periods=window).corr(panel.core).replace([np.inf, -np.inf], np.nan)
        cutoff = data.nav.index[-1] - pd.DateOffset(years=config['valuation_history_years'])
        low = rolling.loc[cutoff:].dropna()
        fund.update(recent_correlation=float(rolling.iloc[-1]) if len(rolling) and pd.notna(rolling.iloc[-1]) else None, historical_low_correlation=float(low.min()) if len(low) else None, correlation_observations=len(panel), correlation_history_start=str(low.index[0].date()) if len(low) else None)
    tenure = data.tables.get('经理任职', pd.DataFrame())
    if not tenure.empty and {'任职日期', '离任日期'}.issubset(tenure) and data.available:
        dates = pd.to_datetime(pd.concat([tenure['任职日期'], tenure['离任日期']]), errors='coerce').dropna()
        inception = pd.to_datetime(data.info.get('成立时间'), errors='coerce')
        if pd.isna(inception):
            from stress_test import inception_date
            inception = inception_date(data.info)
        if inception is not None:
            dates = dates.loc[dates > inception]
            end = data.nav.index[-1]
            fund['manager_changed_recently'] = bool(((dates >= end - pd.DateOffset(months=config['active_fund_manager_change_months'])) & (dates <= end)).any())
    scale = data.tables.get('规模变化', pd.DataFrame())
    if {'截止日期', '净资产规模（亿元）'}.issubset(scale) and len(scale) >= 2:
        frame = pd.DataFrame({'date': pd.to_datetime(scale['截止日期'], errors='coerce'), 'value': pd.to_numeric(scale['净资产规模（亿元）'], errors='coerce')}).dropna().drop_duplicates('date').sort_values('date')
        if len(frame) >= 2:
            prev, last = frame.iloc[-2], frame.iloc[-1]
            if last.date.to_period('Q').ordinal - prev.date.to_period('Q').ordinal == 1 and prev.value > 0:
                fund['quarterly_scale_change'] = float(last.value / prev.value - 1)
    # No standard verified PE/PB-history, category median, or top-ten turnover field
    # exists in the current cache. Do not infer these from names or a few holdings.
    if fund['tracking_index']:
        peers = []
        for code, peer in funds.items():
            if code != data.code and classify(peer.info).category == '指数型' and peer.info.get('跟踪标的') == fund['tracking_index']:
                comparison = {'code': code, 'tracking_index': fund['tracking_index'], 'total_fee': fee_total(peer), 'tracking_error': None, 'candidate_tracking_error': None, 'basis': '两只基金与沪深300价格指数的同一共同区间，不含分红的参照口径'}
                if '沪深300' in fund['tracking_index'] and not benchmark.empty and data.available and peer.available:
                    panel = aligned_returns({'fund': data.nav, 'peer': peer.nav, 'benchmark': benchmark}, calendar_days=settings.get('calendar_days'))
                    if len(panel) >= settings['minimum_overlap']:
                        scale = np.sqrt(settings['annualization_days'])
                        comparison.update(tracking_error=float((panel.peer-panel.benchmark).std(ddof=1)*scale), candidate_tracking_error=float((panel.fund-panel.benchmark).std(ddof=1)*scale), start=str(panel.index[0].date()), end=str(panel.index[-1].date()), observations=len(panel))
                peers.append(comparison)
        if peers:
            fund['same_index_peers'] = peers
    portfolio_result['role_weights'] = weights
    portfolio_result['holding_roles'] = roles
    portfolio_result['role_gaps'] = {role: config['role_target_weights'][role] - value for role, value in weights.items()} if weights is not None else None
    return fund, portfolio
