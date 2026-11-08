"""Portable single-file HTML, embedded styles and data-derived SVG, traceable JSON."""
from pathlib import Path
import hashlib
import json
import math
import numpy as np
import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape, StrictUndefined
from markupsafe import Markup

from data_loader import ROOT, MISSING, now, write_json

RISK = '本报告仅作历史描述、条件核对和区间推导；不预测未来，不作分值评价，不提供投资操作指令。条件满足不代表应该买，触发卖出条件不代表必须卖。所有历史统计不代表未来表现。请自行判断并承担投资决策责任。'
PORTFOLIO_WARNING = '以上组合分析基于历史协方差，极端行情下相关性会上升，分散效果可能失效。'
STRESS_WARNING = '情景压力测试是“如果历史重演”的推演，不是概率预测；未来可能发生历史上没发生过的事。'
ACTIVE_WARNING = '历史超额不保证持续。主动基金的历史业绩受基金经理、风格、市场环境影响，对未来参考价值有限；基金经理变更仅提示重新审视，不判断其能力或后续表现。'
LIMITS = [
    '不能预测未来收益、不能给出上涨/下跌概率；',
    '不能替代你对基金经理、策略、市场环境的独立判断；',
    '不能消除幸存者偏差（只分析存续基金，已清盘基金不在样本内）；',
    '不能反映未来可能发生的历史上没出现过的极端事件；',
    '主动基金的统计特征不保证未来延续，尤其基金经理变更后；',
    '情景压力测试是历史重演假设，不是概率。',
    '场外申赎有确认及到账时滞；每日固定权重模拟未计交易费、滑点、暂停申赎与跨境额度限制。',
    '净值舍入、披露时滞、历史缺口与人民币汇率变化可能影响统计；缺失字段不能理解为零。'
]
FORMULAS = {
    '历史年化收益': '(末期净值 / 初期净值)^(年化日数 / 有效收益期数) − 1；货币基金365日，其他252日；不足30条有效净值时不计算年化。',
    '日收益': 'r[t] = NAV[t]/NAV[t−1]−1；通用分析先以前值填充共同日历，不向成立前回填。严格历史研究保留原始前后日期对齐模式。',
    '年化波动率': '样本日收益标准差(ddof=1) × √年化日数；货币基金全历史自然日取365，组合统一观测口径另列。',
    '最大回撤': 'min(NAV[t] / 截至t的净值峰值 − 1)，以负数展示；模拟组合及情景包含初始基准净值。',
    '夏普比率': '(日收益均值 − [(1+年无风险利率)^(1/年化日数)−1]) / 日收益样本标准差 × √年化日数；零波动时不可得。',
    'Calmar比率': '区间历史年化收益 / |最大回撤|；零回撤时不可得。',
    '滚动12个月胜率': '每个净值日期向前一个日历年取当日或此前最近观测(最多10个自然日)，正收益窗口数 / 有效窗口数；窗口重叠，非独立样本概率。',
    '最差完整年度': '上年年末净值为基准、当年末净值为终点；两端距年界不超过10个自然日，排除成立首个不完整年度及未结束年度。',
    '最长水下期': '净值严格低于此前历史峰值的最长连续观测日数，包含截至末日尚未恢复的区间；不是自然日数，不含恢复当日。',
    '组合纳入': '原持仓等比例缩减至(1−新增比例)，候选基金增加该比例；同一代码合并权重，每个共同观测日重置，现金零息。',
    '组合波动与路径': '√(wᵀΣw)，Σ为共同区间样本日收益协方差×年化日数；回撤与夏普由同一区间固定权重历史日收益路径计算，协方差本身不能决定回撤。',
    '角色': '先判冗余，再判对冲、防守、分散、进攻；阈值见参数。类别只描述历史暴露，不能代表未来有效性或基金优劣。',
    '历史情景': '取开始日前最近净值作为基准，结束日或此前最后净值作为终点；边界缺口>10自然日或内部缺口>16自然日则不计算。',
    '情景波动变化': '情景窗口年化波动 − 情景前60个(或配置值)日收益的年化波动，单位为百分点；前置窗口不足则不可得。',
    'Beta': 'Cov(基金日收益, 沪深300日收益)/Var(沪深300日收益)，最多最近756个(或配置值)共同区间观测；不替代其他指数。',
    '分红与复权': '优先按供应商日增长率连乘构建复权净值；累计净值仅作缺省降级，明确披露与分红再投资的差异。净值已含管理、托管等运作费用，不再重复扣除。'
}


def pct(value):
    return MISSING if value is None or not math.isfinite(float(value)) else f'{value:.2%}'


def number(value, digits=2):
    return MISSING if value is None or not math.isfinite(float(value)) else f'{value:,.{digits}f}'


