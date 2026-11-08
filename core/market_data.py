"""Generic ETF analysis; the verified price cache is explicit backtest input."""
import hashlib
import json
import logging
import math
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
import requests

from .allocation import UNIVERSE
from .engine import load

DATA = Path(__file__).resolve().parent.parent/'data/etf_v3'

_last_request_time = {}
_quote_lock = threading.Lock()
_inflight_codes = set()
_logger = logging.getLogger(__name__)
ETF_PREFIXES = ('15', '51', '56', '58')
_quote_progress = ContextVar('quote_progress', default=None)


@contextmanager
def quote_progress(callback):
    """Publish transient batch progress to a caller-owned queue, without UI state."""
    token = _quote_progress.set(callback)
    try:
        yield
    finally:
        _quote_progress.reset(token)


def _publish_quotes(stage, codes, quotes=None, *, reason=None):
    callback = _quote_progress.get()
    if callback:
        callback(dict(stage=stage, codes=list(codes), quotes=dict(quotes or {}), reason=reason))


def _quote_failure_reason(exc):
    """Expose a useful category without leaking URLs, cookies or credentials."""
    name = type(exc).__name__
    if '400016' in str(exc) or 'xq_a_token' in str(exc):
        return '行情源要求登录'
    if 'Proxy' in name:
        return '代理连接失败'
    if 'Timeout' in name:
        return '行情源请求超时'
    if 'SSL' in name:
        return '行情源安全连接失败'
    if 'Connection' in name:
        return '行情源连接失败'
    if name in ('JSONDecodeError', 'KeyError', 'ValueError', 'TypeError'):
        return '行情源返回的数据不完整或格式异常'
    return '行情源请求失败'


def _xq_symbol(code):
    if code.startswith(('60', '68', '51', '58')):
        return 'SH' + code
    if code.startswith(('00', '30', '15')):
        return 'SZ' + code
    if code.startswith('8'):
        return 'BJ' + code
    return code


