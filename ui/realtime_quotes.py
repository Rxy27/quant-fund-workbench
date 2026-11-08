"""Only this UI adapter owns quote caches; background workers never use Streamlit."""
from concurrent.futures import Future
import logging
import math
import re
import threading
import time
from queue import Empty, SimpleQueue

import streamlit as st

from core.market_data import ETF_PREFIXES, get_realtime_quotes, quote_progress

_logger = logging.getLogger(__name__)
OFF_EXCHANGE = '场外基金无盘中实时行情，参考最新净值'
DELAYED = '⚠️ 更新延迟，为上次数据'


def _start_request(codes):
    future = Future()
    future.quote_updates = SimpleQueue()

    def run():
        try:
            with quote_progress(future.quote_updates.put):
                # This worker is already asynchronous. Keep its queue attached
                # until slow fallbacks finish, so late quotes are not discarded.
                future.set_result(get_realtime_quotes(codes))
        except Exception:
            _logger.exception('后台行情请求失败')
            future.set_result({})

    threading.Thread(target=run, daemon=True, name='realtime-quotes').start()
    return future


def _merge_quotes(quotes):
    """Reject invalid ticks and jumps >20%, preserving each previous good quote."""
    cache = st.session_state.setdefault('last_quotes', {})
    accepted = set()
    for code, quote in quotes.items():
        try:
            price = float(quote['price'])
            change = float(quote['change_pct'])
            if not math.isfinite(price) or price <= 0 or not math.isfinite(change):
                raise ValueError('价格或涨跌幅无效')
            old = cache.get(code)
            if old and abs(price - old['price']) > old['price'] * 0.2:
                _logger.warning('拒绝覆盖异常行情 code=%s old=%s new=%s，价格跳变超过20%%',
                                code, old['price'], price)
                continue
            cache[code] = {**quote, 'price': price, 'change_pct': change}
            accepted.add(code)
        except Exception:
            _logger.warning('拒绝覆盖无效行情 code=%s', code, exc_info=True)
    return accepted


def _consume_updates(task, codes):
    """The provider writes a queue; only this script thread changes session state."""
    accepted = set()
    updates = getattr(task['future'], 'quote_updates', None)
    if updates is None:
        return accepted
    while True:
        try:
            event = updates.get_nowait()
        except Empty:
            break
        if event['stage'] == 'cooldown':
            task['cooldown'] = True
        elif event['stage'] == 'error':
            for code in event['codes']:
                task.setdefault('failures', {})[code] = event['reason']
        elif event['stage'] != 'quotes':
            for code in event['codes']:
                task.setdefault('stages', {})[code] = event['stage']
        accepted.update(_merge_quotes({code: quote for code, quote in event['quotes'].items()
                                       if code in codes}))
    task.setdefault('accepted', set()).update(accepted)
    return accepted


