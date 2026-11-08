"""Orchestration of the audited functions; Streamlit is deliberately absent."""
from copy import deepcopy
import re

import pandas as pd
import streamlit as st

from .engine import ROOT, load
from .state import read_json, portfolio_settings
from .decision_rules import evaluate_conditions
from .position_sizing import derive_position_range
from .allocation import portfolio_controls
from .network import network_limited, report_progress


class IncompleteData(Exception):
    """Carry a degraded result without caching it for a whole day."""
    def __init__(self, data):
        self.data = data


@st.cache_data(ttl=86400, show_spinner=False)
def _load_fund(code, offline=True):
    data = load('data_loader').DataLoader(offline=offline, refresh=False).load(code)
    if data.meta.get('retry_required'):
        raise IncompleteData(data)
    return data


def load_fund(code, offline=True):
    try:
        return _load_fund(code, offline)
    except IncompleteData as exc:
        return exc.data


@st.cache_data(ttl=86400, show_spinner=False)
def _load_benchmark(offline=True):
    data = load('data_loader').DataLoader(offline=offline).benchmark()
    if data[1].get('error') or any(c.get('refresh_failed') for c in data[1].get('calls', [])):
        raise IncompleteData(data)
    return data


def load_benchmark(offline=True):
    try:
        return _load_benchmark(offline)
    except IncompleteData as exc:
        return exc.data


# Preserve the public invalidation API used by the UI and existing callers.
load_fund.cache_clear = _load_fund.clear
load_benchmark.cache_clear = _load_benchmark.clear


def search_funds(query, offline=True):
    query = query.strip()
    if re.fullmatch(r'\d{6}', query):
        return [(query, query)]
    if query.isdecimal():
        raise ValueError('基金代码必须为6位数字')
    if not query:
        raise ValueError('请输入基金代码或名称')
    # Name search uses verified snapshots first; the online catalog adds new funds.
    choices = []
    for path in sorted((ROOT / 'data').glob('*_manifest.json')):
        code = path.name.split('_')[0]
        if not re.fullmatch(r'\d{6}', code):
            continue
        fund = load_fund(code, True)
        name = str(fund.info.get('基金名称', ''))
        if query in name:
            choices.append((code, name))
    if not choices and not offline:
        catalog = load('data_loader').DataLoader(offline=False).api('fund_name_em')
        for row in catalog.loc[catalog['基金简称'].str.contains(query, regex=False, na=False)].to_dict('records'):
            choices.append((str(row['基金代码']).zfill(6), row['基金简称']))
    if not choices:
        raise ValueError('未找到匹配基金；可输入6位代码，或关闭仅使用本地缓存后重试')
    return choices


