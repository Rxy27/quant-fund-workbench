"""Streamlit UI and session state only. Run: streamlit run app.py."""
from copy import deepcopy
import hashlib
import sys
import logging
import time

# Retain existing ledger shortcuts while Streamlit owns the new entry point.
if __name__ == '__main__' and any(arg in sys.argv for arg in ('--open', '--port', '--data', '--no-refresh')):
    from ledger_app import main
    main()
    raise SystemExit(0)

import streamlit as st
from streamlit.logger import get_logger

from core.analysis import analyze, search_funds, load_fund, load_benchmark
from core.state import (CONFIG_DIR, load_state, save_portfolio, save_risk,
                        reset_state, read_json, portfolio_cash)
from ui.presentation import render_results, export_html
from ui.theme import theme_css
from ui.ai_advisor import render_settings
from core.network import analysis_progress, network_budget
from core.portfolio_sync import sync_portfolio, display_amount, today
from ui.realtime_quotes import render_market_watch


def retry_online():
    from core.valuation import fetch_akshare_pe_threaded
    fetch_akshare_pe_threaded.clear()
    from core.fees import _fetch_fee_table, _fetch_fund_catalog, _get_fund_info_and_fees
    from core.engine import load
    _fetch_fee_table.clear()
    _get_fund_info_and_fees.clear()
    _fetch_fund_catalog.clear()
    from core.alternatives import _get_all_funds_list, _get_net_values, clear_s5_cache
    clear_s5_cache()
    _get_all_funds_list.clear()
    _get_net_values.clear()
    load('data_loader').fetch_api_payload.clear()
    st.session_state['offline_mode'] = False
    load_fund.cache_clear()
    load_benchmark.cache_clear()
    st.session_state.pop('analysis_fingerprint', None)
    st.session_state.pop('report', None)
    if not st.session_state.get('selected_code'):
        st.session_state['retry_search'] = True


def short_reason(exc):
    return f'{type(exc).__name__}：' + ' '.join(str(exc).split())[:100]


def draft_rows():
    """Capture widgets before changing the number of editor rows."""
    state = st.session_state
    rows = []
    for i, holding in enumerate(state.editor_rows):
        prefix = f'holding_{state.editor_revision}_{i}'
        rows.append({**holding, 'code': state.get(prefix + '_code', holding['code']),
                     'amount': entered_amount(holding, state.get(prefix + '_amount', display_amount(holding))),
                     'shares': state.get(prefix + '_shares', holding.get('shares')),
                     'cost_nav': state.get(prefix + '_cost', holding.get('cost_nav'))})
    return rows


def entered_amount(holding, value):
    """An unchanged estimate must never become a saved official amount."""
    return holding['amount'] if value == display_amount(holding) else value


def sync_portfolio_callback():
    """Callbacks run before widgets: persist first, then update their state."""
    state = st.session_state
    state['sync_messages'] = []
    try:
        portfolio, _ = load_state()
        drafts = draft_rows()
        fields = ('code', 'amount', 'cost_nav', 'shares')
        if (state.get('account_total') != portfolio['account_total'] or
                [{k: h.get(k) for k in fields} for h in drafts] !=
                [{k: h.get(k) for k in fields} for h in portfolio['holdings']]):
            state['sync_messages'] = [('warning', '有未保存的持仓修改，请先保存持仓再同步市值。')]
            return
        updated, warnings_list, count = sync_portfolio(portfolio)
        if updated != portfolio:
            updated = save_portfolio(updated)
            state.editor_rows = deepcopy(updated['holdings'])
            state['account_total'] = float(updated['account_total'])
            for i, holding in enumerate(updated['holdings']):
                state[f'holding_{state.editor_revision}_{i}_amount'] = float(display_amount(holding))
            for key in ('analysis_fingerprint', 'report', 'ai_summary_cache', 'ai_summary_task'):
                state.pop(key, None)
        if any(h.get('is_calibrated', True) is False for h in updated['holdings']):
            state['sync_messages'].append(('info',
                'ℹ️ 部分基金官方净值未更新，当前展示为【盘中临时估算】（基于 ETF 涨跌幅或东财估值，非最终净值）。'
                '请在晚间净值公布后再次点击【同步市值】以完成校准。'))
        elif count and not warnings_list:
            state['sync_messages'].append(('success', '✅ 全部持仓已按官方净值校准'))
        elif not warnings_list and updated['holdings']:
            dates = '、'.join(sorted({h['base_date'] for h in updated['holdings']}))
            prefix = '今天为周末。' if today().weekday() >= 5 else ''
            state['sync_messages'].append(('info', prefix +
                f'已核对官方净值，与已保存基准一致（净值日期：{dates}），本次无需更新市值。'
                '已填份额的持仓按份额×官方单位净值校验；未填份额的持仓沿用原金额基准。'))
        state['sync_messages'].extend(('warning', warning) for warning in warnings_list)
    except Exception as exc:
        state['sync_messages'] = [('warning', f'市值同步未保存，原持仓已保留：{short_reason(exc)}')]


