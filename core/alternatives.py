"""S5: sample cheaper off-exchange funds and compare daily return differences."""
import concurrent.futures
from contextvars import copy_context
import re

import akshare as ak
import numpy as np
import pandas as pd
import streamlit as st

from .fees import get_fund_info_and_fees, _fetch_fee_table, _fetch_fund_catalog
from .network import bounded_call, network_limited, remaining_timeout

# Include the supplied ETF prefixes and exchange-traded LOF code families.
EXCHANGE_PREFIXES = ('15', '16', '50', '51', '56', '58')
EXCHANGE_REASON = '本基金为场内ETF或LOF，S5的场外净值对比逻辑不适用于场内品种'


@st.cache_data(ttl=86400, show_spinner=False)
def _get_all_funds_list():
    return _fetch_fund_catalog()


@st.cache_data(ttl=86400, show_spinner=False)
def _get_net_values(fund_code):
    return ak.fund_open_fund_info_em(symbol=fund_code, indicator='累计净值走势')


@network_limited
def get_s5_alternative_result(fund_code: str) -> dict:
    try:
        return bounded_call(lambda: _get_s5_recent(fund_code), key=('s5-analysis', fund_code))
    except TimeoutError as exc:
        return dict(status='无法判定', triggered=False, reason=str(exc), evidence='')


class IncompleteSample(Exception):
    def __init__(self, data):
        self.data = data


@st.cache_data(ttl=300, show_spinner=False)
def _get_s5_recent(fund_code):
    # Short cooldown prevents repeated sampling when a provider is unavailable.
    try:
        return _get_s5_cached(fund_code)
    except IncompleteSample as exc:
        return exc.data


@st.cache_data(ttl=86400, show_spinner=False)
def _get_s5_cached(fund_code):
    data = _calculate_s5_result(fund_code)
    if data['status'] == '无法判定':
        raise IncompleteSample(data)
    return data


def clear_s5_cache():
    _get_s5_recent.clear()
    _get_s5_cached.clear()


