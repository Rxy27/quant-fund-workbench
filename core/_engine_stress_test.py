"""Full-window historical episodes and user-conditioned sensitivity arithmetic."""
import numpy as np
import pandas as pd

from statistics import daily_returns, max_drawdown, beta_estimate


def inception_date(info):
    for key in ('成立时间', '成立日期', '成立日期/规模'):
        text = str(info.get(key, '')).split('/')[0].strip().replace('年', '-').replace('月', '-').replace('日', '')
        value = pd.to_datetime(text, errors='coerce')
        if pd.notna(value):
            return value
    return None


def validate_scenarios(config):
    for scene in config['historical']:
        start, end = pd.Timestamp(scene['start']), pd.Timestamp(scene['end'])
        if start > end:
            raise ValueError('历史情景的开始日期不得晚于结束日期')
    for scene in config.get('hypothetical', []):
        if scene['kind'] == 'equity':
            if not np.isfinite(scene['shock']) or scene['shock'] < -1:
                raise ValueError('股票冲击必须是有限小数且不小于-1')
        elif scene['kind'] == 'rates':
            if not np.isfinite(scene['shock_bp']):
                raise ValueError('利率冲击bp必须为有限数')
        else:
            raise ValueError('自定义敏感性情景kind仅支持equity或rates')


def episode(nav, start, end, annualization_days, pre_event_window):
    base = nav.loc[:start - pd.Timedelta(days=1)]
    inside = nav.loc[start:end]
    if base.empty or inside.empty:
        return {'error': '历史数据未覆盖完整窗口，无法评估'}
    if (start - base.index[-1]).days > 10 or (end - inside.index[-1]).days > 10:
        return {'error': '窗口边界净值缺失，无法评估'}
    # Detect large unobserved holes, without claiming to know a foreign-market calendar.
    segment = pd.concat([base.tail(1), inside])
    if segment.index.to_series().diff().dt.days.max() > 16:
        return {'error': '窗口存在超过16个自然日的净值缺口，无法评估'}
    r = daily_returns(segment)
    if len(r) < 2:
        return {'error': '窗口观测不足，无法评估'}
    before = daily_returns(base).tail(pre_event_window)
    volatility = float(r.std(ddof=1) * np.sqrt(annualization_days))
    prior_volatility = float(before.std(ddof=1) * np.sqrt(annualization_days)) if len(before) == pre_event_window else None
    return {'error': '', 'return': float(inside.iloc[-1] / base.iloc[-1] - 1), 'max_drawdown': max_drawdown(segment), 'volatility': volatility, 'prior_volatility': prior_volatility, 'volatility_change': volatility - prior_volatility if prior_volatility is not None else None, 'base_date': str(base.index[-1].date()), 'end_date': str(inside.index[-1].date()), 'observations': len(r)}


def stress_analysis(data, benchmark, scenarios, settings):
    history = []
    inception = inception_date(data.info)
    for scene in scenarios['historical']:
        start, end = pd.Timestamp(scene['start']), pd.Timestamp(scene['end'])
        row = dict(scene)
        if inception is not None and inception > start:
            row['error'] = '该基金未经历此情景，无法评估'
        elif not data.available:
            row['error'] = '数据不可得，无法评估'
        elif data.nav.index[0] >= start and (inception is None or inception < start):
            row['error'] = '成立日期或情景前净值不足，无法评估；未使用相似基金替代'
        else:
            row.update(episode(data.nav, start, end, settings['annualization_days'], settings['pre_event_window']))
        comparison = episode(benchmark, start, end, 252, settings['pre_event_window']) if not benchmark.empty else {'error': '指数数据不可得'}
        row['benchmark_return'] = comparison.get('return')
        row['benchmark_max_drawdown'] = comparison.get('max_drawdown')
        row['benchmark_error'] = comparison['error']
        row['benchmark_base_date'] = comparison.get('base_date')
        row['benchmark_end_date'] = comparison.get('end_date')
        history.append(row)
    hypothetical = []
    beta = beta_estimate(data.nav, benchmark, settings['minimum_overlap'], settings['sensitivity_window'], settings.get('calendar_days')) if data.available and not benchmark.empty else {'beta': None, 'observations': 0, 'reason': '基金或沪深300净值不可得'}
    for scene in scenarios.get('hypothetical', []):
        row = {**scene, 'estimate': None, 'error': '', 'basis': ''}
        if scene['kind'] == 'equity':
            row['basis'] = '线性 Beta 估算，未考虑非线性关系和极端相关性变化；β×用户输入的指数涨跌幅。Beta基于日收益，不推断未来三个月路径，未计截距。'
            row['sensitivity'] = beta
            if beta['beta'] is None:
                row['error'] = '数据不可得：历史Beta无法估计'
            else:
                row['estimate'] = beta['beta'] * scene['shock']
        else:
            duration = data.info.get('修正久期')
            row['basis'] = '久期估算，未考虑信用利差变化；−修正久期×利率变化（100bp=0.01）；未计凸性、杠杆变化。'
            try:
                duration = float(duration)
                if not np.isfinite(duration) or duration < 0:
                    raise ValueError('invalid duration')
                row['estimate'] = -duration * scene['shock_bp'] / 10000
                row['sensitivity'] = {'modified_duration': duration}
            except (ValueError, TypeError):
                row['error'] = '数据不可得：未取得可核验的修正久期，不计算冲击数值'
        if row['estimate'] is not None and row['estimate'] < -1:
            row['estimate'] = None
            row['error'] = '线性外推超过净值下界，模型不适用；不输出该数值'
        row['warning'] = '这是基于历史敏感性的推演，不是概率预测。'
        hypothetical.append(row)
    return {'historical': history, 'hypothetical': hypothetical, 'beta': beta}
