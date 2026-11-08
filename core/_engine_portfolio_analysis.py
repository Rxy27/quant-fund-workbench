"""Historical covariance scenarios; roles are exposure descriptions, never merit labels."""
import numpy as np
import pandas as pd
from scipy.stats import pearsonr

from statistics import aligned_returns, return_metrics


def exposure_role(annual_vol, equity_corr, portfolio_corr, rules):
    if equity_corr is not None and equity_corr <= rules['hedge_correlation']:
        return '对冲型'
    if equity_corr is not None and equity_corr < rules['low_correlation'] and annual_vol < rules['low_volatility']:
        return '防守型'
    if portfolio_corr is not None and abs(portfolio_corr) < rules['low_correlation']:
        return '分散型'
    if equity_corr is not None and equity_corr >= rules['high_stock_correlation'] and annual_vol >= rules['low_volatility']:
        return '进攻型'
    return '无法判定'


def validate_portfolio(settings):
    holdings = settings.get('holdings', [])
    codes = [h['code'] for h in holdings]
    if not holdings or len(set(codes)) != len(codes):
        raise ValueError('持仓不能为空且代码不得重复')
    if any(not np.isfinite(h['weight']) or h['weight'] < 0 for h in holdings):
        raise ValueError('持仓权重必须为非负有限数')
    if not np.isclose(sum(h['weight'] for h in holdings), 1.0, atol=1e-9):
        raise ValueError('持仓权重之和必须为1；不自动归一化')
    if not -1 < settings['risk_free_rate'] < 1:
        raise ValueError('无风险利率需按小数填写且在(-1,1)内')
    for key in ('annualization_days', 'minimum_overlap', 'sensitivity_window', 'pre_event_window'):
        if not isinstance(settings[key], int) or settings[key] < 2:
            raise ValueError(f'{key}必须为至少2的整数')
    if settings['sensitivity_window'] < settings['minimum_overlap']:
        raise ValueError('敏感性窗口不得少于最小样本数')
    if any(not 0 < w < 1 for w in settings['inclusion_weights']):
        raise ValueError('新增配置比例必须在(0,1)内')


def correlation_pair(a, b, minimum, calendar_days=None):
    frame = aligned_returns({'a': a, 'b': b}, calendar_days=calendar_days)
    if len(frame) < minimum or min(frame.a.std(), frame.b.std()) <= 1e-12:
        return None, len(frame)
    corr = float(pearsonr(frame.a.to_numpy(), frame.b.to_numpy()).statistic)
    return corr if np.isfinite(corr) else None, len(frame)