@network_limited
def analyze(code, user_portfolio, risk, offline=True):
    settings = portfolio_settings(user_portfolio)
    parameters = read_json(ROOT / 'config/rule_parameters.json')
    config = {**risk, **parameters, 'used_default_drawdown_tolerance': False,
              '_valuation_offline': offline}
    load('position_sizing').validate_decision_config(config)
    reference = config.get('reference_fund', '110020')
    settings['equity_reference'] = settings['reference_fund'] = reference
    report_progress('正在获取当前基金数据...')
    fund = load_fund(code, offline)
    classification = load('fund_classifier').classify(fund.info)
    codes = list(dict.fromkeys([reference] +
        [h['code'] for h in settings['holdings'] if h['code'] != 'CASH' and h['weight'] > 0] +
        (settings.get('fee_comparison_codes', []) if classification.category == '指数型' else [])))
    funds = {code: fund}
    report_progress('正在获取组合相关基金数据...')
    for c in codes:
        if c not in funds:
            funds[c] = load_fund(c, offline)
    report_progress('正在获取基准数据...')
    benchmark, benchmark_meta = load_benchmark(offline)
    annualization = 365 if classification.category == '货币型' else 252
    # A mixed portfolio uses trading-date returns, including money-fund weekend
    # income in the next observation. An all-money portfolio uses natural days.
    invested = {code} | {h['code'] for h in settings['holdings'] if h['code'] != 'CASH' and h['weight'] > 0}
    calendar = 365 if all(load('fund_classifier').classify(funds[c].info).category == '货币型' for c in invested) else 252
    settings['annualization_days'] = settings['calendar_days'] = calendar
    config['annualization_days'] = calendar
    config['calendar_days'] = calendar
    report_progress('正在计算相关性矩阵与扫描风险预算...')
    metrics = load('statistics').describe(fund.nav, annualization, settings['risk_free_rate'])
    portfolio = load('portfolio_analysis').analyze_portfolio(fund, funds, settings)
    costs = {'cost_nav': {h['code']: h.get('cost_nav') for h in user_portfolio['holdings']}}
    decision_fund, decision_portfolio = load('decision_data').build_decision_inputs(
        fund, classification, portfolio, funds, settings, config, benchmark, costs)
    decision_fund['management_fee'] = load('decision_data').fee_value(fund.info.get('管理费率'))
    decision_fund['custody_fee'] = load('decision_data').fee_value(fund.info.get('托管费率'))
    decision_portfolio['_risk_controls'] = portfolio_controls(decision_fund, decision_portfolio, config)
    sizing = derive_position_range(decision_fund, decision_portfolio, config)
    decision_portfolio['position_range'] = sizing
    conditions = evaluate_conditions(decision_fund, decision_portfolio, config)
    network_timeout = any('网络超时' in str(f.meta.get('calls', [])) + str(f.meta.get('fetch_failures', [])) for f in funds.values()) or '网络超时' in str(benchmark_meta) or '网络超时' in str(conditions)
    thresholds = load('report_generator').condition_thresholds(config)
    thresholds['S5'] = '简称匹配同一指数并排除场内ETF/LOF；随机抽样最多10只，费率窗口5秒；管理费+托管费严格更低，年化收益差异标准差<2%，共同日收益≥50条'
    if classification.category == '指数型':
        thresholds['B2'] = '最近1250条观测（至少100条有效PE）：分位≤30%；30%–50%估值中性；>50%估值偏高'
    thresholds['B5'] = '每月第一个已观察交易日，实际权重低于最新ERC/显式目标；次日开盘执行'
    thresholds['S3'] = '每月第一个已观察交易日，实际权重高于最新ERC/显式目标；次日开盘执行'
    thresholds['B4'] += '；仓位扫描使用EWMA下行风险；目标杠杆按总波动率即时缩放，订单仅在月度调仓日生成'
    stress = load('stress_test').stress_analysis(fund, benchmark,
        read_json(ROOT / 'config/scenarios.json'), {**settings, 'annualization_days': annualization})
    return dict(code=code, fund=fund, classification=classification, metrics=metrics,
                portfolio=portfolio, sizing=sizing, conditions=conditions, stress=stress,
                config=config, thresholds=thresholds,
                decision_fund=decision_fund, decision_portfolio=decision_portfolio,
                user_portfolio=deepcopy(user_portfolio), risk=deepcopy(risk),
                benchmark_meta=benchmark_meta, offline=offline,
                network_timeout=network_timeout,
                reference_note=f'基于 {reference} 作为参照系',
                calendar_note=f'单基金年化 {annualization} 天；组合年化 {calendar} 天。缺失日期以前值填充，首个有效净值前不回填；混合组合保留货币基金周末收益。',
                retry_required=bool(benchmark_meta.get('error')) or
                    any(m.get('refresh_failed') for m in benchmark_meta.get('calls', [])) or
                    any(f.meta.get('retry_required') or not f.available for f in funds.values()))


def simulate_purchase(report, amount):
    """Treat purchase as additional capital, then reuse the engine's zero-scan row.

    No scan parameter, cap or budget changes. The zero row evaluates the newly
    funded portfolio with the exact same covariance and drawdown implementation.
    """
    if not pd.notna(amount) or amount < 0 or not float('-inf') < amount < float('inf'):
        raise ValueError('假设金额必须为非负有限数')
    total = report['user_portfolio']['account_total']
    portfolio = deepcopy(report['decision_portfolio'])
    for holding in portfolio['holdings']:
        holding['weight'] *= total / (total + amount)
    held = next((h for h in portfolio['holdings'] if h['code'] == report['code']), None)
    if held is None:
        held = {'code': report['code'], 'weight': 0.}
        portfolio['holdings'].append(held)
    held['weight'] += amount / (total + amount)
    result = derive_position_range(report['decision_fund'], portfolio, report['config'])
    row = next((r for r in result['scan_table'] if r['weight'] == 0), None)
    return {'row': row, 'note': result['note'], 'candidate_weight': held['weight'],
            'added_capital_weight': amount / (total + amount),
            'start': result.get('start'), 'end': result.get('end')}
