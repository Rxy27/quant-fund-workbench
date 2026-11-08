"""Historical calculations. Also preserve stdlib statistics for third-party imports."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import sysconfig

_spec = importlib.util.spec_from_file_location('_stdlib_statistics', Path(sysconfig.get_path('stdlib')) / 'statistics.py')
_stdlib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_stdlib)
for _name in _stdlib.__all__:
    globals()[_name] = getattr(_stdlib, _name)

import numpy as np
import pandas as pd

MIN_ANNUALIZATION_OBSERVATIONS = 30
SHORT_HISTORY_NOTE = '历史过短，无法计算年化'


def daily_returns(nav):
    return nav.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna()


def max_drawdown(nav):
    return float((nav / nav.cummax() - 1).min()) if len(nav) else None


def underwater(nav):
    if nav.empty:
        return 0, None
    mask = (nav / nav.cummax() - 1) < -1e-12
    longest = streak = 0
    for flag in mask:
        streak = streak + 1 if flag else 0
        longest = max(longest, streak)
    return longest, float(nav.iloc[-1] / nav.cummax().iloc[-1] - 1)


def return_metrics(returns, annualization_days=252, risk_free_rate=0.0):
    """For daily-rebalanced portfolios, include the initial wealth=1 in drawdown."""
    r = returns.dropna()
    if len(r) < 2:
        return {'volatility': None, 'max_drawdown': None, 'sharpe': None}
    std = float(r.std(ddof=1))
    daily_rf = (1 + risk_free_rate) ** (1 / annualization_days) - 1
    wealth = pd.Series(np.r_[1.0, np.cumprod(1 + r.to_numpy())])
    sufficient = len(r) >= MIN_ANNUALIZATION_OBSERVATIONS - 1
    return {'volatility': std * np.sqrt(annualization_days) if sufficient else None, 'max_drawdown': max_drawdown(wealth), 'sharpe': (float(r.mean()) - daily_rf) / std * np.sqrt(annualization_days) if sufficient and std > 1e-12 else None}


def describe(nav, annualization_days=252, risk_free_rate=0.0):
    nav = nav.replace([np.inf, -np.inf], np.nan).dropna().sort_index()
    nav = nav.loc[(nav > 0) & ~nav.index.duplicated(keep='last')]
    if len(nav) < 2:
        return {'annual_return': None, 'volatility': None, 'sharpe': None, 'calmar': None,
                'annualization_note': SHORT_HISTORY_NOTE, 'annualization_days': annualization_days,
                'observations': len(nav), 'return_observations': 0, 'short_history': True,
                'max_drawdown': max_drawdown(nav), 'longest_underwater': 0,
                'start': str(nav.index[0].date()) if len(nav) else None,
                'end': str(nav.index[-1].date()) if len(nav) else None}
    r = daily_returns(aligned_navs({'fund': nav}, annualization_days)['fund'])
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    sufficient = len(nav) >= MIN_ANNUALIZATION_OBSERVATIONS
    annual = float(np.expm1(np.log(nav.iloc[-1] / nav.iloc[0]) * annualization_days / len(r))) if sufficient and len(r) else None
    result = return_metrics(r, annualization_days, risk_free_rate)
    if not sufficient:
        result.update(volatility=None, sharpe=None)
    mdd = max_drawdown(nav)
    longest, current = underwater(nav)
    rolling = []
    # Calendar 12 months, prior observed NAV at/before each anniversary (<=10 calendar days).
    for end, val in nav.items():
        anniversary = end - pd.DateOffset(years=1)
        idx = nav.index.searchsorted(anniversary, side='right') - 1
        if idx >= 0 and (anniversary - nav.index[idx]).days <= 10:
            rolling.append(float(val / nav.iloc[idx] - 1))
    annual_rows = []
    for year in range(nav.index[0].year, nav.index[-1].year + 1):
        if year >= pd.Timestamp.now(tz='Asia/Shanghai').year:
            continue  # A December quote near year-end still does not complete the calendar year.
        start, end = pd.Timestamp(year=year, month=1, day=1), pd.Timestamp(year=year, month=12, day=31)
        # A full year requires last-year-end base and data through this year-end.
        before = nav.loc[:start - pd.Timedelta(days=1)]
        within = nav.loc[start:end]
        if before.empty or within.empty or (start - before.index[-1]).days > 10 or (end - within.index[-1]).days > 10 or end > nav.index[-1] + pd.Timedelta(days=10):
            continue
        if year == nav.index[-1].year and nav.index[-1].month < 12:
            continue
        annual_rows.append({'year': year, 'return': float(within.iloc[-1] / before.iloc[-1] - 1)})
    worst = min(annual_rows, key=lambda row: row['return']) if annual_rows else None
    result.update({'annual_return': annual, 'years': years, 'start': str(nav.index[0].date()), 'end': str(nav.index[-1].date()), 'observations': len(nav), 'return_observations': len(r), 'max_drawdown': mdd, 'calmar': annual / abs(mdd) if annual is not None and mdd is not None and abs(mdd) > 1e-12 else None, 'rolling_win_rate': float(np.mean(np.array(rolling) > 0)) if rolling else None, 'rolling_windows': len(rolling), 'worst_year': worst, 'annual_returns': annual_rows, 'longest_underwater': longest, 'current_drawdown': current, 'underwater': current < -1e-12, 'short_history': years < 3,
                   'annualization_days': annualization_days, 'annualization_note': '' if sufficient else SHORT_HISTORY_NOTE})
    return result


def aligned_navs(series_by_code, calendar_days=252):
    """Forward valuation on a common calendar; never backfill before inception.

    For trading-day portfolios, retain the union of observed weekdays. Weekend
    money income is compounded into the next retained observation.
    """
    if not series_by_code:
        return pd.DataFrame()
    frame = pd.concat(series_by_code, axis=1).sort_index().replace([np.inf, -np.inf], np.nan)
    if frame.empty:
        return frame
    if calendar_days == 365:
        frame = frame.reindex(pd.date_range(frame.index.min(), frame.index.max(), freq='D'))
    frame = frame.ffill()
    if calendar_days == 252:
        frame = frame.loc[frame.index.day_of_week < 5]
    return frame.dropna()


def aligned_returns(series_by_code, calendar_days=None):
    """Use forward-filled common dates when a calendar is explicitly selected.

    The default preserves the strict matching-interval research contract.
    """
    if calendar_days is not None:
        return aligned_navs(series_by_code, calendar_days).pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna()
    panels, previous = {}, {}
    for code, nav in series_by_code.items():
        panels[code] = nav.pct_change(fill_method=None)
        previous[code] = pd.Series(nav.index, index=nav.index).shift(1)
    frame = pd.concat(panels, axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    if frame.empty:
        return frame
    prev = pd.concat(previous, axis=1).reindex(frame.index)
    return frame.loc[prev.nunique(axis=1).eq(1) & prev.notna().all(axis=1)]


def beta_estimate(nav, benchmark, minimum=60, window=756, calendar_days=None):
    frame = aligned_returns({'fund': nav, 'index': benchmark}, calendar_days=calendar_days).tail(window)
    if len(frame) < minimum or frame['index'].var(ddof=1) < 1e-15:
        return {'beta': None, 'observations': len(frame), 'reason': '共同日收益不足或指数方差为零'}
    return {'beta': float(frame['fund'].cov(frame['index']) / frame['index'].var(ddof=1)), 'observations': len(frame), 'start': str(frame.index[0].date()), 'end': str(frame.index[-1].date())}


def type_details(data, classification, benchmark, bond_nav, settings):
    from data_loader import MISSING
    rows, notices, tables = [], [], []
    def add(label, value=None, note=''):
        rows.append({'label': label, 'value': MISSING if value is None else str(value), 'note': note})
    info, category = data.info, classification.category
    if category == '指数型':
        target = info.get('跟踪标的', MISSING)
        add('跟踪指数', target)
        if '沪深300' in str(target) and not benchmark.empty and data.available:
            aligned = aligned_returns({'fund': data.nav, 'index': benchmark}, calendar_days=settings.get('calendar_days'))
            te = float((aligned.fund - aligned['index']).std(ddof=1) * np.sqrt(settings['annualization_days'])) if len(aligned) >= settings['minimum_overlap'] else None
            add('年化跟踪误差', f'{te:.2%}' if te is not None else None, f'共同区间日收益差标准差×√{settings["annualization_days"]}；{len(aligned)}个同区间观测；沪深300为价格指数，不含分红，非官方总回报口径跟踪误差。联接基金现金及费率可带来偏差。')
        else:
            add('年化跟踪误差', note='对应跟踪指数的可比历史不可得，不用沪深300替代其他指数。')
        add('费率与同类对照', info.get('管理费率'), '本基金管理费如左；同类可核验费率样本数据不可得，不做排序或优劣判断。')
        scale = data.tables.get('规模变化', pd.DataFrame())
        if not scale.empty and '净资产规模（亿元）' in scale:
            tables.append({'title': '规模变化（净资产，亿元）', 'columns': list(scale.columns), 'rows': scale.astype(str).values.tolist()})
            first, last = scale['净资产规模（亿元）'].iloc[[0, -1]].astype(float)
            add('规模变化趋势', f'{first:.2f} → {last:.2f} 亿元', '所披露报告期的净资产变化；混合了净申赎与市场涨跌，不能据此推断资金流入。')
        else:
            add('规模变化趋势', note='仅有单期规模时不推断趋势。')
    if classification.active:
        tenure = data.tables.get('经理任职', pd.DataFrame())
        if not tenure.empty and {'任职日期', '离任日期', '基金经理'}.issubset(tenure):
            current = tenure.loc[tenure['离任日期'].astype(str).str.contains('至今|现任|--|nan', regex=True)]
            for row in current.to_dict('records'):
                start = pd.to_datetime(row['任职日期'], errors='coerce')
                if pd.isna(start) or data.nav.empty or start > data.nav.index[-1]:
                    continue
                segment = data.nav.loc[start:]
                yrs = (data.nav.index[-1] - start).days / 365.25
                add(f'{row["基金经理"]} · 本基金任职年限', f'{yrs:.2f} 年', f'任职起始日{start.date()}，截至净值末日；共同管理不可拆分个人贡献。')
                metrics = describe(segment, settings['annualization_days'], settings['risk_free_rate'])
                ar = metrics.get('annual_return')
                add('任职以来历史年化', f'{ar:.2%}' if ar is not None else None, '从任职日之后首个净值计算；非个人能力评价，不作延续判断。任期不足一年时年化折算会放大短期变化。')
            if current.empty:
                add('基金经理任职年限 / 任职以来年化', note='未获得可确认的现任任职日期；不以经理从业年限替代本基金任职。')
        else:
            add('基金经理任职年限 / 任职以来年化', note='未获得可核验的本基金任职记录。')
        add('换手率', note='当前接口未提供可核验的报告期换手率。')
        holds = [df for key, df in data.tables.items() if key.startswith('重仓') and not df.empty]
        industries = [df for key, df in data.tables.items() if key.startswith('行业') and not df.empty]
        if holds:
            df = pd.concat(holds, ignore_index=True).drop_duplicates()
            if {'季度', '占净值比例'}.issubset(df):
                quarter = sorted(df['季度'].astype(str).unique())[-1]
                latest = df.loc[df['季度'].astype(str).eq(quarter)].copy()
                latest['weight'] = pd.to_numeric(latest['占净值比例'].astype(str).str.rstrip('%'), errors='coerce')
                largest = latest.sort_values('weight', ascending=False).head(10)
                add('前十大重仓集中度', f'{largest.weight.sum():.2f}%' if len(largest) == 10 and largest.weight.notna().all() else None, f'{quarter}，占基金净资产；披露可能滞后，少于十只不冒充前十合计。')
            else:
                add('前十大重仓集中度')
        else:
            add('前十大重仓集中度')
        if industries:
            df = pd.concat(industries, ignore_index=True).drop_duplicates()
            if '季度' not in df and '截止时间' in df:
                df = df.rename(columns={'截止时间': '季度'})
            if {'季度', '行业类别', '占净值比例'}.issubset(df):
                df['weight'] = pd.to_numeric(df['占净值比例'].astype(str).str.rstrip('%'), errors='coerce')
                pivot = df.pivot_table(index='季度', columns='行业类别', values='weight', aggfunc='first').sort_index()
                latest = pivot.iloc[-1].dropna()
                add('行业集中度（最大行业）', f'{latest.idxmax()} · {latest.max():.2f}%' if len(latest) else None, '行业占基金净资产；不与股票仓位占比混淆。')
                cutoff = (data.nav.index[-1] - pd.DateOffset(years=2)) if data.available else pd.Timestamp('1900-01-01')
                def quarter_end(label):
                    import re
                    match = re.search(r'(\d{4})年(\d)[季度]', str(label))
                    if match:
                        return pd.Timestamp(year=int(match[1]), month=int(match[2]) * 3, day=1) + pd.offsets.MonthEnd(0)
                    return pd.to_datetime(str(label), errors='coerce')
                valid_periods = [period for period in pivot.index if pd.notna(quarter_end(period)) and quarter_end(period) >= cutoff]
                pivot = pivot.loc[valid_periods]
                tables.append({'title': '近两年已披露行业分布（占净资产%，缺失不等于零）', 'columns': ['披露期', '已披露行业 / 占净值比例'], 'rows': [[str(i), '；'.join(f'{industry} {value:.2f}%' if pd.notna(value) else f'{industry} 缺失' for industry, value in row.items())] for i, row in pivot.iterrows()]})
                notices.append('风格漂移提示：以上仅展示已披露行业变化；行业缺失不当作零，披露频率及持仓覆盖不支持连续风格或能力判断。')
            else:
                add('行业集中度 / 近两年行业分布')
                notices.append('风格漂移提示：行业披露字段不满足可比口径，无法检验变化；主动基金风格仍可能漂移。')
        else:
            add('行业集中度 / 近两年行业分布')
            notices.append('风格漂移提示：近两年可比行业数据不可得，无法检验漂移；主动基金风格可能随经理与市场变化。')
    if category == '债券型':
        add('修正久期（年）', info.get('修正久期'), '未从名称、债券期限或历史涨跌反推久期。')
        add('信用等级分布', info.get('信用等级分布'), '评级披露滞后且不能消除信用风险。')
        add('杠杆率', info.get('杠杆率'), '需要报告期总资产/净资产口径，缺失不假设为100%。')
        notices.append('利率敏感性提示：利率上行可能压低债券估值；久期、信用利差、转债权益敞口与杠杆会影响幅度。未取得久期时不输出利率冲击数值。')
    if category == 'QDII':
        add('币种与汇率', info.get('币种', '人民币计价口径以净值披露为准'), '人民币净值同时受底层资产价格及汇率影响；没有美元净值，不拆分汇率贡献。')
        add('额度与申赎限制', note='最新跨境额度不可核验；下方交易状态仅为数据拉取时快照，以销售渠道及基金公告为准。')
        notices.append('境外交易时差、不同休市日及净值确认时滞会扭曲同日相关性；未把净值披露日期视作境外成交日，未通过移位选择较高相关性。')
    if category == '货币型':
        for key, label in [('7日年化', '7日年化（历史折算口径）'), ('万份收益', '每万份收益（元）')]:
            df = data.tables.get(key, pd.DataFrame())
            add(label, ' / '.join(str(v) for v in df.iloc[-1].values) if not df.empty else None, '为已披露历史值；非该报告推算的后续收益。')
        add('规模', info.get('净资产规模', info.get('最新规模')))
    if category == '商品型':
        add('跟踪标的', info.get('跟踪标的'))
        for label, ref in [('与股票相关性（沪深300价格指数）', benchmark), ('与债券相关性（003358基金）', bond_nav)]:
            frame = aligned_returns({'fund': data.nav, 'ref': ref}, calendar_days=settings.get('calendar_days')) if data.available and not ref.empty else pd.DataFrame()
            corr = float(frame.fund.corr(frame.ref)) if len(frame) >= settings['minimum_overlap'] else None
            add(label, f'{corr:.3f}' if corr is not None and np.isfinite(corr) else None, f'同前后日期的共同日收益，共{len(frame)}条；相关性并非恒定。')
    if category in ('FOF', 'REITs', '混合型', '无法判定'):
        notices.append({'FOF': 'FOF底层持仓穿透、双层费用及底层估值滞后数据不可得；净值统计不能替代底层资产分析。', 'REITs': 'REITs包含经营、流动性及分派风险；场内价格与披露净值不同，不能混用。', '混合型': '混合基金股债配置可能变化；固定历史协方差不代表后续资产配置。', '无法判定': '无法判定，需人工确认；仅使用通用统计，不推断基金资产类别。'}[category])
    return {'rows': rows, 'notices': notices, 'tables': tables}
