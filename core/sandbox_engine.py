"""Point-in-time fund replay and a monthly, equally funded cash-pool sandbox.

Only cumulative NAV is accepted. Signals use the last observation strictly
before execution; purchases at the next published NAV are a frictionless model,
not a claim about subscription availability or confirmation. Cash interest is
credited once per observed trading day. No live analysis adapter is used here.
"""
from dataclasses import dataclass
import json
import math
from pathlib import Path

import akshare as ak
import pandas as pd

from .network import bounded_call

ROOT = Path(__file__).resolve().parent.parent
STRATEGIES = ('smart', 'regular', 'lump_sum')


def calculate_xirr(cashflows, dates):
    """ACT/365 XIRR, pure Python Newton iteration, capped at 100 attempts.

    Newton's variable is log(1 + rate), keeping rates in the real domain > -1.
    Invalid inputs, zero derivatives, overflow and nonconvergence return None.
    Pool purchases are internal transfers and must not be passed as cashflows.
    """
    try:
        if len(cashflows) != len(dates) or len(cashflows) < 2:
            return None
        amounts = [float(c) for c in cashflows]
        if not all(math.isfinite(c) for c in amounts):
            return None
        # Aggregate same-day flows: an instantaneous round trip has no IRR.
        combined = {}
        for amount, date in zip(amounts, dates):
            day = pd.Timestamp(date).date()
            combined[day] = combined.get(day, 0.) + amount
        pairs = sorted((d, c) for d, c in combined.items() if c != 0)
        if (len(pairs) < 2 or not any(c < 0 for _, c in pairs)
                or not any(c > 0 for _, c in pairs)):
            return None
        scale = max(abs(c) for _, c in pairs)
        years = [(d - pairs[0][0]).days / 365. for d, _ in pairs]
        values = [c / scale for _, c in pairs]
        log_rate = math.log1p(.1)
        for _ in range(100):
            discounted = [c * math.exp(-t * log_rate) for c, t in zip(values, years)]
            npv = math.fsum(discounted)
            derivative = math.fsum(-t * c for t, c in zip(years, discounted))
            if derivative == 0:
                raise ZeroDivisionError('XIRR derivative is zero')
            # A fixed absolute tolerance falsely accepts enormous rates when
            # both discounted legs are tiny. Compare against their own scale.
            if abs(npv) <= 1e-10 * math.fsum(abs(c) for c in discounted):
                rate = math.expm1(log_rate)
                return rate if math.isfinite(rate) and rate > -1 else None
            # Damping bounds the Newton step without an unbounded search loop.
            step = max(-1., min(1., npv / derivative))
            log_rate -= step
        return None
    except (ValueError, TypeError, ArithmeticError, AttributeError):
        return None


def _validate_code(fund_code):
    if not isinstance(fund_code, str) or len(fund_code) != 6 or not fund_code.isascii() or not fund_code.isdigit():
        raise ValueError('基金代码须为六位数字')


def prepare_nav_history(frame, end_date=None):
    """Calculate a trailing MA on full prehistory BEFORE slicing a test period.

    AkShare's cumulative endpoint has no start-date parameter; requesting since
    inception provides more than a year's warmup whenever the fund has it.
    Never backfill missing NAV or the initial 199 missing moving averages.
    """
    frame = frame.rename(columns={'净值日期': 'date', '累计净值': 'cumulative_nav'}).copy()
    if not {'date', 'cumulative_nav'}.issubset(frame):
        raise ValueError('沙盘必须使用累计净值，不能用单位净值或实时估值替代')
    frame = frame[['date', 'cumulative_nav']]
    frame['date'] = pd.to_datetime(frame['date'], errors='coerce').dt.normalize()
    frame['cumulative_nav'] = pd.to_numeric(frame['cumulative_nav'], errors='coerce')
    frame = frame.dropna().sort_values('date').drop_duplicates('date', keep='last')
    frame = frame.loc[frame['cumulative_nav'].map(lambda v: math.isfinite(v) and v > 0)]
    # Quarter/year-end accounting NAV on weekends is not a trading session.
    frame = frame.loc[frame['date'].dt.day_of_week < 5]
    if end_date is not None:
        frame = frame.loc[frame['date'] <= pd.Timestamp(end_date).normalize()]
    frame['ma200'] = frame['cumulative_nav'].rolling(200, min_periods=200).mean()
    return frame.reset_index(drop=True)


