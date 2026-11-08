"""Nonblocking sandbox coach card; separate cache from the live fund summary."""
from concurrent.futures import Future, TimeoutError as FutureTimeout
from html import escape
import threading

import streamlit as st

from core.sandbox_advisor import (TITLES, build_sandbox_context,
                                  generate_sandbox_review, review_fingerprint)
from ui.ai_advisor import clear_ai_results, is_ai_enabled


def review_html(sections):
    """Highlight the three requested sections, treating model prose as text."""
    return ''.join(f'<div class="sandbox-coach-section"><p><strong>【{escape(title)}】</strong></p>'
                   f'<p style="white-space:pre-wrap">{escape(sections[title])}</p></div>' for title in TITLES)


@st.fragment(run_every=1)
def _poll_review(fingerprint):
    if not st.session_state.get('ai_input_enabled', True):
        clear_ai_results()
        return
    task = st.session_state.get('sandbox_ai_task')
    if task and task['fingerprint'] == fingerprint and task['future'].done():
        st.session_state.sandbox_ai_cache = dict(fingerprint=fingerprint, review=task['future'].result())
        st.rerun()


def render_sandbox_review(payload, settings):
    if not is_ai_enabled(settings):
        clear_ai_results()
        return None
    settings = {**settings, 'enabled': True}
    context = payload.get('ai_context') or build_sandbox_context(payload['result'], payload['code'])
    fingerprint = review_fingerprint(context, settings)
    cache = st.session_state.get('sandbox_ai_cache')
    with st.container(border=True, key='sandbox_ai_card'):
        st.subheader('🤖 AI 投资教练复盘')
        if cache is None or cache['fingerprint'] != fingerprint:
            task = st.session_state.get('sandbox_ai_task')
            if task is None or task['fingerprint'] != fingerprint:
                future = Future()
                # Capture immutable inputs; worker threads never touch Streamlit.
                def generate(review_future=future, review_context=context, review_settings=dict(settings)):
                    review_future.set_result(generate_sandbox_review(review_context, review_settings))
                threading.Thread(target=generate, daemon=True, name='sandbox-coach').start()
                task = dict(fingerprint=fingerprint, future=future)
                st.session_state.sandbox_ai_task = task
            try:
                review = task['future'].result(timeout=.1)
            except FutureTimeout:
                st.status('AI 教练正在复盘…（最长30秒）', state='running')
                st.caption('回测已完成，可以继续查看曲线和下方对比表格。')
                _poll_review(fingerprint)
                return None
            cache = dict(fingerprint=fingerprint, review=review)
            st.session_state.sandbox_ai_cache = cache
        st.session_state.pop('sandbox_ai_task', None)
        review = cache['review']
        if review['error']:
            st.error(review['error'])
            if st.button('重试 AI 沙盘复盘', key='sandbox_ai_retry'):
                st.session_state.pop('sandbox_ai_cache', None)
                st.session_state.pop('sandbox_ai_task', None)
                st.rerun()
        else:
            st.caption(f"{review['source']} · {review['model']} · 生成于 {review['date']} · 仅解读本次历史模拟")
            st.html(review_html(review['sections']))
        with st.expander('查看本次 AI 复盘依据'):
            st.json(context, expanded=False)
        return review
