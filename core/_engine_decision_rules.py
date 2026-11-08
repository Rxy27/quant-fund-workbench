"""Independent descriptive conditions. Missing observations never become zero."""
import math
from decimal import Decimal

import pandas as pd


def result(status, reason, evidence, source='计算推导'):
    return {'status': status, 'reason': reason, 'evidence': evidence, 'data_source': source}


def finite(value):
    try:
        return value is not None and math.isfinite(float(value))
    except (ValueError, TypeError):
        return False


def has_year(fund_data):
    nav = fund_data.get('nav')
    return nav is not None and len(nav) > 1 and nav.index[-1] >= nav.index[0] + pd.DateOffset(years=1)


def decimal(value):
    """Preserve decimal boundary semantics without widening a configured threshold."""
    return Decimal(str(value))


def buy_condition_1(fund_data, portfolio, config) -> dict:
    role = fund_data.get('role')
    weights = portfolio.get('role_weights')
    evidence = {'role': role, 'threshold': config['rebalance_threshold_pp']}
    if not portfolio.get('holdings') or not role or role == '无法判定':
        return result('无法判定', '组合配置为空或该基金角色无法判定', evidence)
    if role == '冗余型':
        return result('不满足', '冗余角色，无缺口可填补', evidence)
    if weights is None or role not in config['role_target_weights'] or role not in weights:
        return result('无法判定', '组合角色实际权重或该角色目标权重不可得', evidence)
    gap = float(decimal(config['role_target_weights'][role]) - decimal(weights[role]))
    evidence.update(target=config['role_target_weights'][role], actual=weights[role], gap=gap)
    if gap >= config['rebalance_threshold_pp']:
        return result('满足', '该角色缺口达到当前配置阈值', evidence)
    return result('不满足', '该角色已超配' if gap <= 0 else f'角色缺口不足 {config["rebalance_threshold_pp"] * 100:g}pp', evidence)


def buy_condition_2(fund_data, portfolio, config) -> dict:
    category = fund_data.get('category')
    if category in ('债券型', '货币型', 'QDII'):
        return result('不适用', '该类型不适用估值或回撤条件', {'category': category})
    if category == '指数型':
        value = fund_data.get('valuation_percentile')
        evidence = {'percentile': value, 'history_years': config['valuation_history_years'], 'threshold': config['valuation_percentile_threshold'], 'neutral_threshold': config['valuation_neutral_threshold']}
        if not finite(value) or not 0 <= value <= 1:
            return result('无法判定', f'akshare 无可核验的跟踪指数 PE/PB 近{config["valuation_history_years"]}年历史分位字段', evidence, 'akshare:跟踪指数PE/PB历史分位')
        if value <= config['valuation_percentile_threshold']:
            return result('满足', '历史估值分位达到配置条件；不推断后续涨跌', evidence, 'akshare:跟踪指数PE/PB历史分位')
        return result('不满足', '估值中性' if value <= config['valuation_neutral_threshold'] else '估值偏高', evidence, 'akshare:跟踪指数PE/PB历史分位')
    if fund_data.get('active'):
        if not has_year(fund_data):
            return result('无法判定', '主动基金净值历史不足1年', {'threshold': config['active_fund_drawdown_threshold']})
        nav = fund_data['nav']
        drawdown = float(1 - decimal(nav.iloc[-1]) / decimal(nav.max()))
        evidence = {'current_drawdown': drawdown, 'threshold': config['active_fund_drawdown_threshold'], 'near_high_band': config['trend_deviation_band']}
        if drawdown >= config['active_fund_drawdown_threshold']:
            return result('满足', '回撤条件，非估值条件；历史超额不保证持续', evidence)
        return result('不满足', '接近历史高点' if drawdown < config['trend_deviation_band'] else '回撤幅度未达到配置阈值', evidence)
    return result('无法判定', '基金类型不明确或未配置该类型的估值规则', {'category': category})


