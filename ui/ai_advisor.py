"""AI settings and a shared, escaped summary card."""
from concurrent.futures import Future, TimeoutError as FutureTimeout
from html import escape
import hashlib
import json
import threading

import httpx
import streamlit as st

from core.ai_advisor import (ACTION_TITLE, DEFAULT_SETTINGS, DISCLAIMER, NETWORK_TIMEOUT_MESSAGE, TITLES, build_context,
    clean_base_url, current_date, generate_summary, get_effort_levels, load_settings,
    mark_sensitive_sentences, save_settings, test_connection)


def _clear_model_catalog():
    """网址或密钥改变后，旧目录及测试反馈立即失效。"""
    s = st.session_state
    s.available_models = []
    s.model_metadata = {}
    s.test_status = None
    s.ai_input_effort = ''
    s.pop('ai_input_select_model', None)


def _begin_connection_test():
    """先切换状态，让下一轮脚本渲染禁用的按钮及 Loading。"""
    st.session_state.is_testing = True
    st.session_state.test_status = None


def is_ai_enabled(settings):
    """The live switch takes precedence over the persisted configuration."""
    return st.session_state.get('ai_input_enabled', settings.get('enabled', False)) is True


def clear_ai_results():
    """Detach pending work without waiting; late results cannot repopulate state."""
    for key in ('ai_summary_task', 'ai_summary_cache', 'ai_summary',
                'sandbox_ai_task', 'sandbox_ai_cache'):
        st.session_state.pop(key, None)


def render_settings(*, show_investment_plan=True):
    if show_investment_plan:
        month = current_date().strftime('%Y-%m')
        if st.session_state.get('investment_plan_month') != month:
            st.session_state.investment_plan_month = month
            st.session_state.has_invested_this_month = False
        st.subheader('本月买入计划')
        st.checkbox('本月是否已执行买入计划', value=False, key='has_invested_this_month')
        st.caption('每月金额由你自行决定；条件较好可提前执行，否则默认等到月底。')
    try:
        settings = load_settings()
    except (OSError, ValueError, TypeError):
        settings = dict(DEFAULT_SETTINGS)
        st.warning('AI 配置无法读取；请检查并重新保存 AI 配置。')
    s = st.session_state
    # 同一会话中的 rerun 会保留这些状态；硬刷新后的新会话从已保存配置初始化。
    for field in ('enabled', 'base_url', 'api_key', 'model_id', 'effort'):
        s.setdefault('ai_input_' + field, settings.get(field, ''))
    for key, value in dict(is_testing=False, available_models=[], model_metadata={},
                           test_status=None).items():
        s.setdefault(key, value)

    # 在创建控件之前应用上一轮测试结果，避免修改已创建控件的状态。
    result = s.pop('_ai_connection_result', None)
    if result is not None:
        s.available_models = result.get('models', [])
        s.model_metadata = result.get('model_metadata', {})
        if result['success']:
            previous_model = s.ai_input_model_id.strip()
            if s.available_models:
                s.ai_input_model_id = (previous_model if previous_model in s.available_models
                                       else s.available_models[0])
                s.ai_input_select_model = s.ai_input_model_id
            else:
                # 未取得模型目录时保留用户填写的模型。
                s.ai_input_model_id = previous_model
                s.pop('ai_input_select_model', None)
            s.ai_input_base_url = clean_base_url(s.ai_input_base_url)
            s.test_status = dict(type='success', msg=result.get('msg') or f"连接成功，延迟 {result['latency']} ms")
        else:
            s.test_status = dict(type='error', msg=result['msg'])

    with st.expander('⚙️ AI 参谋设置', key='ai_settings_panel'):
        st.caption('兼容 OpenAI、DeepSeek、Kimi 等 Chat Completions 接口。')
        # 移除 form 的批量提交语义，保留控件顺序，使模型切换立即 rerun。
        st.toggle('是否启用 AI 总结', key='ai_input_enabled', disabled=s.is_testing)
        st.text_input('Base URL', key='ai_input_base_url', disabled=s.is_testing,
            help='填到 v1 结尾，如 https://api.foxapi.cn/v1', on_change=_clear_model_catalog)
        st.text_input('API Key', key='ai_input_api_key', type='password',
            on_change=_clear_model_catalog, disabled=s.is_testing)
        current_model = s.ai_input_model_id
        if s.available_models:
            # 下拉框与文本框使用独立 key；保留目录更新后仍有效的选择。
            selected = s.get('ai_input_select_model', current_model)
            s.ai_input_select_model = (selected if selected in s.available_models
                                       else s.available_models[0])
            st.selectbox('Model ID', s.available_models, key='ai_input_select_model',
                         disabled=s.is_testing)
            s.ai_input_model_id = s.ai_input_select_model
        else:
            # 显式重存持久值，避免切回文本框时因控件身份变化而清空。
            s.ai_input_model_id = current_model
            st.text_input('Model ID', key='ai_input_model_id', disabled=s.is_testing)
        levels = get_effort_levels(s.ai_input_model_id, s.model_metadata)
        if levels:
            # options 改变时也显式赋值，保留两个模型共同支持的原档位。
            s.ai_input_effort = s.ai_input_effort if s.ai_input_effort in levels else levels[0]
            st.selectbox('推理强度（Effort）', levels, key='ai_input_effort', disabled=s.is_testing)
        else:
            s.ai_input_effort = ''
        st.caption('启用开关即时生效；接口配置保存后生效。密钥保存在本机 JSON；调用时用于所填服务的鉴权，量化摘要会发送至该服务。')
        st.caption('测试连接仅拉取模型列表，不发送聊天请求。无需测试连接，可直接手动填写 Model ID 并保存配置。')
        st.button('测试连接', key='ai_test_button', on_click=_begin_connection_test,
                  disabled=s.is_testing, width='stretch')
        save = st.button('保存配置', key='btn_save_ai', type='primary', disabled=s.is_testing, width='stretch')
        if s.test_status:
            show_status = st.success if s.test_status['type'] == 'success' else st.error
            # 完整显示错误提示、状态码及服务端原因。
            show_status(s.test_status['msg'])
        if save:
            try:
                draft = dict(enabled=s.ai_input_enabled, base_url=clean_base_url(s.ai_input_base_url),
                    api_key=s.ai_input_api_key.strip(), model_id=s.ai_input_model_id.strip(),
                    temperature=settings['temperature'], effort=s.ai_input_effort)
                settings = save_settings(draft)
                s.pop('ai_summary_cache', None)
                st.success('密钥仅保存在本机，不会上传云端')
            except (OSError, ValueError, TypeError):
                st.error('AI 配置未保存。请检查 URL、Model ID 和本机文件写入权限。')
        if not s.ai_input_enabled:
            st.caption('AI 总结已关闭。')
        elif not settings['api_key']:
            st.caption('请填写并保存 API Key 以使用 AI 总结。')
        if s.is_testing:
            try:
                with st.spinner('正在测试连接…'):
                    result = test_connection(s.ai_input_base_url, s.ai_input_api_key, s.ai_input_model_id)
            except httpx.TimeoutException:
                result = dict(success=False, msg=NETWORK_TIMEOUT_MESSAGE)
            except Exception:
                # 不向页面输出原始异常，以免包含密钥等配置。
                result = dict(success=False, msg='连接测试异常，请稍后重试')
            finally:
                s.is_testing = False
            s['_ai_connection_result'] = result
            st.rerun()
    if not s.ai_input_enabled:
        clear_ai_results()
    return {**settings, 'enabled': s.ai_input_enabled}


