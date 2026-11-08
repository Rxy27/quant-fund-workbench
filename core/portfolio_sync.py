"""Cumulative-NAV baselines and cent-rounded portfolio valuation, without UI."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import logging
import math
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
import streamlit as st
from requests import RequestException

from .network import bounded_call, network_budget
from .market_data import get_realtime_quotes

ETF_MAPPING_PATH = Path(__file__).resolve().parent.parent / 'config/intraday_etf_mapping.json'


def display_amount(holding):
    """Temporary valuations are display-only, never a new monetary baseline."""
    if holding.get('is_calibrated', True) is False:
        return holding['estimated_amount']
    return holding['amount']


def _positive(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError('净值必须为正的有限数')
    return value


def _shares_amount(shares, nav):
    return float((Decimal(str(shares)) * Decimal(str(nav))).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP))


def fetch_official_unit_nav(code, nav_date):
    """Match the official unit NAV to the cumulative baseline's exact date."""
    history = bounded_call(lambda: ak.fund_open_fund_info_em(
        symbol=code, indicator='单位净值走势', period='成立来'),
        timeout=4., key=('portfolio-sync-unit-nav', code))
    dates = pd.to_datetime(history['净值日期'], errors='coerce').dt.strftime('%Y-%m-%d')
    matching = history.loc[dates == nav_date, '单位净值']
    if len(matching) != 1:
        raise ValueError('缺少对应日期的官方单位净值，停止校准')
    return _positive(matching.iloc[0])


def _estimate_from_table(holding, frame):
    if not isinstance(frame, pd.DataFrame) or '基金代码' not in frame.columns:
        raise ValueError('东财估值表缺少基金代码')
    frame = frame.copy()
    frame['基金代码'] = frame['基金代码'].astype(str).str.zfill(6)
    rows = frame[frame['基金代码'] == holding['code']]
    if rows.empty:
        raise ValueError(f'东财当前返回的 {len(frame)} 只基金估值中未覆盖 {holding["code"]}，暂无可用估值')
    if len(rows) != 1:
        raise ValueError('东财返回了重复基金记录，停止估算')
    # AkShare prefixes these columns with their actual valuation date.
    columns = [c for c in frame.columns if str(c).endswith('-估算数据-估算值')]
    candidates = []
    for column in columns:
        day = date.fromisoformat(str(column).split('-估算数据-')[0])
        if date.fromisoformat(holding['base_date']) < day <= today():
            candidates.append((day, column))
    if not candidates:
        raise ValueError('东财未提供基准日之后的有效估值')
    _, column = max(candidates)
    estimated_nav = _positive(rows.iloc[0][column])
    if holding.get('shares') is not None:
        return _shares_amount(holding['shares'], estimated_nav)
    # base_nav is cumulative NAV, whereas Eastmoney estimates UNIT NAV.
    # Use the published unit NAV at precisely base_date, never mix the two.
    unit_column = holding['base_date'] + '-单位净值'
    if unit_column in rows.columns:
        unit_nav = _positive(rows.iloc[0][unit_column])
    else:
        unit_nav = fetch_official_unit_nav(holding['code'], holding['base_date'])
    return holding['amount'] * estimated_nav / unit_nav


def today():
    return datetime.now(ZoneInfo('Asia/Shanghai')).date()


@st.cache_data(ttl=86400, show_spinner=False)
def get_fund_nav_history(code):
    """Cache published cumulative history by the exact fund-code argument."""
    try:
        frame = bounded_call(lambda: ak.fund_open_fund_info_em(
            symbol=code, indicator='累计净值走势', period='成立来'),
            timeout=6., key=('portfolio-sync-nav', code))
    except RequestException as exc:
        raise ConnectionError('网络连接失败，请检查网络或代理设置后重试。') from exc
    if frame is None or not {'净值日期', '累计净值'}.issubset(frame.columns):
        raise ValueError('接口未返回累计净值')
    return frame


