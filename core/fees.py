"""B6 fee comparison supplied by the user, adapted to AkShare and UI records."""
import pandas as pd
import akshare as ak
import time
import os
import json
import tempfile
import concurrent.futures
import threading
from pathlib import Path
import streamlit as st

from .network import bounded_call, remaining_timeout, network_limited

FEE_TIMEOUT_SEC = 15.0


@st.cache_data(ttl=86400, show_spinner=False)
def _fetch_fee_table(endpoint, fund_code):
    if endpoint == 'fund_info_ths':
        return ak.fund_info_ths(fund_code)
    return ak.fund_fee_em(fund_code, indicator='运作费用')


@st.cache_data(ttl=86400, show_spinner=False)
def _fetch_fund_catalog():
    return ak.fund_name_em()

# 全局冷却防刷与锁
_last_fee_fail_time = 0.0
_fee_lock = threading.Lock()

def clean_val(v):
    """安全转换数值，捕获 ValueError 和 TypeError"""
    try:
        return float(str(v).split("%", 1)[0].replace(" ", "").strip()) / 100
    except (ValueError, TypeError):
        return None

@network_limited
def get_fund_info_and_fees(fund_code: str) -> dict:
    try:
        return bounded_call(lambda: _get_fund_info_and_fees(fund_code),
                            timeout=FEE_TIMEOUT_SEC, key=('fund-fees', fund_code))
    except FeeUnavailable as exc:
        return exc.data
    except TimeoutError:
        return {'mgmt': None, 'custody': None, 'type': '', 'network_timeout': True}


class FeeUnavailable(Exception):
    def __init__(self, data):
        self.data = data


@st.cache_data(ttl=86400, show_spinner=False)
def _get_fund_info_and_fees(fund_code: str) -> dict:
    """获取单只基金的费率，并顺便尝试获取基金类型"""
    fee_dict = {"mgmt": None, "custody": None, "type": ""}
    
    # 首选同花顺接口（位置参数兼容）
    try:
        df = bounded_call(lambda: _fetch_fee_table('fund_info_ths', fund_code),
                          timeout=FEE_TIMEOUT_SEC, key=('fees-ths', fund_code))
        if not df.empty:
            for _, row in df.iterrows():
                key = str(row.iloc[0]).replace(" ", "")
                if "管理费" in key:
                    fee_dict['mgmt'] = clean_val(row.iloc[1])
                elif "托管费" in key:
                    fee_dict['custody'] = clean_val(row.iloc[1])
                elif "基金类型" in key or "投资类型" in key:
                    fee_dict['type'] = str(row.iloc[1])
            if fee_dict['mgmt'] is not None and fee_dict['custody'] is not None:
                return fee_dict
    except TimeoutError:
        fee_dict['network_timeout'] = True
    except Exception:
        pass
        
    # 备选天天基金接口（位置参数兼容）
    try:
        df = bounded_call(lambda: _fetch_fee_table('fund_fee_em', fund_code),
                          timeout=FEE_TIMEOUT_SEC, key=('fees-em', fund_code))
        for _, row in df.iterrows():
            # AkShare returns operating fees as adjacent label/value pairs.
            for i in range(0, len(row) - 1, 2):
                key = str(row.iloc[i])
                if "管理费" in key:
                    fee_dict['mgmt'] = clean_val(row.iloc[i + 1])
                elif "托管费" in key:
                    fee_dict['custody'] = clean_val(row.iloc[i + 1])
    except TimeoutError:
        fee_dict['network_timeout'] = True
    except Exception:
        pass
    if fee_dict['mgmt'] is None or fee_dict['custody'] is None:
        raise FeeUnavailable(fee_dict)
    return fee_dict