def buy_condition_3(fund_data, portfolio, config) -> dict:
    nav = fund_data.get('nav')
    n = len(nav) if nav is not None else 0
    evidence = {'observations': n, 'ma_days': config['trend_ma_days'], 'minimum_days': config['min_ma_days_required'], 'band': config['trend_deviation_band']}
    if n < max(config['min_ma_days_required'], config['trend_ma_days']):
        return result('无法判定', f'有效交易日不足 {max(config["min_ma_days_required"], config["trend_ma_days"])} 天（按有效净值观测日计，未核验交易所日历）', evidence)
    average = float(nav.tail(config['trend_ma_days']).mean())
    if not finite(average) or average <= 0 or not finite(nav.iloc[-1]):
        return result('无法判定', '均线或当前净值数据不可得', evidence)
    deviation = float(decimal(nav.iloc[-1]) / decimal(average) - 1)
    evidence.update(current_nav=float(nav.iloc[-1]), moving_average=average, deviation=deviation)
    band = config['trend_deviation_band']
    if -band <= deviation <= band:
        return result('满足', '净值距历史均线的偏离处于配置区间', evidence)
    return result('不满足', '下跌趋势中' if deviation < -band else '短期涨幅过大', evidence)


def buy_condition_4(fund_data, portfolio, config) -> dict:
    sizing = portfolio.get('position_range', {})
    evidence = {'volatility_limit': config['portfolio_target_volatility'] + config['volatility_buffer'], 'upper': sizing.get('upper')}
    if not has_year(fund_data):
        return result('无法判定', '该基金净值历史不足1年', evidence)
    if not sizing or sizing.get('binding_constraint') == '数据不足':
        return result('无法判定', sizing.get('note', '仓位推导数据不可得，可能存在持仓净值缺失'), evidence)
    rows = [r for r in sizing.get('scan_table', []) if r['weight'] == sizing.get('upper')]
    if not rows or not finite(rows[0]['portfolio_volatility']):
        return result('无法判定', '仓位上界对应的组合波动数据不可得', evidence)
    evidence.update(portfolio_volatility=rows[0]['portfolio_volatility'], feasible=sizing.get('feasible'))
    satisfied = rows[0]['portfolio_volatility'] <= evidence['volatility_limit']
    return result('满足' if satisfied else '不满足', '纳入后历史波动未超过配置预算；不代表回撤约束也满足' if satisfied else '纳入后突破波动预算', evidence)


def buy_condition_5(fund_data, portfolio, config) -> dict:
    role = fund_data.get('role')
    if role == '冗余型':
        role = fund_data.get('exposure_role')
    evidence = {'role': role, 'candidate_role': fund_data.get('role'), 'threshold': config['rebalance_threshold_pp']}
    if not portfolio.get('holdings'):
        return result('无法判定', '组合配置为空', evidence)
    weights = portfolio.get('role_weights')
    if weights is None or role not in config['role_target_weights'] or role not in weights:
        return result('无法判定', '基金角色、该角色目标或实际角色权重不可得', evidence)
    target = config['role_target_weights'][role]
    actual = weights[role]
    evidence.update(actual=actual, target=target, boundary=target - config['rebalance_threshold_pp'])
    satisfied = decimal(actual) < decimal(target) - decimal(config['rebalance_threshold_pp'])
    return result('满足' if satisfied else '不满足', '实际角色权重显著低于目标' if satisfied else '再平衡未触发', evidence)


def buy_condition_6(fund_data, portfolio, config) -> dict:
    category = fund_data.get('category')
    if fund_data.get('active'):
        return result('不适用', '主动基金费率与超额不构成简单可比关系', {'category': category})
    if category == 'QDII':
        return result('不适用', 'QDII 费率结构差异大，不宜简单对比', {'category': category})
    fee, median = fund_data.get('total_fee'), fund_data.get('peer_fee_median')
    evidence = {'management_plus_custody': fee, 'peer_median': median, 'peer_scope': fund_data.get('peer_fee_scope')}
    if category not in ('指数型', '债券型', '货币型') or not finite(fee) or not finite(median):
        return result('无法判定', 'akshare 同类费率中位数或本基金管理费/托管费数据不可得；不将少量配置样本冒充同类总体', evidence, 'akshare:管理费率/托管费率/同类费率中位数')
    return result('满足' if fee <= median else '不满足', '费用合计不高于同类中位数' if fee <= median else '费率偏高', evidence, 'akshare:管理费率/托管费率/同类费率中位数')