def load_cumulative_nav(fund_code, *, offline=False, refresh=False, data_dir=None):
    """A dedicated, cumulative-only cache; never reuse ambiguous NAV caches."""
    _validate_code(fund_code)
    directory = Path(data_dir) if data_dir is not None else ROOT / 'data' / 'sandbox'
    path = directory / f'{fund_code}_cumulative.parquet'
    if path.exists() and not refresh:
        return prepare_nav_history(pd.read_parquet(path)), f'本地累计净值缓存 · {path.name}'
    if offline:
        raise ValueError('没有累计净值缓存，请取消“仅使用本地缓存”后重试')
    # The API returns the entire series, including >=1 year before start_date.
    raw = bounded_call(lambda: ak.fund_open_fund_info_em(
        symbol=fund_code, indicator='累计净值走势', period='成立来'),
        timeout=15., key=('sandbox-nav', fund_code))
    history = prepare_nav_history(raw)
    if history.empty:
        raise ValueError('接口未返回有效累计净值')
    directory.mkdir(parents=True, exist_ok=True)
    history[['date', 'cumulative_nav']].to_parquet(path, index=False)
    return history, 'AkShare · 天天基金 · 成立以来累计净值'


def prepare_valuation_history(frame):
    if frame is None or frame.empty:
        return pd.DataFrame(columns=['date', 'pe'])
    frame = frame.rename(columns={'日期': 'date', '滚动市盈率': 'pe'}).copy()
    if not {'date', 'pe'}.issubset(frame):
        raise ValueError('估值缓存缺少 date / pe 列')
    frame = frame[['date', 'pe']]
    frame['date'] = pd.to_datetime(frame['date'], errors='coerce').dt.normalize()
    frame['pe'] = pd.to_numeric(frame['pe'], errors='coerce')
    frame = frame.dropna().sort_values('date').drop_duplicates('date', keep='last')
    return frame.loc[frame['pe'].map(lambda v: math.isfinite(v) and v > 0)].reset_index(drop=True)


def load_valuation_history(fund_code):
    """Read only the local mapped PE cache, with no live valuation calls."""
    _validate_code(fund_code)
    try:
        mapping = json.loads((ROOT / 'config/index_mapping.json').read_text(encoding='utf-8'))
        index_code = mapping.get(fund_code, {}).get('code')
        if not index_code:
            return prepare_valuation_history(None), '无指数估值映射，降级为200日均线'
        if len(index_code) != 6 or not index_code.isascii() or not index_code.isdigit():
            raise ValueError('指数映射代码无效')
        path = ROOT / 'data' / f'valuation_cache_{index_code}.parquet'
        return prepare_valuation_history(pd.read_parquet(path)), f'本地估值缓存 · {path.name}'
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return prepare_valuation_history(None), f'估值数据不可用，降级为200日均线（{type(exc).__name__}）'