def action_style(action):
    if '本月已买入' in action or '本月买入计划已完成' in action:
        return 'info'
    if '等到月底' in action or '已至月底' in action or '当前已是月底' in action:
        return 'warning'
    if '可考虑提前买入' in action or '可考虑提前执行本月买入计划' in action:
        return 'success'
    return 'info'


def summary_html(summary):
    header = ('<section class="ai-summary"><h2>🤖 AI 大白话总结</h2>'
              f'<p>{escape(summary["source"])} · 分析日期 {escape(str(summary["date"]))}'
              f' · 数据截止 {escape(str(summary["as_of"] or "无法判定"))}</p>')
    if summary.get('error'):
        return header + f'<p role="alert">{escape(summary["error"])}</p></section>'
    action = summary['sections'][ACTION_TITLE]
    color, ink = {'success': ('#e9f5ed', '#18582f'), 'warning': ('#fff4d6', '#755500'),
                  'info': ('#e8f1ff', '#204f87')}[action_style(action)]
    paragraphs = (f'<p style="white-space:pre-wrap;background:{color};color:{ink};padding:16px;border-radius:8px"><strong>{ACTION_TITLE}</strong><br>{escape(mark_sensitive_sentences(action))}</p>'
                  + ''.join(f'<p style="white-space:pre-wrap"><strong>{escape(title)}</strong><br>{escape(mark_sensitive_sentences(summary["sections"][title]))}</p>'
                            for title in TITLES if title != ACTION_TITLE))
    warning = f'<p role="alert">{escape(summary["warning"])}</p>' if summary['warning'] else ''
    return header + warning + paragraphs + f'<p>{escape(DISCLAIMER)}</p></section>'


