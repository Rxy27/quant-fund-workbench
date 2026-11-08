"""Shared presentation for the Streamlit screen and HTML archive."""
from html import escape
import json
import math

import pandas as pd
import streamlit as st

NO_SOLUTION = '无可行解！该基金加入后，组合无法满足回撤/波动预算，不建议买入。'
# Re-export the shared theme helpers for presentation callers.
from ui.theme import CSS, DARK_CSS, MINT, BLUE, CORAL, apply_plotly_theme
import plotly.graph_objects as go


def render_nav_chart(series):
    figure = go.Figure(go.Scatter(
        x=series.index, y=series.values, name=series.name, mode='lines',
        line=dict(color=MINT, width=2.5),
        hovertemplate='%{x|%Y-%m-%d}<br>%{y:,.4f}<extra>%{fullData.name}</extra>'))
    figure.update_layout(height=320, hovermode='x unified', showlegend=False,
                         xaxis=dict(title=None, showgrid=False),
                         yaxis=dict(title=series.name, rangemode='normal'))
    st.plotly_chart(apply_plotly_theme(figure, st.session_state.get('dark_mode', False)),
                    width='stretch', theme=None,
                    key=f'nav_chart_{series.name}', config={'displaylogo': False})


def pct(value):
    return f'{value:.2%}' if value is not None and math.isfinite(value) else '无法判定'


def evidence_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def scan_frame(sizing):
    return pd.DataFrame([{'权重': pct(r['weight']), '回撤': pct(r['portfolio_max_drawdown']),
        '波动': pct(r['portfolio_volatility']), '预算': '满足' if r['within_budget'] else '不满足'}
        for r in sizing['scan_table']])


def render_conditions(report):
    for key, title in [('buy', '买入条件 B1–B6'), ('sell', '卖出条件 S1–S5')]:
        st.subheader(title)
        for row in report['conditions'][key]:
            status = row['status']
            display = st.success if status in ('满足', '触发') else st.warning if status == '无法判定' else st.info
            details = row['reason']
            if row['id'] in ('B2', 'B6', 'S5') and row['evidence'].get('summary'):
                details += '\n\n' + row['evidence']['summary']
            display(f"**{row['id']} · {row['label']} — {status}**\n\n{details}")
            st.caption('当前阈值：' + report['thresholds'][row['id']])
            if row['id'] == 'B6':
                st.caption('动态费率阈值：' + pct(row['evidence'].get('peer_median')))
            if row['id'] == 'S5':
                st.caption('费用比较基准：' + pct(row['evidence'].get('fund_fee')) +
                    '；年化收益差异标准差 < 2%；共同日收益至少50条；随机样本仅提供替代线索。')
            with st.expander(f"{row['id']} 原始依据与来源"):
                st.write(row['data_source'])
                st.html('<pre class="evidence">' + escape(evidence_text(row['evidence'])) + '</pre>')