def _snapshot(nav, valuation, cutoff_date):
    cutoff = pd.Timestamp(cutoff_date).normalize()
    # These are the only data sources for a historical decision.
    past_nav = nav.loc[nav['date'] <= cutoff]
    past_pe = valuation.loc[valuation['date'] <= cutoff].tail(1250)
    nav_date = past_nav['date'].iloc[-1] if len(past_nav) else None
    price = float(past_nav['cumulative_nav'].iloc[-1]) if len(past_nav) else None
    ma = float(past_nav['ma200'].iloc[-1]) if len(past_nav) else math.nan
    deviation = price / ma - 1 if price is not None and math.isfinite(ma) else None
    pe_date = past_pe['date'].iloc[-1] if len(past_pe) else None
    # Carry only a recently observed PE forward; a stale cache must not decide
    # all future monthly purchases. 62 days accommodates the monthly cache.
    valid_pe = len(past_pe) >= 100 and (cutoff - pe_date).days <= 62
    percentile = float((past_pe['pe'] < past_pe['pe'].iloc[-1]).mean()) if valid_pe else None
    if percentile is not None:
        multiplier = 1.5 if percentile <= .3 else 1. if percentile <= .5 else .5
        b2 = '满足' if percentile <= .3 else '不满足'
        rule = 'PE分位'
        reason = f'历史PE分位 {percentile:.1%}，B2{b2}'
    else:
        multiplier = 1. if deviation is None or abs(deviation) < 1e-12 else 1.5 if deviation < 0 else .5
        b2 = '无法判定'
        rule = '200日均线' if deviation is not None else '基础定投'
        reason = ('PE分位不可用；均线历史不足，跳过趋势判断' if deviation is None else
                  'PE分位不可用；低于200日均线，按均值回归多投' if deviation < 0 else
                  'PE分位不可用；高于200日均线，按均值回归少投' if deviation > 0 else
                  'PE分位不可用；净值等于200日均线')
    trend = '历史不足' if deviation is None else '均线下方' if deviation < 0 else '均线上方' if deviation > 0 else '均线附近'
    recommendation = f'{reason}；建议按基础金额的 {multiplier:g} 倍计划投入，以资金池余额为上限。'
    if price is None:
        recommendation = '回溯日期前无累计净值，无法判断；不生成买入建议。'
    return dict(cutoff_date=cutoff, nav_date=nav_date, pe_date=pe_date,
                pe_percentile=percentile, pe_observations=len(past_pe), b2_status=b2,
                cumulative_nav=price, ma200=ma if math.isfinite(ma) else None,
                ma_deviation=deviation, trend=trend, rule=rule,
                multiplier=multiplier, recommendation=recommendation)


def replay_decision(nav_history, valuation_history, cutoff_date):
    """Dedicated historical cutoff entry point: rolling stats cannot see future rows."""
    nav = prepare_nav_history(nav_history, end_date=cutoff_date)
    valuation = prepare_valuation_history(valuation_history)
    return _snapshot(nav, valuation, cutoff_date)


@dataclass
class SandboxResult:
    daily: pd.DataFrame
    orders: pd.DataFrame
    metrics: dict
    snapshot: dict
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    monthly_amount: float
    market_return: float


def run_sandbox(nav_history, valuation_history, start_date, end_date, monthly_amount):
    """Three portfolios; smart/regular share monthly funding, not order amounts.

    Monthly funding is credited at the month's first observed trading date.
    The first-date calendar is built BEFORE the start-date slice, so starting
    midmonth never creates an extra contribution/purchase. Lump sum receives
    the same total budget up front; its funding timing differs explicitly.
    """
    start, end = pd.Timestamp(start_date).normalize(), pd.Timestamp(end_date).normalize()
    amount = float(monthly_amount)
    if pd.isna(start) or pd.isna(end) or end <= start:
        raise ValueError('结束日期必须晚于回溯日期')
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError('基础定投金额必须大于0且为有限数值')
    nav = prepare_nav_history(nav_history, end_date=end)
    valuation = prepare_valuation_history(valuation_history)
    monthly_first = nav.groupby(nav['date'].dt.to_period('M'))['date'].min()
    scheduled = set(monthly_first.loc[(monthly_first >= start) & (monthly_first <= end)])
    period = nav.loc[nav['date'] >= start]
    if len(period) < 2 or not scheduled:
        raise ValueError('区间内须有至少两个有效净值日期和一个月首交易日；月中起始请延长结束日期')
    principal = amount * len(scheduled)
    lump_shares = principal / float(period['cumulative_nav'].iloc[0])
    cash = {'smart': 0., 'regular': 0.}
    shares = {'smart': 0., 'regular': 0.}
    flow_amounts, flow_dates, records, orders = [], [], [], []
    contributed = 0.
    for row in period.itertuples():
        day, price = row.date, row.cumulative_nav
        for strategy in cash:
            # Deliberately executed on EVERY observed trading day.
            cash[strategy] *= (1 + 0.015 / 252)
        injection = amount if day in scheduled else 0.
        contributed += injection
        if injection:
            flow_amounts.append(-injection)
            flow_dates.append(day)
            # A closing NAV/PE stamped on execution day is not yet known when
            # subscribing. Use only information from the preceding calendar day.
            signal = _snapshot(nav, valuation, day - pd.Timedelta(days=1))
            order = dict(date=day, signal_cutoff=signal['cutoff_date'],
                         signal_nav_date=signal['nav_date'], signal_pe_date=signal['pe_date'],
                         rule=signal['rule'], multiplier=signal['multiplier'],
                         pe_percentile=signal['pe_percentile'], ma_deviation=signal['ma_deviation'])
            for strategy in cash:
                cash[strategy] += injection
                desired = amount * (signal['multiplier'] if strategy == 'smart' else 1.)
                purchase = min(cash[strategy], desired)
                cash[strategy] -= purchase
                shares[strategy] += purchase / price
                order[strategy + '_purchase'] = purchase
                order[strategy + '_cash'] = cash[strategy]
            orders.append(order)
        record = dict(date=day, cumulative_nav=price, injection=injection,
                      principal=contributed, lump_sum=price * lump_shares)
        for strategy in cash:
            record[strategy] = cash[strategy] + shares[strategy] * price
            record[strategy + '_cash'] = cash[strategy]
            record[strategy + '_shares'] = shares[strategy]
        records.append(record)
    daily = pd.DataFrame(records).set_index('date')
    metrics = {}
    for strategy in STRATEGIES:
        assets = daily[strategy]
        flows = daily['injection'] if strategy != 'lump_sum' else pd.Series(0., index=daily.index)
        # Chain daily returns after removing external contributions. Fresh
        # monthly capital cannot artificially reset the high-water mark.
        previous = assets.shift(1)
        returns = ((assets - flows) / previous - 1).where(previous > 0, 0.)
        unit_value = (1 + returns).cumprod()
        daily[strategy + '_unit_value'] = unit_value
        drawdown = max(0., float(-(unit_value / unit_value.cummax() - 1).min()))
        final = float(assets.iloc[-1])
        external = flow_amounts if strategy != 'lump_sum' else [-principal]
        dates = flow_dates if strategy != 'lump_sum' else [daily.index[0]]
        xirr = calculate_xirr([*external, final], [*dates, end])
        metrics[strategy] = dict(principal=principal, final_assets=final,
            final_cash=float(daily[strategy + '_cash'].iloc[-1]) if strategy in cash else 0.,
            profit=final - principal, cumulative_return=final / principal - 1,
            xirr=xirr, max_drawdown=drawdown, contributions=len(scheduled))
    return SandboxResult(daily=daily, orders=pd.DataFrame(orders), metrics=metrics,
        snapshot=replay_decision(nav, valuation, start), start_date=start, end_date=end,
        monthly_amount=amount,
        market_return=float(period['cumulative_nav'].iloc[-1] / period['cumulative_nav'].iloc[0] - 1))