@st.fragment(run_every="1s")
def render_realtime_quotes(instruments):
    """Repaint quotes only. Provider latency must never block the script thread."""
    state = st.session_state
    cache = state.setdefault('last_quotes', {})
    errors = state.setdefault('realtime_quote_errors', {})
    codes = [row['code'] for row in instruments if row['kind'] != 'fund']
    now = time.monotonic()
    received = state.setdefault('quote_received_at', {})
    accepted = set()
    task = state.get('realtime_quote_task')
    if task:
        accepted.update(_consume_updates(task, codes))
    if task and task['future'].done():
        try:
            # Discard late results for removed items and for off-exchange holdings.
            quotes = task['future'].result()
            accepted.update(_merge_quotes({code: quote for code, quote in quotes.items() if code in codes}))
            if not task.get('cooldown'):
                for code in task.get('codes', codes):
                    if code not in accepted | task.get('accepted', set()) and code in codes:
                        reason = task.get('failures', {}).get(code, '本次未取得行情')
                        errors[code] = f'{reason}，将按冷却规则自动重试。'
        except Exception:
            _logger.exception('读取后台行情失败，保留上次数据')
            for code in task.get('codes', codes):
                errors[code] = '本次未取得行情，将按冷却规则自动重试。'
        state.pop('realtime_quote_task', None)
        task = None
    new_codes = set(codes) - set(state.get('quote_requested_codes', []))
    if codes and task is None and (new_codes or now >= state.get('quote_next_request_at', 0)):
        task = dict(future=_start_request(codes), codes=codes, started_at=time.monotonic())
        state['realtime_quote_task'] = task
        state['quote_requested_codes'] = list(codes)
        state['quote_next_request_at'] = now + 60
        accepted.update(_consume_updates(task, codes))
    for code in accepted:
        errors.pop(code, None)
        received[code] = now

    if not instruments:
        st.caption('暂无关注标的，可在上方添加股票或场内 ETF。')
    for row in instruments:
        code = row['code']
        with st.container():
            name_column, price_column = st.columns([1, 2])
            quote = cache.get(code) if row['kind'] != 'fund' else None
            # Provider strings are plain text, never executable Markdown or HTML.
            name_column.text(f"{code} · {quote['name']}" if quote else code)
            if row['kind'] == 'fund':
                price_column.write(OFF_EXCHANGE)
            elif quote:
                price_column.text(f"¥ {quote['price']:,.3f}  /  {quote['change_pct']:+.2f}%")
                if code in errors or now - received.get(code, 0) > 90:
                    price_column.caption(DELAYED)
                if quote.get('market_time'):
                    price_column.caption(f"行情时间 {quote['market_time']}")
                price_column.caption(f"获取于 {quote['update_time']}（每60秒请求，非交易所时间）")
            else:
                price_column.text('--')
                if code in errors:
                    price_column.caption(errors[code])
                elif task and time.monotonic() - task.get('started_at', time.monotonic()) >= 60:
                    price_column.caption('行情源响应缓慢，仍在等待；其他功能可正常使用。')
                elif task and task.get('stages', {}).get(code) == 'fallback':
                    price_column.caption('首选行情源暂不可用，正在尝试备用行情源。')
                elif task and task.get('cooldown'):
                    price_column.caption('请求冷却中，稍后自动重试。')
                else:
                    price_column.caption('行情获取中，暂未取得数据' if task else '暂未取得数据')


def render_market_watch(holdings):
    """Watchlist controls are outside the timed fragment and never trigger on a tick."""
    st.subheader('关注标的实时行情')
    watchlist = st.session_state.setdefault('quote_watchlist', [])
    holdings_rows = [dict(code=holding['code'], kind=(
        'stock' if holding.get('asset_type') == 'stock' else
        'etf' if holding['code'].startswith(ETF_PREFIXES) else 'fund'))
        for holding in holdings if holding['code'] != 'CASH']
    with st.form('quote_watchlist_form', clear_on_submit=True):
        code_column, type_column, add_column = st.columns([2, 2, 1], vertical_alignment='bottom')
        code = code_column.text_input('关注代码', placeholder='例如 688981 或 510300', max_chars=6)
        label = type_column.selectbox('标的类型', ['股票', '场内 ETF', '场外基金'])
        add = add_column.form_submit_button('添加关注')
    if add:
        kind = {'股票': 'stock', '场内 ETF': 'etf', '场外基金': 'fund'}[label]
        if not re.fullmatch(r'[0-9]{6}', code.strip()):
            st.warning('请输入6位标的代码。')
        elif kind == 'etf' and not code.startswith(ETF_PREFIXES):
            st.warning('该代码不属于支持的场内 ETF 代码段；场外联接基金请选择“场外基金”。')
        elif kind == 'stock' and not code.startswith(('60', '68', '00', '30', '8', '4', '92')):
            st.warning('该代码不属于支持的 A 股代码段；基金请按其实际类型添加。')
        elif any(row['code'] == code for row in holdings_rows + watchlist):
            st.caption('该代码已在持仓或关注列表中。')
        else:
            watchlist.append(dict(code=code, kind=kind))
    if watchlist and st.button('清空额外关注', key='clear_quote_watchlist'):
        watchlist.clear()
    st.caption('盘中价格与基金净值分别展示；行情更新不参与沙盘或历史图表计算。')
    render_realtime_quotes(holdings_rows + watchlist)
