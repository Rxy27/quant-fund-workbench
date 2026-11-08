"""Real AkShare data, bounded acquisition, checksummed parquet snapshots, no proxies."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from io import StringIO
import hashlib
import json
import subprocess
import sys
import tempfile
import re
import uuid
import time

import numpy as np
import pandas as pd
import streamlit as st

if __package__:
    from .network import bounded_call, remaining_timeout
else:
    from network import bounded_call, remaining_timeout

ROOT = Path(__file__).resolve().parent.parent
MISSING = '数据不可得'


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_api_payload(endpoint, parameters, cache_namespace, _timeout=15.0):
    """Cache only successful responses; a subprocess can be killed at deadline."""
    with tempfile.TemporaryDirectory(prefix='fund_checkup_') as task_dir:
        target = Path(task_dir) / 'response.json'
        try:
            proc = subprocess.run(
                [sys.executable, '-X', 'utf8', str(ROOT / 'core' / 'api_worker.py'), endpoint,
                 parameters, str(target)], capture_output=True,
                timeout=remaining_timeout(_timeout), encoding='utf-8', errors='replace')
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError('网络超时：AkShare 请求超过15秒等待预算') from exc
        if proc.returncode:
            error_id = uuid.uuid4().hex[:12]
            logs = Path(cache_namespace) / 'diagnostics'
            logs.mkdir(parents=True, exist_ok=True)
            (logs / f'{error_id}.log').write_text(proc.stderr, encoding='utf-8')
            lines = [line.strip() for line in proc.stderr.splitlines()
                     if re.search(r'(Error|Exception|Timeout)[:(]', line)]
            reason = lines[-1] if lines else '接口调用失败'
            raise RuntimeError(f'{reason[:200]}（诊断编号 {error_id}）')
        payload = target.read_text(encoding='utf-8')
        pd.read_json(StringIO(payload), orient='split')
        return payload


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai'))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def clean_nav(frame, value_col):
    if frame.empty or '净值日期' not in frame or value_col not in frame:
        return pd.Series(dtype=float, name='nav'), {'raw': len(frame), 'removed': len(frame)}
    df = pd.DataFrame({'date': pd.to_datetime(frame['净值日期'], errors='coerce').dt.normalize(), 'nav': pd.to_numeric(frame[value_col], errors='coerce')})
    df = df.dropna().loc[lambda x: np.isfinite(x.nav) & (x.nav > 0)]
    df = df.loc[df.date <= pd.Timestamp(now().date())]
    df = df.drop_duplicates('date', keep='last').sort_values('date')
    result = df.set_index('date').nav.astype(float)
    return result, {'raw': len(frame), 'removed': len(frame) - len(result), 'weekend_observations': int((result.index.day_of_week >= 5).sum())}


@dataclass
class FundData:
    code: str
    info: dict = field(default_factory=dict)
    nav: pd.Series = field(default_factory=lambda: pd.Series(dtype=float, name='nav'))
    tables: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    error: str = ''

    @property
    def available(self):
        return len(self.nav) >= 2


class DataLoader:
    def __init__(self, cache_dir=None, offline=False, refresh=False, timeout=15):
        self.cache = Path(cache_dir or ROOT / 'data')
        self.cache.mkdir(parents=True, exist_ok=True)
        self.offline, self.refresh, self.timeout = offline, refresh, timeout
        self.calls = []
        self.deadline = time.monotonic() + min(timeout, 15.0)

    def api(self, endpoint, **kwargs):
        key = hashlib.sha256(json.dumps([endpoint, kwargs], sort_keys=True).encode()).hexdigest()[:20]
        destination = self.cache / 'raw' / f'{endpoint}_{key}.json'
        manifest = destination.with_suffix('.meta.json')
        destination.parent.mkdir(exist_ok=True)
        def cached():
            meta = json.loads(manifest.read_text(encoding='utf-8'))
            if digest(destination) != meta['sha256']:
                raise ValueError('原始缓存校验失败')
            self.calls.append(meta)
            return pd.read_json(StringIO(destination.read_text(encoding='utf-8')), orient='split')
        if destination.exists() and manifest.exists() and not self.refresh:
            try:
                meta = json.loads(manifest.read_text(encoding='utf-8'))
                age = (now() - datetime.fromisoformat(meta['fetched_at'])).total_seconds()
                if self.offline or 0 <= age < 86400:
                    return cached()
            except (ValueError, OSError, KeyError):
                if self.offline:
                    raise
        if self.offline:
            raise RuntimeError('离线模式且没有有效缓存')
        try:
            timeout = remaining_timeout(min(self.timeout, self.deadline - time.monotonic()))
            parameters = json.dumps(kwargs, sort_keys=True)
            payload = bounded_call(
                lambda: fetch_api_payload(endpoint, parameters, str(self.cache.resolve()), _timeout=timeout),
                timeout=timeout, key=('fund-api', str(self.cache.resolve()), endpoint, parameters))
            destination.write_text(payload, encoding='utf-8')
            from importlib.metadata import version
            meta = {'endpoint': endpoint, 'parameters': kwargs, 'fetched_at': now().isoformat(), 'source': 'akshare', 'akshare_version': version('akshare'), 'sha256': digest(destination), 'file': str(destination.relative_to(self.cache))}
            write_json(manifest, meta)
            return cached()
        except (RuntimeError, TimeoutError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
            if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
                self.deadline = time.monotonic()
            if destination.exists() and manifest.exists():
                frame = cached()
                self.calls[-1] = {**self.calls[-1], 'refresh_failed': True,
                                  'error': '网络超时' if isinstance(exc, TimeoutError) else type(exc).__name__}
                return frame
            raise

    def load(self, code):
        from fund_classifier import classify
        if not re.fullmatch(r'\d{6}', code):
            raise ValueError('基金代码必须为6位数字')
        data = FundData(code)
        data.meta['attempted_at'] = now().isoformat()
        start_call = len(self.calls)
        def optional(key, endpoint, **kwargs):
            try:
                df = self.api(endpoint, **kwargs)
                data.tables[key] = df
                return df
            except Exception as exc:
                data.warnings.append(f'{key}：数据不可得（{type(exc).__name__}）；详见追溯记录。')
                data.meta.setdefault('fetch_failures', []).append({'field': key, 'endpoint': endpoint, 'error_type': type(exc).__name__, 'error': str(exc)[:260]})
                return pd.DataFrame()
        basic = optional('基本信息', 'fund_individual_basic_info_xq', symbol=code)
        if {'item', 'value'}.issubset(basic.columns):
            data.info.update({str(row['item']): row['value'] for row in basic.to_dict('records') if row['item'] not in ('基金评级', '评级机构')})
        overview = optional('档案', 'fund_overview_em', symbol=code)
        if not overview.empty:
            data.info.update({str(k): str(v) for k, v in overview.iloc[0].items() if pd.notna(v)})
        if '基金简称' in data.info:
            data.info['基金名称'] = data.info['基金简称']
        # A provider's generic empty page can be a nonempty table of dashes.
        data.info = {k: v for k, v in data.info.items() if v is not None and not re.fullmatch(r'[>\s\-—]*(?:（.*?）)?', str(v)) and str(v) not in ('nan', 'None', '<NA>')}
        classification = classify(data.info)
        unit = pd.DataFrame() if classification.category == '货币型' else optional('单位净值', 'fund_open_fund_info_em', symbol=code, indicator='单位净值走势')
        cumulative = pd.DataFrame() if classification.category == '货币型' else optional('累计净值', 'fund_open_fund_info_em', symbol=code, indicator='累计净值走势')
        u, unit_cleaning = clean_nav(unit, '单位净值')
        c, cleaning = clean_nav(cumulative, '累计净值')
        data.nav = c
        nav_type = '累计净值'
        if not u.empty and '日增长率' in unit:
            growth = unit.assign(date=pd.to_datetime(unit['净值日期'], errors='coerce')).drop_duplicates('date', keep='last').set_index('date')['日增长率']
            growth = pd.to_numeric(growth, errors='coerce').reindex(u.index) / 100
            valid = growth.iloc[1:]
            if len(u) >= 2 and valid.notna().all() and np.isfinite(valid).all() and (valid > -1).all():
                factors = growth.copy()
                factors.iloc[0] = 0.0  # base NAV, not an invented first-period return
                data.nav = (1 + factors).cumprod() * u.iloc[0]
                data.nav.name = 'nav'
                nav_type = '复权净值（由akshare公布日增长率逐日连乘）'
                _, cleaning = clean_nav(unit, '单位净值')
                data.warnings.append('复权序列按供应商已披露日增长率连乘；增长率的小数舍入可能累积误差。原始单位与累计净值均保留，便于复核。')
        classification = classify(data.info)
        if not data.available and re.match(r'^(51|15|56|58)', code):
            etf = optional('ETF净值', 'fund_etf_fund_info_em', fund=code, start_date='19900101', end_date=now().strftime('%Y%m%d'))
            c, cleaning = clean_nav(etf, '累计净值')
            data.nav = c
        if classification.category == '货币型' or not data.available or classification.category == '无法判定':
            # Unit NAV near 1 for money funds is not an investment return history.
            if classification.category == '货币型':
                data.nav = pd.Series(dtype=float, name='nav')
            income = optional('万份收益', 'fund_open_fund_info_em', symbol=code, indicator='每万份收益')
            if not income.empty and {'净值日期', '每万份收益'}.issubset(income):
                inc = income[['净值日期', '每万份收益']].copy()
                inc['净值日期'] = pd.to_datetime(inc['净值日期'], errors='coerce')
                inc['每万份收益'] = pd.to_numeric(inc['每万份收益'], errors='coerce')
                inc = inc.dropna().drop_duplicates('净值日期').sort_values('净值日期')
                inc = inc.loc[(inc['净值日期'] <= pd.Timestamp(now().date())) & np.isfinite(inc['每万份收益']) & (inc['每万份收益'] > -10000)]
                if len(inc) >= 1:
                    r = inc.set_index('净值日期')['每万份收益'] / 10000
                    r.iloc[0] = 0.0
                    data.nav = (1 + r).cumprod().rename('nav')
                    nav_type = '复权净值（万份收益按日再投资假设）'
                    data.info['基金类型'] = '货币型'
                    data.meta['classification_source'] = 'fund_open_fund_info_em 每万份收益有效披露'
                    classification = classify(data.info)
                    cleaning = {'raw': len(income), 'removed': len(income)-len(inc)}
                    optional('7日年化', 'fund_open_fund_info_em', symbol=code, indicator='7日年化收益率')
                    data.warnings.append('货币基金收益按万份收益再投资假设计算；未使用7日年化作为收益路径。自然日收益与股票交易日口径不同。')
        if data.available:
            optional('分红', 'fund_open_fund_info_em', symbol=code, indicator='分红送配详情')
            optional('拆分', 'fund_open_fund_info_em', symbol=code, indicator='拆分详情')
            for key, indicator in [('运作费用', '运作费用'), ('申购费', '申购费率（前端）'), ('赎回费', '赎回费率'), ('交易状态', '交易状态')]:
                # AkShare 1.20.27 accepts this indicator, but Eastmoney may use
                # the new heading 申购费率. The disclosure adapter handles both.
                optional(key, 'fund_fee_em', symbol=code, indicator=indicator)
            if data.tables.get('申购费', pd.DataFrame()).empty:
                # Eastmoney changed this heading; retain exact original rates and units.
                fallback = optional('申购费补充披露', 'disclosure_purchase_fee_em', symbol=code)
                if not fallback.empty:
                    data.tables['申购费'] = fallback
                    data.warnings = [w for w in data.warnings if not w.startswith('申购费：')]
                    data.meta.setdefault('recovered_fields', []).append('申购费')
            if classification.category == '指数型' or classification.active:
                optional('规模变化', 'disclosure_scale_em', symbol=code)
            if classification.active:
                optional('经理任职', 'disclosure_managers_em', symbol=code)
                for year in (now().year, now().year - 1, now().year - 2):
                    optional(f'重仓{year}', 'fund_portfolio_hold_em', symbol=code, date=str(year))
                    optional(f'行业{year}', 'fund_portfolio_industry_allocation_em', symbol=code, date=str(year))
        else:
            has_identity = bool(data.info.get('基金名称') or data.info.get('基金简称'))
            data.error = ('数据不可得：净值接口未返回可用历史，无法计算；基本信息存在。' if has_identity else '数据不可得：该基金不在 akshare 覆盖范围内，或本次接口请求失败；无法确认具体原因。')
        if nav_type == '累计净值' and data.available:
            data.warnings.append('累计净值是单位净值加累计现金分红，并非严格的分红再投资总回报；拆分可能改变可比性，统计仅描述该净值口径。')
        data.warnings.append('净值日期为供应商估值日期，不等于可成交日期、申购确认日或赎回到账日。原始净值不补造；跨资产分析以前值对齐缺失估值，填充值不代表可成交价格。')
        calls = self.calls[start_call:]
        data.meta.update({'code': code, 'source': 'akshare', 'nav_type': nav_type, 'cleaning': cleaning, 'calls': calls, 'cache_policy': '成功响应缓存24小时；联网等待预算15秒；失败显式回退校验通过的旧缓存',
                          'annualization_days': 365 if classification.category == '货币型' else 252,
                          'retry_required': not data.available or any(m.get('refresh_failed') for m in calls) or any(
                              f['field'] not in data.meta.get('recovered_fields', []) and re.search(r'超时|Timeout|timed out|Connection|离线|缓存', f.get('error_type', '') + f['error'], re.I)
                              for f in data.meta.get('fetch_failures', []))})
        if data.available:
            target = self.cache / f'{code}_nav.parquet'
            temp = target.with_suffix('.parquet.tmp')
            try:
                data.nav.to_frame().to_parquet(temp)
                temp.replace(target)
                data.meta['nav_sha256'] = digest(target)
            except OSError as exc:
                data.warnings.append(f'净值快照保存失败（{type(exc).__name__}），当前结果来自内存；请重试。')
                data.meta['retry_required'] = True
        if len(data.nav):
            data.meta.update({'start': str(data.nav.index[0].date()), 'end': str(data.nav.index[-1].date()), 'observations': len(data.nav), 'fetched_at': max((m['fetched_at'] for m in data.meta['calls']), default=None)})
        try:
            write_json(self.cache / f'{code}_manifest.json', data.meta)
        except OSError as exc:
            data.warnings.append(f'追溯文件保存失败（{type(exc).__name__}），请重试。')
            data.meta['retry_required'] = True
        return data

    def benchmark(self):
        try:
            df = self.api('stock_zh_index_daily', symbol='sh000300')
            frame = df.rename(columns={'date': '净值日期', 'close': 'price'})
            series, _ = clean_nav(frame, 'price')
            return series, {'name': '沪深300价格指数', 'code': '000300', 'source': 'akshare.stock_zh_index_daily', 'note': '价格指数不含现金分红；与基金复权收益存在口径差异', 'calls': self.calls[-1:]}
        except Exception as exc:
            reason = '网络超时，基准数据不可得' if isinstance(exc, TimeoutError) else f'数据不可得（{type(exc).__name__}）'
            return pd.Series(dtype=float), {'name': '沪深300价格指数', 'error': reason}