def fetch_latest_cumulative_nav(code):
    """Read the latest published row; invalid new values must never look current."""
    frame = get_fund_nav_history(code=code)
    dates = pd.to_datetime(frame['净值日期'], errors='coerce', format='ISO8601')
    values = pd.to_numeric(frame['累计净值'], errors='coerce')
    valid = dates.notna() & (dates.dt.date <= today())
    history = pd.DataFrame({'date': dates[valid], 'nav': values[valid]}).sort_values('date')
    if history.empty:
        raise ValueError('没有截至今天的有效累计净值')
    latest = history.iloc[-1]
    if pd.isna(latest['nav']) or not math.isfinite(latest['nav']) or latest['nav'] <= 0:
        raise ValueError('最新累计净值无效，必须为正的有限数')
    return float(latest['nav']), latest['date'].date().isoformat()


def _baseline_valid(holding):
    try:
        nav = holding['base_nav']
        day = date.fromisoformat(holding['base_date'])
        return (math.isfinite(nav) and nav > 0 and
                day.isoformat() == holding['base_date'] and day <= today())
    except (KeyError, ValueError, TypeError):
        return False


def initialize_baselines(portfolio):
    """Upgrade state; only published official NAVs may establish a baseline."""
    updated = deepcopy(portfolio)
    with network_budget():
        for holding in updated['holdings']:
            holding.setdefault('is_calibrated', True)
            if _baseline_valid(holding):
                continue
            try:
                get_fund_nav_history.clear(code=holding['code'])
                nav, day = fetch_latest_cumulative_nav(holding['code'])
                if nav is None or nav <= 0 or not math.isfinite(nav):
                    raise ValueError('最新累计净值无效，必须为正的有限数')
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    '%s 累计净值基准初始化失败，保留原基准，等待官方数据：%s',
                    holding['code'], exc)
                continue
            holding.update(base_nav=nav, base_date=day)
    return updated