def _calculate_s5_result(fund_code: str) -> dict:
    """Return the supplied S5 status/reason/evidence plus auditable sample counts."""
    metadata = {}

    def result(status, reason, evidence='', triggered=False):
        return dict(status=status, triggered=triggered, reason=reason,
                    evidence=evidence, **metadata)

    # This must precede fees, benchmark lookup and sampling: none may mask it.
    if fund_code.startswith(EXCHANGE_PREFIXES):
        return result('无法判定', EXCHANGE_REASON)

    base_info = get_fund_info_and_fees(fund_code)
    if base_info.get('mgmt') is None or base_info.get('custody') is None:
        return result('无法判定', '无法获取本基金费率')
    my_total_fee = base_info['mgmt'] + base_info['custody']
    metadata['fund_fee'] = my_total_fee

    benchmark_str = ''
    try:
        df_info = bounded_call(lambda: _fetch_fee_table('fund_info_ths', fund_code),
                               key=('s5-benchmark', fund_code))
        if df_info is not None and not df_info.empty:
            for _, row in df_info.iterrows():
                if '业绩比较基准' in str(row.iloc[0]):
                    benchmark_str = str(row.iloc[1])
                    break
    except Exception:
        pass
    if benchmark_str.strip().lower() in ('', 'nan', 'none'):
        return result('无法判定', '业绩比较基准为空')
    metadata['benchmark'] = benchmark_str

    index_name = ''
    match = re.search(r'([\u4e00-\u9fa5A-Za-z0-9]{2,10}(?:指数|指))', benchmark_str)
    if match:
        index_name = match.group(1).replace('指数', '').replace('指', '').strip()
    else:
        common_indices = ['沪深300', '中证500', '中证1000', '创业板指', '科创50',
                          '上证50', '中证100', '纳斯达克100', '中证白酒', '中证红利']
        index_name = next((name for name in common_indices if name in benchmark_str), '')
    if not index_name:
        return result('无法判定', '未能从基准中解析出标准指数格式',
                      f'业绩基准原文：{benchmark_str}')
    metadata['index_name'] = index_name

    try:
        all_funds = bounded_call(_get_all_funds_list, key='s5-catalog')
        peer_df = all_funds[
            all_funds['基金简称'].str.contains(index_name, na=False, regex=False)
            & (all_funds['基金代码'] != fund_code)
            & ~all_funds['基金代码'].str.startswith(EXCHANGE_PREFIXES)]
        peer_count = len(peer_df)
        metadata['peer_count'] = peer_count
        if not peer_count:
            return result('未触发', f'基于基金简称匹配未找到包含【{index_name}】的场外同类基金',
                          f'匹配规则：基金简称包含指数名且排除场内品种。样本池为空。基准原文：{benchmark_str}')
    except Exception as exc:
        return result('无法判定', f'获取市场列表失败: {exc}')

    sample_n = min(10, peer_count)
    peer_codes = peer_df.sample(n=sample_n)['基金代码'].tolist()
    metadata.update(sampled_count=sample_n, sampled_codes=peer_codes)
    cheaper_peers = []
    valid_fees = 0
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    futures = {}
    try:
        for code in peer_codes:
            context = copy_context()
            futures[executor.submit(context.run, get_fund_info_and_fees, code)] = code
        done, _ = concurrent.futures.wait(futures, timeout=remaining_timeout(5.0))
        for future in done:
            try:
                res = future.result()
                if res.get('mgmt') is not None and res.get('custody') is not None:
                    valid_fees += 1
                    peer_fee = res['mgmt'] + res['custody']
                    if peer_fee < my_total_fee:
                        cheaper_peers.append((futures[future], peer_fee))
            except Exception:
                pass
    except TimeoutError as exc:
        return result('无法判定', str(exc))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    metadata.update(fee_result_count=valid_fees, cheaper_count=len(cheaper_peers))
    pool_note = (f'基于简称【{index_name}】匹配 {peer_count} 只场外同类，随机抽样 {sample_n} 只，'
                 f'5秒窗口内实际拿到 {valid_fees} 只的有效费率结果。')
    if not valid_fees:
        return result('无法判定', '抽样基金费率暂不可得，可稍后重试', pool_note)
    if not cheaper_peers:
        return result('未触发', '未发现费率更低的场外同类基金',
                      f'当前基金费率 {my_total_fee*100:.2f}%。{pool_note}')

    def fetch_returns(code):
        try:
            df = bounded_call(lambda: _get_net_values(code), key=('s5-nav', code))
            if df is not None and not df.empty:
                df = df.rename(columns={'净值日期': '日期'}).copy()
                df['日期'] = pd.to_datetime(df['日期'])
                return df.set_index('日期').sort_index()['累计净值'].tail(252).pct_change(fill_method=None).dropna()
        except Exception:
            pass
        return pd.Series(dtype=float)

    my_returns = fetch_returns(fund_code)
    if my_returns.empty:
        return result('无法判定', '本基金净值数据暂不可得，可稍后重试', pool_note)

    cheaper_peers.sort(key=lambda peer: peer[1])
    skipped_candidates = []
    for peer_code, peer_fee in cheaper_peers:
        peer_returns = fetch_returns(peer_code)
        aligned = pd.concat([my_returns, peer_returns], axis=1, keys=['my', 'peer']).dropna()
        if len(aligned) < 50:
            skipped_candidates.append(peer_code)
            continue
        return_diff_std = (aligned['peer'] - aligned['my']).std() * np.sqrt(252)
        if return_diff_std < .02:
            metadata.update(candidate_code=peer_code, candidate_fee=peer_fee,
                            return_diff_std=float(return_diff_std), common_observations=len(aligned),
                            skipped_candidates=skipped_candidates)
            note = (f' (注：另有 {len(skipped_candidates)} 只候选因净值数据不可得或共同交易日不足跳过对比)'
                    if skipped_candidates else '')
            return result('触发', f'发现更优替代线索: {peer_code}',
                          f'候选基金 {peer_code} 费率 {peer_fee*100:.2f}% 低于本基金 '
                          f'{my_total_fee*100:.2f}%，且最近252条净值的共同区间'
                          f'【收益差异标准差】仅为 {return_diff_std*100:.2f}%，走势高度相似。{pool_note}{note}', True)

    metadata['skipped_candidates'] = skipped_candidates
    if len(skipped_candidates) == len(cheaper_peers):
        return result('无法判定', '低费率候选净值不可得或共同交易日不足50条', pool_note)
    skip_note = f' 其中 {len(skipped_candidates)} 只候选因数据不满足条件而跳过。' if skipped_candidates else ''
    return result('未触发', '存在低费率候选，但走势差异均偏大',
                  f'已完成对比的低费率候选收益差异标准差均不低于 2%，替换可能改变收益特征。{pool_note}{skip_note}')


def sell_condition_5(fund_data, portfolio, config) -> dict:
    fund_code = fund_data.get('code')
    if not fund_code:
        res = dict(status='无法判定', triggered=False, reason='缺少基金代码', evidence='')
    elif fund_code.startswith(EXCHANGE_PREFIXES):
        res = get_s5_alternative_result(fund_code)
    elif config.get('_valuation_offline', False):
        res = dict(status='无法判定', triggered=False,
                   reason='仅使用本地缓存，S5需要联网抽样；请取消仅使用本地缓存后重新分析', evidence='')
    else:
        res = get_s5_alternative_result(fund_code)
    return dict(status=res['status'], reason=res['reason'],
                evidence={'summary': res['evidence'],
                          **{key: value for key, value in res.items()
                             if key not in ('status', 'triggered', 'reason', 'evidence')}},
                data_source='AkShare:同花顺业绩基准/管理费/托管费；天天基金场外累计净值；简称随机抽样')