def get_peer_median_fee(base_type: str, offline: bool = False) -> tuple:
    """获取同类费率中位数，返回 (数值, 是否兜底, 是否旧缓存, 样本量说明)"""
    global _last_fee_fail_time
    cache_path = Path(__file__).resolve().parent.parent / "data/fee_median_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    
    old_median = None
    if cache_path.exists():
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            if base_type in cache:
                old_median = cache[base_type]['median']
                if time.time() - cache[base_type]['updated_at'] < 30 * 86400:
                    return old_median, False, False, "缓存命中"
        except Exception:
            pass

    if offline:
        return (old_median, False, True, '离线缓存') if old_median is not None else (None, False, False, '离线模式无同类费率缓存')

    with _fee_lock:
        if time.time() - _last_fee_fail_time < 300:
            return (old_median, False, True, "冷却降级") if old_median else (0.006, True, False, "冷却兜底")

    median_fee = None
    sample_note = ""
    
    try:
        all_funds = bounded_call(_fetch_fund_catalog, key='fees-catalog')
        if base_type == "指数型":
            peer_df = all_funds[all_funds['基金类型'].str.contains("指数", na=False) & ~all_funds['基金类型'].str.contains("债", na=False)]
        elif base_type == "债券型":
            peer_df = all_funds[all_funds['基金类型'].str.contains("债", na=False)]
        else:
            peer_df = all_funds[all_funds['基金类型'].str.contains("混合", na=False)]

        if len(peer_df) >= 5:
            sample_n = min(10, len(peer_df))
            peer_codes = peer_df.sample(n=sample_n)['基金代码'].tolist()
        else:
            peer_codes = all_funds.sample(n=5)['基金代码'].tolist()
            
        fees = []
        # 临时池，并发限制为 2 防封禁
        # Submit from two daemon coordinators; no non-daemon executor workers
        # can survive indefinitely inside an unresponsive provider.
        from queue import Queue, Empty
        futures = []

        def sample_worker():
            while True:
                try:
                    code, future = queue.get_nowait()
                except Empty:
                    return
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(get_fund_info_and_fees(code))
                    except Exception as exc:
                        future.set_exception(exc)

        queue = Queue()
        for code in peer_codes:
            future = concurrent.futures.Future()
            futures.append(future)
            queue.put((code, future))
        from contextvars import copy_context
        for _ in range(2):
            context = copy_context()
            threading.Thread(target=context.run, args=(sample_worker,), daemon=True).start()
        
        # 5 秒硬超时，取已完成的，不管没跑完的
        done, not_done = concurrent.futures.wait(futures, timeout=remaining_timeout(5.0))
        if not done and not_done:
            sample_note = '网络超时'
        
        for future in done:
            try:
                res = future.result()
                if res['mgmt'] is not None and res['custody'] is not None:
                    fees.append(res['mgmt'] + res['custody'])
            except Exception:
                pass
                
        # 放弃孤儿线程，防主线程卡死
        for future in not_done:
            future.cancel()
                    
        if fees:
            median_fee = pd.Series(fees).median()
            if len(fees) < 3:
                sample_note = f" (注: 样本量仅 {len(fees)} 个，代表性偏弱)"
                
    except TimeoutError:
        sample_note = '网络超时'
    except Exception:
        pass

    if median_fee is not None:
        try:
            cache_data = {}
            if cache_path.exists():
                with open(cache_path, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
            cache_data[base_type] = {"median": median_fee, "updated_at": time.time()}
            
            # 原子写入，防止 JSON 损坏
            fd, temp_path = tempfile.mkstemp(dir=cache_path.parent, suffix=".json")
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False)
            os.replace(temp_path, cache_path)
        except Exception:
            pass
        return median_fee, False, False, sample_note

    with _fee_lock:
        _last_fee_fail_time = time.time()

    if old_median is not None:
        return old_median, False, True, '使用过期缓存' + ('（网络超时）' if sample_note == '网络超时' else '')
        
    # 兜底值贴近真实市场中位数
    fallback = {"指数型": 0.006, "债券型": 0.002, "混合型": 0.012}
    return fallback.get(base_type, 0.008), True, False, '非实时经验估算值' + ('（网络超时）' if sample_note == '网络超时' else '')