def sell_condition_1(fund_data, portfolio, config) -> dict:
    recent = fund_data.get('recent_correlation')
    historical = fund_data.get('historical_low_correlation')
    n = fund_data.get('correlation_observations', 0)
    reference = config.get('reference_fund', '110020')
    evidence = {'recent_correlation': recent, 'historical_low': historical, 'observations': n, 'reference': reference, 'window_days': config['correlation_window_days'], 'history_years': config['valuation_history_years'], 'recent_threshold': config['role_failure_correlation'], 'low_threshold': config['role_failure_historical_low']}
    evidence['available_rolling_history_start'] = fund_data.get('correlation_history_start')
    if n < config['correlation_window_days'] or not finite(recent) or not finite(historical):
        return result('无法判定', f'共同日收益不足{config["correlation_window_days"]}条，或近{config["valuation_history_years"]}年可用滚动相关性/参照{reference}数据不可得', evidence)
    triggered = recent >= config['role_failure_correlation'] and historical <= config['role_failure_historical_low']
    if triggered:
        return result('触发', f'近{config["correlation_window_days"]}日相关性 {recent:.3f}，近{config["valuation_history_years"]}年可用窗口最低 {historical:.3f}，角色失效；不足完整历史时仅代表可用区间；仅提示重新审视', evidence)
    reason = '一直高相关，非角色失效' if recent >= config['role_failure_correlation'] and historical > config['role_failure_historical_low'] else '相关性未同时达到角色失效的两项条件'
    return result('未触发', reason, evidence)


def sell_condition_2(fund_data, portfolio, config) -> dict:
    cost = fund_data.get('cost_nav')
    nav = fund_data.get('cost_basis_nav', fund_data.get('nav'))
    evidence = {'cost_nav': cost, 'stop_loss': config['single_fund_stop_loss']}
    if not finite(cost) or cost <= 0:
        return result('无法判定', '未填写买入成本净值，无法计算止损', evidence, 'akshare:单位净值/holdings.json买入成本净值')
    if nav is None or nav.empty or not finite(nav.iloc[-1]):
        return result('无法判定', '与买入成本口径相同的当前单位净值不可得；不混用复权净值', evidence, 'akshare:单位净值/holdings.json买入成本净值')
    change = float(decimal(nav.iloc[-1]) / decimal(cost) - 1)
    evidence.update(current_nav=float(nav.iloc[-1]), cost_change=change)
    triggered = change <= -config['single_fund_stop_loss']
    return result('触发' if triggered else '未触发', '成本口径亏损达到配置界限；仅提示重新审视' if triggered else '成本口径亏损未达到配置界限', evidence, 'akshare:单位净值/holdings.json买入成本净值')


def sell_condition_3(fund_data, portfolio, config) -> dict:
    holdings = portfolio.get('holdings', [])
    held = next((h for h in holdings if h['code'] == fund_data['code'] and h['weight'] > 0), None)
    evidence = {'threshold': config['rebalance_threshold_pp']}
    if not held:
        return result('无法判定', '组合配置为空或该基金未持有', evidence)
    role = fund_data.get('exposure_role', fund_data.get('role'))
    target = held.get('target_weight', config['role_target_weights'].get(role))
    if not finite(target):
        return result('无法判定', '该持仓的目标权重或可识别角色目标不可得', evidence)
    evidence.update(actual=held['weight'], target=target, role=role)
    triggered = decimal(held['weight']) > decimal(target) + decimal(config['rebalance_threshold_pp'])
    return result('触发' if triggered else '未触发', '实际权重超过目标及配置偏离阈值；仅提示重新审视' if triggered else '实际权重未超过再平衡偏离阈值', evidence)