def safe_json(value):
    if isinstance(value, dict):
        return {str(k): safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_json(v) for v in value]
    if isinstance(value, np.generic):
        return safe_json(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (pd.Timestamp, Path)):
        return str(value)
    return value


def code_version():
    source = hashlib.sha256()
    for path in sorted([*ROOT.glob('*.py'), *ROOT.glob('templates/*'), *ROOT.glob('config/*.json'), ROOT / 'requirements.txt']):
        source.update(str(path.relative_to(ROOT)).encode('utf-8'))
        source.update(path.read_bytes())
    return source.hexdigest()


def chart_svg(nav):
    if len(nav) < 2:
        return ''
    # Every observed value participates; no fitted or generated history.
    normalized = nav / nav.iloc[0]
    x = np.linspace(64, 954, len(nav))
    lo, hi = float(normalized.min()), float(normalized.max())
    span = max(hi - lo, 0.01)
    y = 205 - (normalized.to_numpy() - lo) / span * 160
    points = ' '.join(f'{a:.1f},{b:.1f}' for a, b in zip(x, y))
    lines = []
    for i in range(4):
        value = lo + span * i / 3
        ypos = 205 - i / 3 * 160
        lines.append(f'<line x1="64" y1="{ypos:.1f}" x2="954" y2="{ypos:.1f}" stroke="#e3eaf3"/><text x="48" y="{ypos+4:.1f}" text-anchor="end">{value:.2f}</text>')
    return Markup(f'<svg viewBox="0 0 980 250" role="img" aria-labelledby="nav-chart-title" xmlns="http://www.w3.org/2000/svg"><title id="nav-chart-title">区间净值轨迹，起点归一为1，仅展示已发生的历史</title><g font-size="13" fill="#616f84">{"".join(lines)}<text x="64" y="236">{nav.index[0]:%Y-%m-%d}</text><text x="954" y="236" text-anchor="end">{nav.index[-1]:%Y-%m-%d}</text></g><polyline points="{points}" fill="none" stroke="#2e5fce" stroke-width="2.2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg>')


def basic_fields(data):
    def get(*keys):
        for key in keys:
            value = data.info.get(key)
            if value is not None and str(value) not in ('nan', 'None', '--', ''):
                return str(value)
        return MISSING
    return [
        ('基金代码', data.code), ('成立日期', get('成立时间', '成立日期', '成立日期/规模')),
        ('净资产规模', get('净资产规模', '最新规模')), ('管理人', get('基金管理人', '基金公司')),
        ('基金经理', get('基金经理人', '基金经理')), ('跟踪指数', get('跟踪标的')),
        ('管理费', get('管理费率')), ('托管费', get('托管费率')), ('销售服务费', get('销售服务费率')),
        ('申购费', '见下方分档费率表' if not data.tables.get('申购费', pd.DataFrame()).empty else MISSING),
        ('赎回费', '见下方持有期限费率表' if not data.tables.get('赎回费', pd.DataFrame()).empty else MISSING)
    ]


def raw_table(data, key):
    df = data.tables.get(key, pd.DataFrame())
    return {'title': key, 'columns': [str(c) for c in df.columns], 'rows': df.fillna('缺失').astype(str).values.tolist()} if not df.empty else None


def dividend_info(data):
    df = data.tables.get('分红')
    split = data.tables.get('拆分')
    if df is None:
        count, latest = MISSING, MISSING
    elif df.empty:
        count, latest = '0（接口未返回记录，仍需核对公告）', '未返回分红日期'
    else:
        count = str(len(df))
        cols = [c for c in df.columns if '权益登记日' in str(c) or '除息日' in str(c)]
        dates = pd.to_datetime(df[cols[0]], errors='coerce') if cols else pd.Series(dtype='datetime64[ns]')
        latest = str(dates.max().date()) if dates.notna().any() else MISSING
    return {'count': count, 'latest': latest, 'splits': str(len(split)) if split is not None else MISSING}