def render_results(report, ai_settings=None):
    from core.ai_advisor import load_settings
    from ui.ai_advisor import clear_ai_results, is_ai_enabled, render_summary
    settings = ai_settings if ai_settings is not None else load_settings()
    enabled = is_ai_enabled(settings)
    if not enabled:
        clear_ai_results()
    fund, metrics = report['fund'], report['metrics']
    st.subheader(f"{fund.info.get('基金名称', '名称数据不可得')} · {report['code']}")
    st.caption(f"{report['classification'].category} · 来源 AkShare · 净值截止 {metrics.get('end', '数据不可得')} · " +
        ('本地缓存' if report['offline'] else '优先使用24小时缓存；按需联网，更新失败会提示'))
    st.caption(report.get('reference_note', '基于 110020 作为参照系'))
    st.caption(report.get('calendar_note', ''))
    summary_slot = st.empty() if enabled else None
    tabs = st.tabs(['基金体检', '组合角色与相关性', '决策依据清单', '仓位推导与压力测试'],
                   key='analysis_tab', on_change='rerun')
    with tabs[0]:
        if not fund.available:
            st.warning(fund.error or '有效净值不足，无法评估')
        elif metrics.get('short_history'):
            st.warning('历史样本不足三年；部分条件或历史情景无法判定，请逐项核对。')
        if metrics.get('annualization_note'):
            st.warning(metrics['annualization_note'])
        cols = st.columns(2)
        cols[0].metric('历史年化', metrics.get('annualization_note') or pct(metrics.get('annual_return')))
        cols[1].metric('历史年化波动', metrics.get('annualization_note') or pct(metrics.get('volatility')))
        cols = st.columns(2)
        cols[0].metric('历史最大回撤', pct(metrics.get('max_drawdown')))
        cols[1].metric('最长水下期', str(metrics['longest_underwater']) if metrics else '无法判定')
        cols[1].caption('单位：有效净值观测日')
        if len(fund.nav):
            st.caption(f"区间 {metrics['start']} — {metrics['end']}，{metrics['observations']} 条观测。历史统计不推断未来表现。")
            render_nav_chart(fund.nav.rename('历史净值'))
        income = fund.tables.get('万份收益', pd.DataFrame())
        if {'净值日期', '每万份收益'}.issubset(income.columns):
            st.caption('万份收益曲线（元）；上方净值按披露收益逐日再投资计算。')
            curve = income.assign(净值日期=pd.to_datetime(income['净值日期'], errors='coerce')).dropna(subset=['净值日期']).set_index('净值日期').sort_index()
            render_nav_chart(curve['每万份收益'])
        st.subheader('基本信息')
        for label in ['基金全称', '基金类型', '成立时间', '基金管理人', '基金经理', '跟踪标的', '管理费率', '托管费率']:
            st.markdown(f"**{label}**　{escape(str(fund.info.get(label, '数据不可得')))}")
        with st.expander('数据边界与追溯'):
            for message in fund.warnings:
                st.warning(message)
            st.html('<pre class="evidence">' + escape(evidence_text(fund.meta)) + '</pre>')
    with tabs[1]:
        portfolio = report['portfolio']
        st.subheader('当前角色：' + portfolio['role'])
        if portfolio['error']:
            st.warning(portfolio['error'])
        for basis in portfolio['basis']:
            st.write(basis)
        cells = [{'持仓': row['code'], '基金': col, '相关性': cell['value'], '共同日收益': cell['n']}
            for row in portfolio['matrix'] for col, cell in zip(portfolio['columns'], row['cells'])]
        if cells:
            figure = go.Figure(go.Heatmap(
                x=portfolio['columns'], y=[row['code'] for row in portfolio['matrix']],
                z=[[cell['value'] for cell in row['cells']] for row in portfolio['matrix']],
                customdata=[[cell['n'] for cell in row['cells']] for row in portfolio['matrix']],
                zmin=-1, zmax=1, zmid=0,
                colorscale=[[0, CORAL], [.5, '#9aa7b5'], [1, BLUE]],
                colorbar=dict(title='相关性'), hoverongaps=False,
                hovertemplate='持仓 %{y}<br>基金 %{x}<br>相关性 %{z:.3f}<br>共同日收益 %{customdata}<extra></extra>'))
            figure.update_layout(height=max(260, len(portfolio['columns']) * 32),
                                 xaxis=dict(tickangle=-45))
            st.plotly_chart(apply_plotly_theme(figure, st.session_state.get('dark_mode', False)),
                            width='stretch', theme=None, key='portfolio_correlation',
                            config={'displaylogo': False})
            st.caption('空白格表示无法判定；现金零收益的相关系数无定义。')
    with tabs[2]:
        render_conditions(report)
        from ui.trade_instructions import render_instruction_download
        render_instruction_download(report)
    with tabs[3]:
        # Render the grid only when visible: a hidden tab has no usable width.
        if tabs[3].open:
            render_sizing(report)
    # Fill the earlier slot after the trial widget so this render/export uses its latest value.
    if summary_slot is not None:
        with summary_slot.container():
            return render_summary(report, settings)
    return None


def render_sizing(report):
    from core.analysis import simulate_purchase
    sizing = report['sizing']
    st.subheader('历史组合扫描')
    if sizing['binding_constraint'] == '数据不足':
        st.error('无法判定：数据不足，尚不能验证预算可行性。' + sizing['note'])
        st.write('lower / upper：无法推导；缺失数据不能解释为风险超限或可行的 0% 仓位。')
    elif not sizing['feasible']:
        st.error(NO_SOLUTION)
        st.write('lower / upper：无法给出可行区间（引擎边界记录不作为 0% 可行结论）。')
    else:
        st.write(f"**lower {pct(sizing['lower'])}　upper {pct(sizing['upper'])}**")
    st.caption('约束：' + sizing['binding_constraint'] + '。' + sizing['note'])
    if sizing['scan_table']:
        st.dataframe(scan_frame(sizing).head(10), hide_index=True, width='stretch',
            column_config={name: st.column_config.TextColumn(width=64) for name in ['权重', '回撤', '波动', '预算']})
        with st.expander('完整扫描表'):
            st.html(scan_frame(sizing).to_html(index=False, escape=True, classes='scan-table'))
    st.subheader('假设投入后的组合冲击')
    st.caption('按额外资金买入：总额 = 当前账户总额 + 输入金额，原持仓金额保持。此试算不写入持仓；风险数字为历史重放。')
    amount = st.number_input('假设买入金额（元）', min_value=0.0,
        value=st.session_state.get('trial_amount', 0.0), step=10000.0, key='hypothetical_amount')
    st.session_state.trial_amount = amount
    simulation = simulate_purchase(report, amount)
    st.write('买入后该基金总权重：' + pct(simulation['candidate_weight']))
    row = simulation['row']
    if row is None:
        st.warning('无法判定：' + simulation['note'])
    else:
        cols = st.columns(2)
        cols[0].metric('组合历史回撤重放', pct(row['portfolio_max_drawdown']))
        cols[1].metric('组合历史波动重放', pct(row['portfolio_volatility']))
        (st.success if row['within_budget'] else st.error)(
            '本次试算满足风险预算' if row['within_budget'] else '本次试算未满足风险预算')
        if simulation['candidate_weight'] > report['config']['single_fund_max_weight']:
            st.warning('试算总持仓权重超过配置的单标的上限 ' + pct(report['config']['single_fund_max_weight']))
        st.caption(f"共同区间 {simulation['start']} — {simulation['end']}。历史结果不保证未来损失边界。")
    st.subheader('极端历史情景 · 候选基金')
    for scene in report['stress']['historical']:
        st.markdown(f"**{scene['name']}**　{scene['start']} — {scene['end']}")
        if scene['error']:
            st.warning(scene['error'])
        else:
            st.write(f"历史回撤 {pct(scene['max_drawdown'])} · 年化波动 {pct(scene['volatility'])}")