def sync_portfolio(portfolio):
    """Return an independent candidate; the caller commits it atomically."""
    updated = deepcopy(portfolio)
    warnings_list = []
    count = 0
    estimates_loaded = False
    estimates = None
    quotes = None
    etf_mapping = None
    with network_budget():
        for holding in updated['holdings']:
            try:
                code = holding['code']
                get_fund_nav_history.clear(code=code)
                latest_nav, latest_nav_date = fetch_latest_cumulative_nav(code)
                if latest_nav is None or latest_nav <= 0 or not math.isfinite(latest_nav):
                    warnings_list.append(f'{code} 最新累计净值无效，已保留原市值，请稍后重试。')
                    continue
                latest_date = date.fromisoformat(latest_nav_date)
                if latest_date > today():
                    raise ValueError('净值日期无效')
                shares = holding.get('shares')
                unit_nav = fetch_official_unit_nav(code, latest_nav_date) if shares is not None else None
                share_amount = _shares_amount(shares, unit_nav) if shares is not None else None
                base_nav = holding.get('base_nav')
                if base_nav is None or base_nav == 0:
                    holding.update(base_nav=latest_nav, base_date=latest_nav_date,
                                   is_calibrated=True)
                    holding.pop('estimated_amount', None)
                    if shares is not None:
                        holding.update(amount=share_amount, base_unit_nav=unit_nav)
                    count += 1
                    continue
                if not _baseline_valid(holding):
                    raise ValueError('累计净值基准无效，请重新保存持仓')
                diff_ratio = abs(latest_nav / base_nav - 1)
                # Treat floating-point roundoff at exactly 20% as the boundary.
                if diff_ratio > 0.20 and not math.isclose(diff_ratio, 0.20, rel_tol=0., abs_tol=1e-12):
                    warnings_list.append(
                        f'⚠️ 基金 {code} 单日波动超过20%，疑似异常数据，已保留原市值。'
                        '（注：若您持有的是原油、黄金、跨境QDII等特殊品种，请手动核对后再决定是否更新）。')
                    continue
                if latest_date < date.fromisoformat(holding['base_date']):
                    warnings_list.append(f'{code} 数据源净值日期早于持仓基准，已保留原市值。')
                    continue
                if shares is not None and (latest_nav_date != holding['base_date'] or
                        diff_ratio != 0 or holding['amount'] != share_amount or
                        holding.get('base_unit_nav') != unit_nav):
                    holding.update(amount=share_amount, base_nav=latest_nav,
                                   base_date=latest_nav_date, base_unit_nav=unit_nav, is_calibrated=True)
                    holding.pop('estimated_amount', None)
                    count += 1
                    continue
                if latest_nav_date == holding['base_date'] and diff_ratio == 0:
                    current_day = today()
                    friday = current_day - timedelta(days=max(0, current_day.weekday() - 4))
                    if (holding.get('is_calibrated', True) and
                            (latest_date == current_day or
                             current_day.weekday() >= 5 and latest_date >= friday)):
                        # Today's official close, or Friday's close on a weekend,
                        # already includes the last observed domestic session.
                        continue
                    try:
                        if etf_mapping is None:
                            etf_mapping = json.loads(ETF_MAPPING_PATH.read_text(encoding='utf-8'))
                        etf = etf_mapping.get(code)
                        if etf:
                            if quotes is None:
                                codes = list(dict.fromkeys(etf_mapping[h['code']] for h in
                                    updated['holdings'] if h['code'] in etf_mapping))
                                # Mark attempted before calling: failures must not retry per holding.
                                quotes = {}
                                quotes = bounded_call(lambda: get_realtime_quotes(codes),
                                    timeout=4., key=('portfolio-sync-quotes', tuple(codes)))
                            change_pct = float(quotes[etf]['change_pct'])
                            if not math.isfinite(change_pct) or change_pct <= -100:
                                raise ValueError('ETF 涨跌幅无效')
                            market_time = quotes[etf].get('market_time')
                            if market_time:
                                market_date = datetime.fromisoformat(market_time).date()
                                if not date.fromisoformat(holding['base_date']) < market_date <= today():
                                    raise ValueError('ETF 行情日期未晚于官方基准，停止重复估算')
                            if shares is not None:
                                estimated_nav = Decimal(str(unit_nav)) * (1 + Decimal(str(change_pct)) / 100)
                                estimate = _shares_amount(shares, estimated_nav)
                            else:
                                estimate = holding['amount'] * (1 + change_pct / 100)
                        else:
                            if not estimates_loaded:
                                estimates_loaded = True
                                estimates = bounded_call(lambda: ak.fund_value_estimation_em(symbol='全部'),
                                    timeout=4., key='portfolio-sync-all-estimates')
                            estimate = _estimate_from_table(holding, estimates)
                        if not math.isfinite(estimate) or estimate < 0:
                            raise ValueError('预估金额无效')
                        holding.update(is_calibrated=False, estimated_amount=round(estimate, 2))
                    except Exception as exc:
                        warning = (f'{code} 数据源尚未更新（最新净值日期 {latest_nav_date}），'
                                   f'盘中估算失败，已保留原市值和校准状态：{exc}')
                        warnings_list.append(warning)
                        logging.getLogger(__name__).warning(warning)
                    continue
                new_amount = round(holding['amount'] * (latest_nav / base_nav), 2)
                if not math.isfinite(new_amount):
                    raise ValueError('推算市值超出有效金额范围')
                holding.update(amount=new_amount, base_nav=latest_nav, base_date=latest_nav_date,
                               is_calibrated=True)
                holding.pop('estimated_amount', None)
                count += 1
            except Exception as exc:
                reason = ' '.join(str(exc).split())[:160]
                warnings_list.append(f"{holding['code']} 同步失败，已保留原金额：{reason}")
    # A market move changes account equity, not cash. In particular, a fully
    # invested account must remain valid after an upward NAV movement.
    delta = math.fsum(h['amount'] for h in updated['holdings']) - math.fsum(
        h['amount'] for h in portfolio['holdings'])
    if delta:
        updated['account_total'] = round(portfolio['account_total'] + delta, 2)
    return updated, warnings_list, count
