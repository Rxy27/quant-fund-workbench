import streamlit as st
import pandas as pd
import akshare as ak
import time
from datetime import datetime
import json
from pathlib import Path
from .network import bounded_call

ROOT = Path(__file__).resolve().parent.parent

# ==========================================
# 1. 全局配置与流控
# ==========================================
# 实测耗时记录见 outputs/b2_valuation/latency.json
# 依据：基于实测 P90 耗时计算，max(P90 * 1.5, 15.0)
AKSHARE_TIMEOUT_SEC = 15.0 

# 全局流控（5分钟冷却），防止狂点触发孤儿线程
_last_ak_fail_time = 0.0

# ==========================================
# 2. 数据拉取与缓存层
# ==========================================
@st.cache_data(ttl=86400, show_spinner=False)
def fetch_akshare_pe_threaded(symbol: str) -> pd.DataFrame:
    return bounded_call(lambda: ak.stock_index_pe_lg(symbol=symbol),
                        timeout=AKSHARE_TIMEOUT_SEC, key=('valuation', symbol))

def get_valuation_data(symbol: str, index_code: str, offline: bool = False):
    """
    核心获取函数：流控防刷 + 缓存优先 + 抛异常兜底 + 读写主键对齐
    """
    global _last_ak_fail_time
    (ROOT / "data").mkdir(exist_ok=True)
    filepath = ROOT / "data" / f"valuation_cache_{index_code}.parquet"

    if offline:
        if not filepath.exists():
            raise ValueError("离线模式下无本地估值缓存，请取消仅使用本地缓存后重试。")
        df = pd.read_parquet(filepath).rename(columns={"日期": "date", "滚动市盈率": "pe"})
        df["date"] = pd.to_datetime(df["date"])
        return df, "本地Parquet缓存 (离线模式)"

    # 降级分支 1：全局冷却期内，直接走本地缓存
    if time.time() - _last_ak_fail_time < 300:
        if filepath.exists():
            df = pd.read_parquet(filepath)
            df = df.rename(columns={'日期': 'date', '滚动市盈率': 'pe'})
            df['date'] = pd.to_datetime(df['date'])
            return df, "本地Parquet缓存 (网络冷却中)"
        raise ValueError("网络处于冷却期，且本地无缓存文件。")

    try:
        # 尝试联网拉取（优先走缓存）
        df = bounded_call(lambda: fetch_akshare_pe_threaded(symbol),
                          timeout=AKSHARE_TIMEOUT_SEC, key=('valuation-cache', symbol))
        if df is None:
            raise RuntimeError("AkShare 返回空数据")
            
        # 立即归一化中文列名，防止后续 KeyError
        df = df.rename(columns={'日期': 'date', '滚动市盈率': 'pe'})
        if 'date' not in df.columns or 'pe' not in df.columns:
            raise ValueError(f"列名归一化失败，现有列: {list(df.columns)}")
            
        df['date'] = pd.to_datetime(df['date'])
        df['index_code'] = index_code
        # 必须在 concat 之前打时间戳
        df['updated_at'] = time.time() 

        # 合并旧文件
        if filepath.exists():
            old_df = pd.read_parquet(filepath)
            old_df = old_df.rename(columns={'日期': 'date', '滚动市盈率': 'pe'})
            old_df['index_code'] = index_code
            if 'updated_at' not in old_df.columns:
                old_df['updated_at'] = 0.0  # 旧格式视为极老数据
            df = pd.concat([old_df, df])

        # concat 之后再次归一化时间，防止旧文件字符串类型退化
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('updated_at').drop_duplicates(subset=['index_code', 'date'], keep='last')
        df.to_parquet(filepath, index=False)
        
        _last_ak_fail_time = 0.0 
        return df, "AkShare实时/缓存"
        
    except Exception as e:
        # 降级分支 2：发生任何异常，进入冷却并读本地
        _last_ak_fail_time = time.time()
        if filepath.exists():
            df = pd.read_parquet(filepath)
            df = df.rename(columns={'日期': 'date', '滚动市盈率': 'pe'})
            df['date'] = pd.to_datetime(df['date'])
            return df, f"本地Parquet缓存 (因: {str(e)})"
        raise ValueError(f"无法获取估值数据，且本地无缓存。原因: {str(e)}")


# ==========================================
# 3. B2 条件判定与 UI 展示
# ==========================================
def evaluate_b2_condition(fund_code: str, offline: bool = False):
    # 1. 读取映射表
    try:
        with open(ROOT / 'config/index_mapping.json', 'r', encoding='utf-8') as f:
            mapping = json.load(f)
    except FileNotFoundError:
        return {"status": "无法判定", "reason": "缺少映射表"}

    if fund_code not in mapping:
        return {"status": "无法判定", "reason": "映射表未收录该基金"}
    
    target = mapping[fund_code]
    symbol = target['name'].replace(" ", "").strip()
    index_code = target['code']

    # 2. 白名单校验
    LG_SUPPORTED = {"上证50", "沪深300", "上证380", "创业板50", "中证500", "上证180", "深证红利", "深证100", "中证1000", "上证红利", "中证100", "中证800"}
    if symbol not in LG_SUPPORTED:
        return {"status": "无法判定", "reason": f"该基金跟踪的指数（{symbol}）不在 AkShare 乐咕覆盖的 12 个宽基范围内"}

    # 3. 获取数据
    try:
        df, source = get_valuation_data(symbol, index_code, offline=offline)
    except Exception as e:
        return {"status": "无法判定", "reason": str(e)}

    # 4. 严格对齐当前PE与日期（过滤空值后取最后一条）
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df['pe'] = pd.to_numeric(df['pe'], errors='coerce')
    df_recent = df.dropna(subset=['date']).sort_values('date').tail(1250).copy()
    pe_valid_df = df_recent.dropna(subset=['pe'])
    
    if len(pe_valid_df) < 100:
        return {"status": "无法判定", "reason": "PE历史样本不足100个交易日", "evidence": f"来源: {source}"}
        
    current_pe = pe_valid_df['pe'].iloc[-1]
    data_date = pe_valid_df['date'].iloc[-1]

    # NaT 防御
    if pd.isna(data_date):
        return {"status": "无法判定", "reason": "数据日期全部无效", "evidence": f"来源: {source}"}

    # UI 日期展示判定（长假防误报）
    days_diff = (datetime.now().date() - data_date.date()).days
    date_str = data_date.strftime("%Y-%m-%d")
    
    date_label = f"数据日期：{date_str}"
    if days_diff >= 9:
        date_label = f"⚠️ 数据截止日期：{date_str}，已超过 {days_diff} 天未更新，建议检查网络。"

    # 5. 计算历史分位
    percentile = (pe_valid_df['pe'] < current_pe).mean() * 100 

    if percentile <= 30:
        status = "满足"
        reason = f"当前PE分位 {percentile:.1f}% ≤ 30%"
    elif 30 < percentile <= 50:
        status = "不满足"
        reason = f"估值中性，当前PE分位 {percentile:.1f}%"
    else:
        status = "不满足"
        reason = f"估值偏高，当前PE分位 {percentile:.1f}%"
        
    evidence = f"{date_label} | 来源: {source} | 当前PE分位: {percentile:.1f}% | 当前PE: {current_pe:.2f} (日期: {date_str})"

    return {
        "status": status,
        "reason": reason,
        "evidence": evidence,
        "percentile": float(percentile / 100),
        "current_pe": float(current_pe),
        "data_date": date_str,
        "data_source": source,
        "observations": len(pe_valid_df),
    }