def _quote_record(name, price, change):
    price, change = float(price), float(change)
    if not math.isfinite(price) or price <= 0 or not math.isfinite(change):
        raise ValueError('行情含无效价格或涨跌幅')
    return dict(name=str(name), price=price, change_pct=change,
                update_time=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S'))


def _em_quotes(frame, codes):
    if not isinstance(frame, pd.DataFrame) or not {'代码', '名称', '最新价', '涨跌幅'}.issubset(frame.columns):
        raise ValueError('东财行情缺少必要字段')
    # IOPV is optional and must never stand in for the traded price.
    if 'IOPV实时估值' in frame.columns:
        _logger.debug('ETF 返回可选 IOPV 实时估值列')
    result = {}
    for row in frame[frame['代码'].isin(codes)].to_dict('records'):
        try:
            result[row['代码']] = _quote_record(row['名称'], row['最新价'], row['涨跌幅'])
        except Exception:
            _logger.warning('忽略无效东财行情 code=%s', row.get('代码'), exc_info=True)
    return result


def _fetch_etf_quotes(codes):
    """One small batch for requested ETFs, avoiding a full-market paginated scan."""
    symbols = [('sz' if code.startswith('15') else 'sh') + code for code in codes]
    response = requests.get('https://qt.gtimg.cn/q=' + ','.join(symbols), timeout=(1.5, 2.5))
    response.raise_for_status()
    response.encoding = 'gbk'
    result = {}
    for match in re.finditer(r'v_(?:sh|sz)(\d{6})="([^"]*)";', response.text):
        code, payload = match.groups()
        if code not in codes:
            continue
        try:
            fields = payload.split('~')
            if fields[2] != code:
                continue
            quote = _quote_record(fields[1], fields[3], fields[32])
            quote['market_time'] = datetime.strptime(fields[30], '%Y%m%d%H%M%S').strftime('%Y-%m-%d %H:%M:%S')
            result[code] = quote
        except (ValueError, IndexError):
            _logger.warning('忽略无效定向 ETF 行情 code=%s', code)
    return result


def _fetch_stock_quote(code):
    try:
        time.sleep(0.2)
        with _quote_lock:
            _last_request_time[code] = time.time()
        _logger.info('雪球个股请求 code=%s thread=%s', code, threading.current_thread().name)
        frame = ak.stock_individual_spot_xq(symbol=_xq_symbol(code))
        if isinstance(frame, pd.DataFrame):
            if {'item', 'value'}.issubset(frame.columns):
                fields = dict(zip(frame['item'], frame['value']))
            else:
                fields = frame.iloc[0].to_dict()
        else:
            fields = dict(frame)
        # AkShare versions that localize item names still enter the English-key adapter.
        aliases = {'现价': 'current', '涨幅': 'percent', '名称': 'name'}
        fields = {aliases.get(key, key): value for key, value in fields.items()}
        return code, _quote_record(fields['name'], fields['current'], fields['percent'])
    except Exception:
        _logger.warning('雪球失败，等待合并降级 code=%s', code, exc_info=True)
        return code, None


def get_realtime_quotes(codes: list[str]) -> dict:
    """Fetch quotes without UI/session state; reserve codes atomically across callers.

    Failed network attempts also cool down. Merely queued or filtered codes do not.
    One batch shares at most one Eastmoney full-market fallback.
    """
    reserved = []
    try:
        now = time.time()
        with _quote_lock:
            reserved = [code for code in dict.fromkeys(codes)
                        if now - _last_request_time.get(code, 0) >= 60
                        and code not in _inflight_codes]
            _inflight_codes.update(reserved)
        if not reserved:
            _publish_quotes('cooldown', codes)
            return {}
        etf_codes = [code for code in reserved if code.startswith(ETF_PREFIXES)]
        stock_codes = [code for code in reserved if code not in etf_codes]
        result, fallback = {}, set()
        if etf_codes:
            try:
                with _quote_lock:
                    for code in etf_codes:
                        _last_request_time[code] = time.time()
                _logger.info('ETF 合并请求 codes=%s', etf_codes)
                _publish_quotes('etf', etf_codes)
                try:
                    result.update(_fetch_etf_quotes(etf_codes))
                except Exception:
                    _logger.info('定向 ETF 行情失败，回退东财 ETF 列表', exc_info=True)
                _publish_quotes('quotes', result.keys(), result)
                missing_etfs = [code for code in etf_codes if code not in result]
                if missing_etfs:
                    frame = ak.fund_etf_spot_em()
                    if len(frame) < 100:
                        _logger.warning('ETF 数据截断 rows=%s，合并降级', len(frame))
                        fallback.update(missing_etfs)
                    else:
                        result.update(_em_quotes(frame, missing_etfs))
                        _publish_quotes('quotes', result.keys(), result)
                        fallback.update(set(missing_etfs) - result.keys())
            except Exception as exc:
                _logger.warning('ETF 请求失败，合并降级', exc_info=True)
                _publish_quotes('error', etf_codes, reason=_quote_failure_reason(exc))
                fallback.update(set(etf_codes) - result.keys())
        def accept_stock(stock_result):
            code, quote = stock_result
            if quote is None:
                fallback.add(code)
            else:
                result[code] = quote
                _publish_quotes('quotes', [code], {code: quote})

        if stock_codes:
            _publish_quotes('stocks', stock_codes)
        if len(stock_codes) > 2:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(_fetch_stock_quote, code) for code in stock_codes]
                for future in as_completed(futures):
                    accept_stock(future.result())
        else:
            for code in stock_codes:
                accept_stock(_fetch_stock_quote(code))
        if fallback:
            try:
                with _quote_lock:
                    for code in fallback:
                        _last_request_time[code] = time.time()
                _logger.info('东财全市场合并降级 count=1 codes=%s', sorted(fallback))
                _publish_quotes('fallback', fallback)
                result.update(_em_quotes(ak.stock_zh_a_spot_em(), fallback))
                _publish_quotes('quotes', result.keys(), result)
            except Exception as exc:
                _logger.warning('东财全市场降级失败', exc_info=True)
                _publish_quotes('error', fallback, reason=_quote_failure_reason(exc))
        return result
    except Exception:
        _logger.exception('实时行情获取失败')
        return {}
    finally:
        with _quote_lock:
            _inflight_codes.difference_update(reserved)


def load_etf(code, offline=True, *, for_backtest=False):
    if not for_backtest:
        from .analysis import load_fund
        return load_fund(code, offline)
    if code not in UNIVERSE:
        raise ValueError('不属于已核验四资产池')
    manifest = json.loads((DATA/'manifest.json').read_text(encoding='utf-8'))
    entry = manifest['assets'][code]
    path = DATA/f'{code}.parquet'
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
        raise ValueError(f'{code}行情哈希不符')
    frame = pd.read_parquet(path)
    return load('data_loader').FundData(code,
        info={'基金名称': entry['label']+' ETF', '基金类型': '交易所ETF'},
        nav=frame.close.rename('nav'),
        meta={'source': entry['source'], 'nav_type': '未复权ETF收盘价',
              'nav_sha256': entry['sha256'], 'observations': len(frame)},
        warnings=['使用已核验ETF价格缓存；未复权价格不含现金分红再投资。'])