def sandbox_conclusion(result):
    """Empathetic, but evidence-based: do not promise an advantage in a bear market."""
    smart, regular = result.metrics['smart'], result.metrics['regular']
    excess_assets = smart['final_assets'] - regular['final_assets']
    dd_reduction = regular['max_drawdown'] - smart['max_drawdown']
    if result.market_return < 0 and smart['profit'] < 0 and regular['profit'] < 0:
        if excess_assets > 0:
            return (f'虽然同期市场下跌导致绝对收益为负，但智能定投相比普通定投少亏了 {excess_assets:,.2f} 元，'
                    f'且期末资产多出 {excess_assets:,.2f} 元，体现了这一区间的防守价值。仍需承受亏损，低位多投也可能继续下跌。')
        return (f'这段下跌行情对持续投入并不容易：智能定投亏损 {-smart["profit"]:,.2f} 元，'
                f'期末资产比普通定投少 {-excess_assets:,.2f} 元。越跌越买在持续下行时也可能扩大亏损，需按承受能力安排投入。')
    annual = (result.end_date - result.start_date).days >= 365
    if annual and smart['xirr'] is not None and regular['xirr'] is not None and smart['xirr'] > regular['xirr']:
        dd_text = (f'且最大回撤降低了 {dd_reduction * 100:.2f} 个百分点' if dd_reduction >= 0 else
                   f'但最大回撤增加了 {-dd_reduction * 100:.2f} 个百分点')
        return (f'通过低位多投、高位少投，智能定投相比普通定投获得了 '
                f'{(smart["xirr"] - regular["xirr"]) * 100:.2f} 个百分点的超额年化收益，{dd_text}。')
    return (f'本区间智能定投期末资产比普通定投{"多" if excess_assets >= 0 else "少"} '
            f'{abs(excess_assets):,.2f} 元；策略表现取决于估值、行情路径及资金池余额，请结合回撤判断。')