def sell_condition_4(fund_data, portfolio, config) -> dict:
    if fund_data.get('category') in (None, '无法判定') and not fund_data.get('active'):
        return result('无法判定', '基金类型数据不可得，无法确认是否为主动基金', {}, 'akshare:基金类型')
    if not fund_data.get('active'):
        return result('不适用', '非主动基金，不适用该项基本面变化条件', {})
    manager = fund_data.get('manager_changed_recently')
    scale = fund_data.get('quarterly_scale_change')
    turnover = fund_data.get('top10_turnover')
    evidence = {'manager_changed_recently': manager, 'quarterly_scale_change': scale, 'top10_turnover': turnover, 'manager_months': config['active_fund_manager_change_months'], 'scale_threshold': config['active_fund_scale_change_ratio'], 'turnover_threshold': config['active_fund_turnover_ratio']}
    checks = [manager if isinstance(manager, bool) else None, abs(scale) > config['active_fund_scale_change_ratio'] if finite(scale) else None, turnover > config['active_fund_turnover_ratio'] if finite(turnover) else None]
    if any(value is True for value in checks):
        return result('触发', '至少一项经理、季度规模或前十重仓换手变化达到条件；仅提示，不判断经理能力或历史超额持续性；其他缺失项仍不可判断', evidence, 'akshare:经理任职/规模季度变化/前十大重仓换手率')
    missing = [name for name, value in zip(('经理变更日期', '相邻季度规模变化', '前十大重仓换手率'), checks) if value is None]
    if missing:
        return result('无法判定', 'akshare 字段缺失：' + '、'.join(missing), evidence, 'akshare:经理任职/规模季度变化/前十大重仓换手率')
    return result('未触发', '三项已取得字段均未达到变化条件；不判断后续持续性', evidence, 'akshare:经理任职/规模季度变化/前十大重仓换手率')


def sell_condition_5(fund_data, portfolio, config) -> dict:
    if fund_data.get('category') in (None, '无法判定'):
        return result('无法判定', '基金类型数据不可得，无法确认是否为指数基金', {}, 'akshare:基金类型')
    if fund_data.get('category') != '指数型':
        return result('不适用', '非指数基金，不适用同跟踪指数的费用与误差条件', {})
    peers = fund_data.get('same_index_peers')
    fee, error = fund_data.get('total_fee'), fund_data.get('tracking_error')
    evidence = {'fund_fee': fee, 'fund_tracking_error': error, 'tracking_index': fund_data.get('tracking_index'), 'peers': peers}
    if peers is None or not finite(fee) or not finite(error) or not fund_data.get('tracking_index'):
        return result('无法判定', 'akshare 同跟踪指数基金列表、本基金费率或可比跟踪误差数据不可得', evidence)
    matches = [p for p in peers if p.get('code') != fund_data['code'] and p.get('tracking_index') == fund_data['tracking_index'] and finite(p.get('total_fee')) and finite(p.get('tracking_error')) and finite(p.get('candidate_tracking_error', error)) and p['total_fee'] < fee and p['tracking_error'] < p.get('candidate_tracking_error', error)]
    evidence['matches'] = matches
    if matches:
        return result('触发', '已核验样本中存在同跟踪指数且费用与误差均较低的记录，仅列事实并提示重新审视，不构成优劣或行动判断', evidence)
    complete = fund_data.get('peer_list_complete', False) and all(finite(p.get('total_fee')) and finite(p.get('tracking_error')) for p in peers)
    if not complete:
        return result('无法判定', '可核验的同跟踪指数样本不完整，无法断言不存在符合条件的记录', evidence)
    return result('未触发', '已核验完整同跟踪指数列表，未发现同时满足两项比较条件的记录', evidence)


BUY_FUNCTIONS = (buy_condition_1, buy_condition_2, buy_condition_3, buy_condition_4, buy_condition_5, buy_condition_6)
SELL_FUNCTIONS = (sell_condition_1, sell_condition_2, sell_condition_3, sell_condition_4, sell_condition_5)
BUY_LABELS = ('组合角色缺口', '估值/回撤条件', '趋势条件', '风险预算条件', '再平衡条件', '费率条件')
SELL_LABELS = ('角色失效', '风险超限', '再平衡偏离', '基本面变化（仅主动基金）', '同跟踪指数的费用与误差条件')


def evaluate_conditions(fund_data, portfolio, config):
    def rows(functions, labels, prefix):
        return [{'id': f'{prefix}{i}', 'label': label, **function(fund_data, portfolio, config)} for i, (function, label) in enumerate(zip(functions, labels), 1)]
    return {'buy': rows(BUY_FUNCTIONS, BUY_LABELS, 'B'), 'sell': rows(SELL_FUNCTIONS, SELL_LABELS, 'S')}