def render_fund(data, classification, metrics, portfolio, stress, details, settings, benchmark_meta, output_dir, run_id, config_hashes, conditions=None, sizing=None, decision_config=None):
    from importlib.metadata import version
    audit = {'generated_at': now().isoformat(), 'code_version_sha256': run_id, 'config_hashes': config_hashes, 'parameters': settings, 'statistics_annualization_days': 365 if classification.category == '货币型' else settings['annualization_days'], 'random_seed': '未使用随机算法', 'dependencies': {name: version(name) for name in ('akshare', 'pandas', 'numpy', 'jinja2', 'pyarrow', 'scipy')}, 'fund': data.meta, 'benchmark': benchmark_meta, 'classification': classification.to_dict(), 'metrics': metrics, 'portfolio': portfolio, 'stress': stress, 'formulas': FORMULAS}
    audit.update(conditions=conditions, position_range=sizing, decision_config=decision_config)
    audit = safe_json(audit)
    context = {'code': data.code, 'name': data.info.get('基金名称', '名称数据不可得'), 'classification': classification.to_dict(), 'basic': basic_fields(data), 'metrics': metrics, 'data': data.meta, 'error': data.error, 'warnings': data.warnings, 'portfolio': portfolio, 'stress': stress, 'type_details': details, 'dividends': dividend_info(data), 'chart': chart_svg(data.nav), 'fee_tables': [t for key in ('申购费', '赎回费', '交易状态') if (t := raw_table(data, key))], 'audit': audit, 'audit_json': json.dumps(audit, ensure_ascii=False, indent=2, allow_nan=False), 'date': now().strftime('%Y-%m-%d'), 'risk': RISK, 'portfolio_warning': PORTFOLIO_WARNING, 'stress_warning': STRESS_WARNING, 'active_warning': ACTIVE_WARNING, 'limits': LIMITS, 'missing': MISSING, 'formulas': FORMULAS}
    context.update(conditions=conditions, sizing=sizing, decision_config=decision_config, thresholds=condition_thresholds(decision_config), buy_count=sum(r['status'] == '满足' for r in conditions['buy']))
    filename = f'{data.code}_{now():%Y%m%d}'
    target = Path(output_dir) / f'{filename}.html'
    target.parent.mkdir(parents=True, exist_ok=True)
    environment = template_env()
    target.write_text(environment.get_template('report.html').render(**context), encoding='utf-8')
    write_json(target.with_suffix('.json'), audit)
    return {'code': data.code, 'name': context['name'], 'category': classification.category, 'subtype': classification.subtype, 'metrics': metrics, 'role': portfolio['role'], 'correlation': portfolio.get('correlation_core'), 'filename': target.name, 'error': data.error, 'data_sha256': data.meta.get('nav_sha256'), 'code_version': run_id, 'fetched_at': data.meta.get('fetched_at'), 'config_hashes': config_hashes}


def condition_thresholds(c):
    p = lambda key: pct(c[key])
    targets = '；'.join(f'{role} {value:.0%}' for role, value in c['role_target_weights'].items())
    return {
        'B1': f'角色目标：{targets}；缺口≥{p("rebalance_threshold_pp")}（百分点）',
        'B2': f'指数：{c["valuation_history_years"]}年分位≤{p("valuation_percentile_threshold")}，中性界{p("valuation_neutral_threshold")}；主动：回撤≥{p("active_fund_drawdown_threshold")}，近高点界{p("trend_deviation_band")}；主动历史至少1年',
        'B3': f'{c["trend_ma_days"]}日均线；至少{c["min_ma_days_required"]}观测；偏离±{p("trend_deviation_band")}',
        'B4': f'上界仓位对应波动≤{p("portfolio_target_volatility")}+{p("volatility_buffer")}；至少1年历史及{c["min_data_days_required"]}条共同收益',
        'B5': f'实际角色权重 < 目标 − {p("rebalance_threshold_pp")}（百分点）；{targets}',
        'B6': '管理费+托管费 ≤ 同类费率中位数（动态阈值，见依据）',
        'S1': f'{c["correlation_window_days"]}日相关性≥{c["role_failure_correlation"]:.2f} 且近{c["valuation_history_years"]}年最低≤{c["role_failure_historical_low"]:.2f}',
        'S2': f'单位净值相对买入成本变化≤−{p("single_fund_stop_loss")}',
        'S3': f'实际权重 > 目标 + {p("rebalance_threshold_pp")}（百分点）；{targets}',
        'S4': f'经理{c["active_fund_manager_change_months"]}个月内变更 / 季度规模绝对变化>{p("active_fund_scale_change_ratio")} / 前十重仓换手>{p("active_fund_turnover_ratio")}',
        'S5': '同跟踪指数；费用与可比跟踪误差均严格较低（动态阈值，见依据）'
    }


def template_env():
    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent / 'templates')), autoescape=select_autoescape(['html', 'xml']), undefined=StrictUndefined)
    env.filters.update(pct=pct, number=number)
    env.policies['json.dumps_kwargs'] = {'sort_keys': True, 'ensure_ascii': False}
    return env


def render_summary(reports, output_dir, run_id):
    target = Path(output_dir) / f'summary_{now():%Y%m%d}.html'
    target.write_text(template_env().get_template('summary.html').render(reports=reports, date=now().strftime('%Y-%m-%d'), risk=RISK, run_id=run_id), encoding='utf-8')
    write_json(target.with_suffix('.json'), {'generated_at': now().isoformat(), 'reports': reports, 'code_version_sha256': run_id, 'order': '用户输入顺序'})
    return target