def main():
    get_logger('core.market_data').setLevel(logging.INFO)
    st.set_page_config(page_title='有数 · 基金分析工作台', layout='wide')
    if 'dark_mode' not in st.session_state:
        try:
            st.session_state['dark_mode'] = (st.context.theme.type == 'dark')
        except Exception:  # Includes AttributeError on older Streamlit versions.
            st.session_state['dark_mode'] = False
    # Handle the button before injecting CSS: the new pass paints only the target theme.
    if st.sidebar.button('切换深浅模式', key='btn_toggle_theme', width='stretch'):
        st.session_state['dark_mode'] = not st.session_state['dark_mode']
        st.rerun()
    is_dark_mode = st.session_state['dark_mode']
    st.markdown(theme_css(is_dark_mode), unsafe_allow_html=True)
    st.sidebar.caption('当前外观：' + ('深色' if is_dark_mode else '浅色'))
    mode = st.sidebar.radio('工作模式', ['基金分析', '历史沙盘推演'],
        index=1 if st.query_params.get('mode') == 'sandbox' else 0, key='workspace_mode')
    if mode == '历史沙盘推演':
        from ui.sandbox import render_sandbox
        with st.sidebar:
            sandbox_ai_settings = render_settings(show_investment_plan=False)
        with st.container(key='sandbox_surface'):
            render_sandbox(sandbox_ai_settings, theme_mode=is_dark_mode)
        try:
            holdings = read_json(CONFIG_DIR / 'user_portfolio.json')['holdings']
        except (OSError, ValueError, KeyError, TypeError):
            holdings = []
        render_market_watch(holdings)
        return
    try:
        portfolio, risk = load_state()
        defaults = read_json(CONFIG_DIR / 'ui_defaults.json')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        st.error(f'配置读取失败：{exc}。请检查 config 目录中的 JSON，原文件已保留。')
        st.stop()
    state = st.session_state
    if 'editor_rows' not in state:
        state.editor_rows = deepcopy(portfolio['holdings'])
    if 'editor_revision' not in state:
        state.editor_revision = 0
    revision = state.editor_revision
    with st.sidebar:
        st.title('账户与配置')
        st.subheader('持仓管理')
        state.setdefault('account_total', float(portfolio['account_total']))
        total = st.number_input('账户总额（含现金，元）', min_value=0.0,
            format='%.2f', key='account_total')
        rows = []
        for i, holding in enumerate(state.editor_rows):
            prefix = f'holding_{revision}_{i}'
            fund_code = holding['code']
            # Draft rows may have empty or duplicate codes before validation.
            card_key = f'portfolio_item_{fund_code}'
            if not fund_code or sum(h['code'] == fund_code for h in state.editor_rows) > 1:
                card_key += f'_draft_{revision}_{i}'
            current_code = state.get(prefix + '_code', fund_code).strip()
            with st.expander(f'基金 {current_code}' if current_code else '新增持仓（填写基金代码）',
                             expanded=not bool(current_code), key=card_key):
                code = st.text_input('基金代码', value=holding['code'], key=prefix + '_code')
                if holding.get('base_nav') and holding.get('base_date'):
                    st.caption(f'官方净值日期：{holding["base_date"]}；'
                               f'基准累计净值：{holding["base_nav"]:.6f}')
                if holding.get('base_unit_nav') is not None and holding.get('shares') is not None:
                    st.caption(f'官方单位净值：{holding["base_unit_nav"]:.6f}；市值按确认份额计算。')
                if holding.get('is_calibrated', True) is False:
                    st.markdown(f'**{holding["code"]} · 🟡 预估待校准**')
                    st.caption(f'官方基准市值：{holding["amount"]:,.2f} 元；下方为盘中临时估算。')
                state.setdefault(prefix + '_amount', float(display_amount(holding)))
                amount = st.number_input('持仓金额（元）', min_value=0.0,
                    step=10000.0, format='%.2f', key=prefix + '_amount')
                shares = st.number_input('持有份额（选填）', min_value=0.0,
                    value=holding.get('shares'), step=0.01, format='%.4f', key=prefix + '_shares',
                    help='填写并保存一次，日常同步自动按份额×最新单位净值更新金额；申购、赎回或分红再投后再修改。留空沿用金额基准。')
                cost = st.number_input('买入成本净值（可留空）', min_value=0.000001,
                    value=holding.get('cost_nav'), format='%.6f', key=prefix + '_cost')
                rows.append({**holding, 'code': code.strip(),
                             'amount': entered_amount(holding, amount), 'shares': shares, 'cost_nav': cost})
                if st.button('删除此持仓', key=prefix + '_delete', width='stretch'):
                    state.editor_rows = [r for j, r in enumerate(draft_rows()) if j != i]
                    state.editor_revision += 1
                    st.rerun()
        cash = portfolio_cash({'account_total': total, 'holdings': rows})
        st.caption(f'剩余现金：{cash:,.2f} 元。保存时按账户总额换算权重；现金沿用引擎零息口径。')
        if st.button('添加持仓', key='btn_add_holding', width='stretch'):
            state.editor_rows = rows + [{'code': '', 'amount': 0.0, 'cost_nav': None}]
            state.editor_revision += 1
            st.rerun()
        save_feedback = None
        col1, col2 = st.columns(2)
        with col1:
            if st.button('保存持仓', key='btn_save', type='primary', width='stretch'):
                updated = {'account_total': total, 'holdings': rows}
                try:
                    portfolio = save_portfolio(updated, rebase=True)
                    state.editor_rows = deepcopy(portfolio['holdings'])
                    save_feedback = ('success', '持仓已保存，分析已按新持仓更新')
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    save_feedback = ('error', f'未保存：{exc}')
        with col2:
            st.button('同步市值', key='btn_sync', width='stretch', on_click=sync_portfolio_callback,
                      help='已填份额按份额×单位净值校准；留空按原金额基准推算。盘中估算不覆盖官方基准。')
        if save_feedback:
            level, message = save_feedback
            {'success': st.success, 'error': st.error}[level](message)
        st.markdown('<p style="font-size:12px;color:#8b95a5;font-style:italic;">'
                    '注：已填份额按份额×单位净值同步；留空按累计净值推算。申购、赎回或分红再投后请更新份额。'
                    '</p>', unsafe_allow_html=True)
        for level, message in state.get('sync_messages', []):
            {'success': st.success, 'info': st.info, 'warning': st.warning}[level](message)
        state['sync_messages'] = []
        st.subheader('风险偏好调整')
        updated_risk = deepcopy(risk)
        for field, label, spec_name in [
            ('portfolio_max_drawdown_tolerance', '组合最大回撤容忍（%）', 'drawdown_slider'),
            ('portfolio_target_volatility', '组合目标波动率（%）', 'volatility_slider')]:
            spec = defaults[spec_name]
            current = float(risk[field] * 100)
            if not spec['min'] <= current <= spec['max']:
                st.error(f'{label}超出滑块可编辑范围，请检查配置；未自动修改。')
                st.stop()
            value = st.slider(label, min_value=float(spec['min']), max_value=float(spec['max']),
                value=current, step=float(spec['step']), key=f'{field}_{revision}')
            updated_risk[field] = value / 100
        risk_saved = True
        if updated_risk != risk:
            try:
                save_risk(updated_risk)
                risk = updated_risk
            except (OSError, ValueError) as exc:
                risk_saved = False
                st.error(f'风险配置保存失败：{exc}')
        if risk_saved:
            st.success('当前配置已生效')
        st.caption('持仓点击保存后生效；风险滑块修改后立即保存。')
        offline = st.checkbox('仅使用本地缓存', value=True, key='offline_mode')
        if st.button('重新读取数据', key='btn_reload', width='stretch'):
            load_fund.cache_clear()
            load_benchmark.cache_clear()
            state.pop('analysis_fingerprint', None)
            state.pop('ai_summary_cache', None)
            state.pop('ai_summary_task', None)
        st.divider()
        ai_settings = render_settings()

    st.title('基金分析工作台')
    st.write('从持仓出发，核对条件，查看风险预算。')
    st.caption('历史统计与条件情景推演，不保证未来损失边界。')
    render_market_watch(portfolio['holdings'])
    with st.form('search_form'):
        search_column, action_column = st.columns([4, 1.4], vertical_alignment='bottom')
        with search_column:
            query = st.text_input('输入基金代码或名称（如 110020）', value='110020')
        with action_column:
            submitted = st.form_submit_button('开始深度分析', key='btn_analyze', type='primary', width='stretch')
    analysis_deadline = None
    if submitted or state.pop('retry_search', False):
        analysis_deadline = time.monotonic() + 15.0
        state.data_retry_required = False
        state.pop('report', None)
        state.pop('selected_code', None)
        state.pop('analysis_fingerprint', None)
        try:
            with st.spinner('正在拉取最新数据与计算量化条件...'), network_budget():
                st.caption('正在匹配基金...')
                state.search_choices = search_funds(query, offline)
                state.search_revision = state.get('search_revision', 0) + 1
        except Exception as exc:
            state.search_choices = []
            st.error(f'搜索未完成：{short_reason(exc)}')
            state.data_retry_required = not query.strip().isdecimal()
    if state.get('search_choices'):
        choices = state.search_choices
        if len(choices) == 1:
            state.selected_code = choices[0][0]
        else:
            selected = st.selectbox('请选择匹配基金', choices, index=None,
                format_func=lambda x: f'{x[0]} · {x[1]}', key=f"match_{state.search_revision}")
            if selected:
                state.selected_code = selected[0]
            else:
                state.pop('selected_code', None)
                state.pop('report', None)
    if state.get('selected_code'):
        fingerprint = repr((state.selected_code, portfolio, risk, offline))
        if state.get('analysis_fingerprint') != fingerprint:
            state.pop('report', None)
            try:
                with st.spinner('正在拉取最新数据与计算量化条件...'):
                    stage = st.empty()
                    seconds = max(0., analysis_deadline - time.monotonic()) if analysis_deadline else 15.0
                    with analysis_progress(stage.info), network_budget(seconds):
                        state.report = analyze(state.selected_code, portfolio, risk, offline)
                    stage.empty()
                state.analysis_fingerprint = fingerprint
                state.data_retry_required = state.report.get('retry_required', False)
            except Exception as exc:
                logging.getLogger(__name__).exception('基金分析失败')
                state.data_retry_required = True
                st.error(f'分析未完成：{short_reason(exc)}。请重新联网获取。')
        if state.get('report'):
            if state.report.get('network_timeout'):
                if state.report['fund'].available:
                    st.warning('网络超时，已展示缓存数据。请核对各项数据截止日期；缺失项目标记为无法判定。')
                else:
                    st.error('网络超时，且本地没有可用净值缓存。请稍后重新联网获取。')
            state.pop('ai_summary', None)
            try:
                state.ai_summary = render_results(state.report, ai_settings)
            except Exception as e:
                logging.getLogger(__name__).exception('基金结果渲染失败')
                st.error(f'渲染故障，请重新尝试（{short_reason(e)}）')
    else:
        st.info('输入基金代码或名称开始分析。保存持仓或调整预算后，当前分析将自动更新。')
    if state.get('data_retry_required'):
        st.warning('离线模式需联网：离线快照缺失，请点击重新联网获取' if offline else
                   '部分数据获取失败或缓存未更新，可能网络超时。请点击重新联网获取。')
        st.button('重新联网获取', type='primary', on_click=retry_online, key='retry_online')
    with st.sidebar:
        st.divider()
        report = state.get('report')
        archive_amount = state.get('trial_amount', 0.0)
        if report:
            st.caption(f'归档包含假设金额：{archive_amount:,.2f} 元')
        archive_key = hashlib.sha256(repr((state.get('analysis_fingerprint'),
                                           archive_amount, state.get('ai_summary'))).encode()).hexdigest()
        try:
            st.download_button('导出当前结果为 HTML',
                data=export_html(report, archive_amount, state.get('ai_summary')) if report else '',
                file_name=f"fund_{report['code'] if report else 'analysis'}.html",
                mime='text/html', disabled=report is None, width='stretch',
                key='btn_download_' + archive_key, on_click='ignore')
        except Exception as e:
            logging.getLogger(__name__).exception('基金结果导出失败')
            st.error(f'渲染故障，请重新尝试（导出：{short_reason(e)}）')
        if st.button('重置所有配置', key='btn_reset', width='stretch', help='恢复原审计配置、初始持仓和账户总额'):
            try:
                reset_state()
                state.clear()
                st.rerun()
            except OSError as exc:
                st.error(f'重置失败：{exc}')


if __name__ == '__main__':
    main()