def export_html(report, amount=0.0, summary=None):
    """Archive the current results, configuration and trial without external assets."""
    from core.analysis import simulate_purchase
    from ui.ai_advisor import summary_html
    esc = lambda value: escape(str(value))
    blocks = [f"<h1>{esc(report['fund'].info.get('基金名称', '基金分析'))} · {report['code']}</h1>",
        '<p>历史描述与条件核对；历史结果不保证未来表现。</p>',
        '<p>' + esc(report.get('reference_note', '基于 110020 作为参照系')) + '</p>',
        '<p>' + esc(report.get('calendar_note', '')) + '</p>',
        summary_html(summary) if summary is not None else '', '<h2>基金体检</h2>',
        '<pre>' + esc(evidence_text(report['metrics'])) + '</pre>',
        '<h2>组合角色与相关性</h2>', '<pre>' + esc(evidence_text(report['portfolio'])) + '</pre>']
    for group in ('buy', 'sell'):
        for row in report['conditions'][group]:
            blocks += [f"<h3>{row['id']} · {esc(row['label'])} · {esc(row['status'])}</h3>",
                f"<p>{esc(row['reason'])}</p><p>当前阈值：{esc(report['thresholds'][row['id']])}</p>",
                '<pre>' + esc(evidence_text(row['evidence'])) + '</pre>', '<p>来源：' + esc(row['data_source']) + '</p>']
    blocks += ['<h2>仓位推导与压力测试</h2>']
    if report['sizing']['binding_constraint'] == '数据不足':
        blocks += ['<p class="error">无法判定：数据不足，尚不能验证预算可行性。</p>',
                   '<p>无法推导仓位区间，不将缺失数据解释为风险超限或可行的零仓位。</p>']
    elif not report['sizing']['feasible']:
        blocks += ['<p class="error">' + NO_SOLUTION + '</p>', '<p>无法给出可行区间；零边界不代表零风险或可行解。</p>']
    else:
        blocks += [f"<p>lower {pct(report['sizing']['lower'])} / upper {pct(report['sizing']['upper'])}</p>"]
    blocks += [scan_frame(report['sizing']).to_html(index=False, escape=True),
        '<pre>' + esc(evidence_text(report['sizing'])) + '</pre>',
        '<h2>假设买入试算（额外资金）</h2>', f'<p>金额：{amount:,.2f} 元</p>',
        '<pre>' + esc(evidence_text(simulate_purchase(report, amount))) + '</pre>',
        '<h2>历史情景</h2>', '<pre>' + esc(evidence_text(report['stress'])) + '</pre>',
        '<h2>配置与数据追溯</h2>', '<pre>' + esc(evidence_text({
            'user_portfolio': report['user_portfolio'], 'decision_config': report['config'],
            'fund_meta': report['fund'].meta, 'benchmark_meta': report['benchmark_meta']})) + '</pre>']
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>基金分析存档</title><style>body{font:16px/1.7 "Microsoft YaHei",sans-serif;max-width:1000px;margin:auto;padding:24px;color:#20314d}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}table{width:100%;table-layout:fixed;border-collapse:collapse}td,th{overflow-wrap:anywhere;padding:6px;border:1px solid #e3eaf3}.error{color:#a52626;background:#fff0f0;padding:16px}</style>' + ''.join(blocks) + '</html>'