def render_summary(report, settings):
    # Check before building context, reading caches, submitting or waiting on work.
    if not is_ai_enabled(settings):
        clear_ai_results()
        return None
    settings = {**settings, 'enabled': True}
    amount = st.session_state.get('trial_amount', 0.0)
    context = build_context(report, amount,
        has_invested_this_month=st.session_state.get('has_invested_this_month', False))
    fingerprint = hashlib.sha256(json.dumps([context, settings], sort_keys=True,
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    cache = st.session_state.get('ai_summary_cache')
    if cache is None or cache['fingerprint'] != fingerprint:
        if settings['enabled'] and settings['api_key']:
            task = st.session_state.get('ai_summary_task')
            if task is None or task['fingerprint'] != fingerprint:
                future = Future()
                def generate(summary_future=future, summary_context=context, summary_settings=dict(settings)):
                    # Workers return data only; all Streamlit calls stay here.
                    try:
                        summary_future.set_result(generate_summary(summary_context, summary_settings))
                    except Exception as exc:
                        summary_future.set_exception(exc)
                threading.Thread(target=generate, daemon=True, name='ai-summary').start()
                task = dict(fingerprint=fingerprint, future=future)
                st.session_state.ai_summary_task = task
            try:
                # Fast responses avoid a second render, slow APIs never hold
                # the page or its widgets for their 30-second network budget.
                summary = task['future'].result(timeout=.1)
            except FutureTimeout:
                with st.container(border=True, key='ai_summary_card'):
                    st.subheader('🤖 AI 大白话总结')
                    st.status('AI 参谋思考中...（最长30秒）', state='running')
                    st.caption('量化分析已完成，可继续查看下方结果。')
                _poll_summary(fingerprint)
                return None
        else:
            summary = generate_summary(context, settings)
        cache = dict(fingerprint=fingerprint, summary=summary)
        st.session_state.ai_summary_cache = cache
    st.session_state.pop('ai_summary_task', None)
    return _render_completed_summary(cache['summary'], context)


@st.fragment(run_every=1)
def _poll_summary(fingerprint):
    if not st.session_state.get('ai_input_enabled', True):
        clear_ai_results()
        return
    task = st.session_state.get('ai_summary_task')
    if task is None or task['fingerprint'] != fingerprint:
        return
    if task['future'].done():
        st.session_state.ai_summary_cache = dict(
            fingerprint=fingerprint, summary=task['future'].result())
        st.rerun()


def _render_completed_summary(summary, context):
    with st.container(border=True, key='ai_summary_card'):
        st.subheader('🤖 AI 大白话总结')
        st.caption(f"{summary['source']} · 分析日期 {summary['date']} · 数据截止 {summary['as_of'] or '无法判定'}")
        if summary.get('error'):
            st.error(summary['error'])
            if st.button('重试 AI 总结', key='ai_summary_retry'):
                st.session_state.pop('ai_summary_cache', None)
                st.session_state.pop('ai_summary_task', None)
                st.rerun()
            return summary
        if summary['warning']:
            st.warning(summary['warning'])
        action = summary['sections'][ACTION_TITLE]
        style = action_style(action)
        color, ink = {'success': ('#e9f5ed', '#18582f'), 'warning': ('#fff4d6', '#755500'),
                      'info': ('#e8f1ff', '#204f87')}[style]
        # Use escaped HTML instead of Markdown so every model character is visible.
        if summary['source'] == '本地规则':
            show_action = {'success': st.success, 'warning': st.warning, 'info': st.info}[style]
            show_action(f'**{ACTION_TITLE}**\n\n{action}')
        else:
            st.html(f'<p style="white-space:pre-wrap;background:{color};color:{ink};padding:16px;border-radius:8px"><strong>{ACTION_TITLE}</strong><br>{escape(mark_sensitive_sentences(action))}</p>')
        # Treat model output strictly as text, even when an API returns markup.
        for title in TITLES:
            if title == ACTION_TITLE:
                continue
            st.html(f'<p style="white-space:pre-wrap"><strong>{escape(title)}</strong><br>{escape(mark_sensitive_sentences(summary["sections"][title]))}</p>')
        st.caption(DISCLAIMER)
        with st.expander('查看总结依据与日期口径'):
            st.caption('仓位上限指新增资金占投入后总额的比例；并非基金总仓位或实际交易额度。')
            st.caption(context['calendar']['estimate_note'])
            st.write('本月预估月底交易日：' + '、'.join(context['calendar']['month_end_estimates']))
            st.json(context, expanded=False)
    return summary