def analyze_portfolio(candidate, funds, settings):
    result = {'matrix': [], 'columns': [], 'changes': [], 'role': '无法判定', 'basis': [], 'error': '', 'overlap': 0, 'correlation_core': None}
    if not candidate.available:
        result['error'] = '候选基金净值数据不可得，无法分析组合。'
        return result
    holdings = [h for h in settings['holdings'] if h['weight'] > 0]
    names = list(dict.fromkeys([h['code'] for h in holdings] + [candidate.code]))
    result['columns'] = names
    pair_cache = {}
    def pair(a, b):
        # Preserve each pair's own calendar/overlap. Reusing the symmetric
        # result avoids repeating alignment in the matrix and role checks.
        key = tuple(sorted((a.code, b.code)))
        if key not in pair_cache:
            pair_cache[key] = correlation_pair(a.nav, b.nav, settings['minimum_overlap'], settings.get('calendar_days'))
        return pair_cache[key]
    for a in names:
        row = {'code': a, 'cells': []}
        for b in names:
            da = candidate if a == candidate.code else funds.get(a)
            db = candidate if b == candidate.code else funds.get(b)
            if a == 'CASH' or b == 'CASH' or da is None or db is None or not da.available or not db.available:
                row['cells'].append({'value': None, 'n': 0})
            else:
                corr, n = pair(da, db)
                row['cells'].append({'value': corr, 'n': n})
        result['matrix'].append(row)
    core = funds.get(settings.get('reference_fund', '110020'))
    if core and core.available:
        result['correlation_core'] = pair(candidate, core)[0]
    absent = [h['code'] for h in holdings if h['code'] != 'CASH' and (h['code'] not in funds or not funds[h['code']].available)]
    if absent:
        result['error'] = f'持仓{", ".join(absent)}净值不可得；未丢弃持仓或重新分配其权重。'
        return result
    series = {h['code']: funds[h['code']].nav for h in holdings if h['code'] != 'CASH'}
    series[candidate.code] = candidate.nav
    panel = aligned_returns(series, calendar_days=settings.get('calendar_days'))
    if len(panel) < settings['minimum_overlap']:
        result['error'] = f'共同的同区间日收益仅{len(panel)}条，少于{settings["minimum_overlap"]}条。'
        return result
    baseline = sum((panel[h['code']] * h['weight'] for h in holdings if h['code'] != 'CASH'), start=pd.Series(0.0, index=panel.index))
    result.update({'overlap': len(panel), 'start': str(panel.index[0].date()), 'end': str(panel.index[-1].date()), 'holdings': holdings})
    columns = list(panel.columns)
    baseline_weights = np.array([sum(h['weight'] for h in holdings if h['code'] == code) for code in columns])
    covariance = panel.cov().to_numpy() * settings['annualization_days']
    for weight in [0.0] + settings['inclusion_weights']:
        combined = (1 - weight) * baseline + weight * panel[candidate.code]
        metrics = return_metrics(combined, settings['annualization_days'], settings['risk_free_rate'])
        w = baseline_weights * (1 - weight)
        w[columns.index(candidate.code)] += weight
        metrics['volatility'] = float(np.sqrt(max(0.0, w @ covariance @ w)))
        result['changes'].append({'weight': weight, **metrics})
    result['covariance'] = {'columns': columns, 'annual_matrix': covariance.tolist()}
    rules = settings['role_rules']
    annual_vol = float(panel[candidate.code].std(ddof=1) * np.sqrt(settings['annualization_days']))
    result['basis'].append(f'共同区间候选基金历史年化波动率{annual_vol:.2%}，样本{len(panel)}条。')
    max_corr, max_code = None, None
    for holding in holdings:
        code = holding['code']
        if code == 'CASH':
            continue
        corr, n = pair(candidate, funds[code])
        if corr is not None and (max_corr is None or corr > max_corr):
            max_corr, max_code = corr, code
        if corr is not None:
            result['basis'].append(f'与现有持仓{code}的历史相关系数{corr:.3f}（{n}条共同日收益）。')
    equity = funds.get(settings['equity_reference'])
    equity_corr = pair(candidate, equity)[0] if equity and equity.available else None
    if equity_corr is not None:
        result['basis'].append(f'与股票参照{settings["equity_reference"]}的相关系数{equity_corr:.3f}。')
    portfolio_corr = None
    if baseline.std(ddof=1) > 1e-12 and panel[candidate.code].std(ddof=1) > 1e-12:
        portfolio_corr = float(pearsonr(baseline, panel[candidate.code]).statistic)
        result['basis'].append(f'与原组合的历史相关系数{portfolio_corr:.3f}。')
    result['exposure_role'] = exposure_role(annual_vol, equity_corr, portfolio_corr, rules)
    if max_corr is not None and max_corr > rules['redundant_correlation']:
        result['role'] = '冗余型'
        result['basis'].append(f'与{max_code}的相关性超过{rules["redundant_correlation"]:.2f}；纳入后分散效果有限。重复研究已持有基金时增加的是同一暴露。')
    elif equity_corr is not None and equity_corr <= rules['hedge_correlation']:
        result['role'] = '对冲型'
        result['basis'].append(f'历史股票相关性≤{rules["hedge_correlation"]:.2f}；这只是阈值描述，不是显著性或有效对冲保证。')
    elif equity_corr is not None and equity_corr < rules['low_correlation'] and annual_vol < rules['low_volatility']:
        result['role'] = '防守型'
        result['basis'].append(f'历史股票相关性<{rules["low_correlation"]:.2f}，波动率<{rules["low_volatility"]:.0%}。')
    elif portfolio_corr is not None and abs(portfolio_corr) < rules['low_correlation']:
        result['role'] = '分散型'
        result['basis'].append(f'历史组合相关性绝对值<{rules["low_correlation"]:.2f}；分散不代表无风险。')
    elif equity_corr is not None and equity_corr >= rules['high_stock_correlation'] and annual_vol >= rules['low_volatility']:
        result['role'] = '进攻型'
        result['basis'].append(f'历史股票相关性≥{rules["high_stock_correlation"]:.2f}，波动率≥{rules["low_volatility"]:.0%}。')
    else:
        result['basis'].append('现有数据未满足任一角色的配置阈值，保留“无法判定”。')
    return result