def buy_condition_6(fund_data, portfolio, config) -> dict:
    # 防止字典 KeyError
    fund_code = fund_data.get('code')
    if not fund_code:
        return {"status": "无法判定", "reason": "缺少基金代码", "evidence": {}, "data_source": "AkShare:同花顺/天天基金费率"}
    
    f_type = str(fund_data.get('type', '') or fund_data.get('category', ''))
    if fund_data.get('active') or any(k in f_type for k in ['主动', '混合', 'QDII']):
        return {'status': '不适用', 'reason': '主动基金与 QDII 不使用同类费率对比',
                'evidence': {}, 'data_source': 'AkShare:同花顺/天天基金费率'}
    offline = config.get('_valuation_offline', False)
    base_info = {'mgmt': fund_data.get('management_fee'),
                 'custody': fund_data.get('custody_fee'), 'type': f_type}
    if not offline and (base_info['mgmt'] is None or base_info['custody'] is None):
        base_info = get_fund_info_and_fees(fund_code)
    f_type = str(fund_data.get('type', '') or fund_data.get('category', '') or base_info.get('type', ''))
    
    if not f_type:
        return {"status": "无法判定", "reason": "无法获取基金类型", "evidence": {}, "data_source": "AkShare:同花顺/天天基金费率"}
        
    if fund_data.get("active") or any(k in f_type for k in ["主动", "混合", "QDII"]):
        return {"status": "不适用", "reason": "主动基金与 QDII 不使用同类费率对比", "evidence": {}, "data_source": "AkShare:同花顺/天天基金费率"}
        
    if base_info['mgmt'] is None or base_info['custody'] is None:
        reason = '网络超时，无法获取本基金管理费/托管费' if base_info.get('network_timeout') else '无法获取本基金管理费/托管费'
        return {"status": "无法判定", "reason": reason, "evidence": {}, "data_source": "AkShare:同花顺/天天基金费率"}
        
    my_total = base_info['mgmt'] + base_info['custody']
    
    if "指数" in f_type and "债" in f_type:
        base_type = "债券型"
        type_label = "债券型指数"
    elif "指数" in f_type:
        base_type = "指数型"
        type_label = "指数型(含股票/海外/其他)"
    elif "债" in f_type:
        base_type = "债券型"
        type_label = "债券型"
    else:
        base_type = "其他"
        type_label = "混合/其他"
        
    peer_median, is_fallback, is_old_cache, note = (get_peer_median_fee(base_type, offline=True)
        if offline else get_peer_median_fee(base_type))
    if peer_median is None:
        return {'status': '无法判定', 'reason': note, 'evidence': {},
                'data_source': '本地同类费率缓存'}
    
    status = "满足" if my_total <= peer_median else "不满足"
    
    source_tag = ""
    if is_fallback:
        source_tag = f" (注: {note})"
    elif is_old_cache:
        source_tag = f" (注: {note})"
    elif note:
        source_tag = note
        
    evidence = f"本基金费率: {my_total*100:.2f}% (管理{base_info['mgmt']*100:.2f}% + 托管{base_info['custody']*100:.2f}%) | {type_label}中位数: {peer_median*100:.2f}%{source_tag}"
    reason = "费率处于同类合理范围" if status == "满足" else "费率高于同类中位数"

    return {
        "status": status,
        "reason": reason,
        "evidence": {
            "summary": evidence,
            "management_fee": base_info['mgmt'],
            "custody_fee": base_info['custody'],
            "management_plus_custody": my_total,
            "peer_median": peer_median,
            "peer_scope": type_label,
            "is_fallback": is_fallback,
            "is_old_cache": is_old_cache,
            "note": note,
        },
        "data_source": "非实时经验估算值" if is_fallback else "AkShare:同花顺/天天基金费率；同类随机抽样中位数（30天缓存）",
    }
