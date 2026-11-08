"""Historical sandbox surface. All calculations belong to sandbox_engine."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.sandbox_engine import (load_cumulative_nav, load_valuation_history,
                                 run_sandbox, sandbox_conclusion)
from core.sandbox_advisor import build_sandbox_context
from ui.sandbox_advisor import render_sandbox_review
from ui.theme import MINT, BLUE, CORAL, apply_plotly_theme

LABELS = {'smart': '智能定投', 'regular': '普通定投', 'lump_sum': '一次性买入'}


def _pct(value):
    return f'{value:.2%}' if value is not None else '无法计算 XIRR'


def asset_figure(result, theme_mode=False):
    figure = go.Figure()
    colors = (MINT, BLUE, CORAL)
    for (key, dash), color in zip([('smart', 'solid'), ('regular', 'dash'),
                                   ('lump_sum', 'dot')], colors):
        figure.add_trace(go.Scatter(x=result.daily.index, y=result.daily[key],
            name=LABELS[key], mode='lines', line=dict(color=color, width=2.5, dash=dash),
            hovertemplate='%{x|%Y-%m-%d}<br>%{y:,.2f} 元<extra>%{fullData.name}</extra>'))
    figure.update_layout(height=360,
        hovermode='x unified', legend=dict(orientation='h', y=1.12, x=0),
        xaxis=dict(title=None, showgrid=False, tickformat='%Y-%m'),
        yaxis=dict(title='累计资产（元，含资金池）', tickformat=',.0f'))
    return apply_plotly_theme(figure, theme_mode)


def render_sandbox_result(payload, ai_settings, theme_mode=False):
    result = payload['result']
    snapshot = result.snapshot
    smart, regular = result.metrics['smart'], result.metrics['regular']
    annual = (result.end_date - result.start_date).days >= 365
    st.caption(f"基金 {payload['code']} · 回测 {result.start_date:%Y-%m-%d} — {result.end_date:%Y-%m-%d} · "
               f"实际净值 {result.daily.index[0]:%Y-%m-%d} — {result.daily.index[-1]:%Y-%m-%d} · "
               f"每月 {result.monthly_amount:,.0f} 元 × {smart['contributions']} 期")
    st.subheader('回到当时，先看买入依据')
    columns = st.columns(3)
    columns[0].metric('当时的 PE 分位', _pct(snapshot['pe_percentile']) if snapshot['pe_percentile'] is not None else '无数据 · 启用均线')
    columns[1].metric('200日均线偏离度', _pct(snapshot['ma_deviation']) if snapshot['ma_deviation'] is not None else '历史不足 · 按1倍投入')
    columns[2].metric('当时的计划投入倍数', f"{snapshot['multiplier']:g} 倍")
    st.info(snapshot['recommendation'])
    nav_date = f"{snapshot['nav_date']:%Y-%m-%d}" if snapshot['nav_date'] is not None else '无'
    pe_date = f"{snapshot['pe_date']:%Y-%m-%d}" if snapshot['pe_date'] is not None else '无'
    st.caption(f"决策截止 {snapshot['cutoff_date']:%Y-%m-%d} · 净值日期 {nav_date} · PE日期 {pe_date} · "
               f"PE样本 {snapshot['pe_observations']} 条 · B2：{snapshot['b2_status']} · {snapshot['trend']}")

    st.subheader('同一份预算，三种投入方式')
    first = st.columns(3)
    metric_name = 'XIRR' if annual else '区间累计收益率'
    metric_key = 'xirr' if annual else 'cumulative_return'
    first[0].metric(f'智能定投 {metric_name}', _pct(smart[metric_key]))
    first[1].metric(f'普通定投 {metric_name}', _pct(regular[metric_key]))
    first[2].metric('累计投入本金（每种策略）', f"¥ {smart['principal']:,.2f}")
    second = st.columns(3)
    excess = smart['final_assets'] - regular['final_assets']
    second[0].metric('智能定投期末总资产（含资金池）', f"¥ {smart['final_assets']:,.2f}",
                     delta=f'{excess:+,.2f} 元，相比普通定投')
    second[1].metric('智能定投最大回撤', _pct(smart['max_drawdown']))
    second[2].metric('普通定投最大回撤', _pct(regular['max_drawdown']))
    if not annual:
        st.caption('回测区间不足365天，展示区间累计收益率，避免短期收益年化放大。')
    st.plotly_chart(asset_figure(result, theme_mode=theme_mode), width='stretch', key='sandbox_assets', theme=None,
                    config={'displaylogo': False, 'responsive': True})
    st.caption('智能与普通定投按月同步注资；一次性买入期初投入全部预算，资金到位时间不同。'
               '资产曲线含外部注资，不等于收益曲线；最大回撤按剔除注资后的单位净值计算。')
    st.info(sandbox_conclusion(result))
    render_sandbox_review(payload, ai_settings)

    rows = []
    for key, label in LABELS.items():
        m = result.metrics[key]
        rows.append({'策略': label, '本金（元）': m['principal'], '期末总资产（元）': m['final_assets'],
            '其中资金池（元）': m['final_cash'], '盈亏（元）': m['profit'],
            metric_name: _pct(m[metric_key]), '最大回撤': _pct(m['max_drawdown'])})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width='stretch',
        column_config={key: st.column_config.NumberColumn(format='%.2f') for key in
                       ('本金（元）', '期末总资产（元）', '其中资金池（元）', '盈亏（元）')})
    with st.expander('计算口径、数据来源与逐月成交核对'):
        st.write(payload['nav_source'])
        st.write(payload['pe_source'])
        st.write('PE分位只使用信号截止日前最多1250条有效PE，至少100条；最近PE超过62个自然日未更新则降级。'
                 'PE分位≤30%投1.5倍、30%至50%投1倍、>50%投0.5倍。无PE时采用均值回归：'
                 '低于200日均线投1.5倍，高于均线投0.5倍；均线不足200条时投1倍。')
        st.write('月首日历从完整历史生成，回溯日期若已错过当月首个交易日，首次定投顺延至下月。'
                 '注资在每月第一个净值交易日到账；先对存量现金按 1 + 0.015/252 计息，再注资、买入，'
                 '超出资金池余额的计划只买最大余额。剔除周末季末/年末会计净值，周末不计息。')
        st.write('买入信号仅使用执行日前已披露的净值及PE；当日公布的收盘数据不参与当日信号。'
                 'XIRR只计每次外部注资与结束日资产变现，采用ACT/365，池内买入不重复计作现金流。'
                 '回测结束日不是净值日时，以此前最后净值估值。')
        st.write('按要求使用累计净值建模；累计净值包含历史现金分红，但并非严格的红利再投资复权净值。'
                 '本模型未计申赎费、确认延迟及限购；本地缓存可能包含事后修订，日期截断不能还原未保存的历史披露版本。')
        audit = result.orders.rename(columns={'date': '买入日期', 'signal_cutoff': '信号截止',
            'signal_nav_date': '信号净值日期', 'signal_pe_date': '信号PE日期',
            'rule': '规则', 'multiplier': '计划倍数', 'pe_percentile': 'PE分位',
            'ma_deviation': '均线偏离度', 'smart_purchase': '智能买入', 'smart_cash': '智能资金池',
            'regular_purchase': '普通买入', 'regular_cash': '普通资金池'})
        st.dataframe(audit, hide_index=True, width='stretch')
        st.download_button('下载每日资产与资金池明细', result.daily.to_csv().encode('utf-8-sig'),
                           file_name=f"sandbox_{payload['code']}_daily.csv", mime='text/csv', key='btn_download_daily', on_click='ignore')
    from ui.trade_instructions import render_instruction_download
    render_instruction_download(payload, sandbox=True)


def render_sandbox(ai_settings, theme_mode=False):
    st.html('''<style>
    .st-key-sandbox_surface [data-testid="stCaptionContainer"],
    .st-key-sandbox_surface [data-testid="stCaptionContainer"] p {color:var(--terminal-muted);opacity:1;}
    .st-key-sandbox_ai_card {background:var(--terminal-surface);border-radius:12px;}
    .st-key-sandbox_ai_card .sandbox-coach-section {padding:12px 16px;margin:12px 0;background:var(--terminal-raised);border-radius:10px;color:var(--terminal-ink);}
    .st-key-sandbox_ai_card .sandbox-coach-section p {margin:0 0 8px;line-height:1.8;}
    @media(max-width:640px) {
      .st-key-sandbox_surface [data-testid="stHorizontalBlock"] {flex-direction:column;}
      .st-key-sandbox_surface [data-testid="stColumn"] {width:100%;flex:1 1 100%;}
    }
    </style>''')
    st.title('历史沙盘推演')
    st.warning('历史回测结果不代表未来表现。智能定投优势在于平抑波动，不代表绝对保本。')
    st.write('把信息停在当时，再用相同预算走完此后的行情。')
    with st.form('sandbox_inputs'):
        columns = st.columns(4)
        code = columns[0].text_input('基金代码', value='110020', max_chars=6, key='sandbox_code')
        start = columns[1].date_input('回溯日期', value=date(2021, 1, 1), key='sandbox_start', format='YYYY-MM-DD')
        end = columns[2].date_input('回测结束日期', value=date(2024, 1, 1), key='sandbox_end', format='YYYY-MM-DD')
        amount = columns[3].number_input('基础定投金额（元/月）', min_value=1., value=1000., step=100., key='sandbox_amount')
        offline = st.checkbox('仅使用本地缓存', value=True, key='sandbox_offline')
        submitted = st.form_submit_button('运行历史推演', key='btn_run', type='primary')
    if submitted:
        # A failed new run must never leave an old fund's results on screen.
        st.session_state.pop('sandbox_payload', None)
        try:
            if end > datetime.now(ZoneInfo('Asia/Shanghai')).date():
                raise ValueError('回测结束日期不能晚于今天')
            with st.spinner('正在截断历史数据、计算月度信号与资金池…'):
                nav, nav_source = load_cumulative_nav(code.strip(), offline=offline, refresh=not offline)
                pe, pe_source = load_valuation_history(code.strip())
                result = run_sandbox(nav, pe, start, end, amount)
            from core.analysis import load_fund
            name = load_fund(code.strip(), True).info.get('基金名称', '名称数据不可得')
            st.session_state.sandbox_payload = dict(result=result, code=code.strip(), name=name,
                nav_source=nav_source, pe_source=pe_source,
                ai_context=build_sandbox_context(result, code.strip()))
        except (ValueError, OSError, KeyError, TypeError, RuntimeError, TimeoutError) as exc:
            st.error(f'推演未完成：{exc}。请核对日期与基金代码；缺少累计净值缓存时可取消“仅使用本地缓存”重试。')
    payload = st.session_state.get('sandbox_payload')
    if payload:
        result = payload['result']
        if (result.end_date - result.daily.index[-1]).days > 10:
            st.warning('末次净值距回测结束日超过10天；末期资产按最后可用净值估算，请核对数据覆盖范围。')
        render_sandbox_result(payload, ai_settings, theme_mode=theme_mode)
    else:
        st.info('设定回溯日期与金额，点击“运行历史推演”查看当时决策和三种投入方式。')
