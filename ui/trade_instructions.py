"""Shared download affordance for human-reviewed instruction sheets."""
import streamlit as st

from core.trade_instructions import (EXPORT_FORMATS, build_instruction_export,
                                      current_date, instruction_filename)


def render_instruction_download(snapshot, *, sandbox=False):
    today = current_date()
    options = dict(today=today, sandbox=sandbox)
    if not sandbox:
        options['has_invested_this_month'] = st.session_state.get('has_invested_this_month', False)
    scope = 'sandbox' if sandbox else 'decision'
    st.divider()
    st.subheader('本月调仓指令单')
    st.caption('导出后核对基金、金额与数据日期，再前往天天基金等平台手动执行。'
               + ('沙盘导出保留历史模拟标识。' if sandbox else '观望时保留依据，不生成买卖指令。'))
    st.html('''<style>
    @media(max-width:640px) {
      .st-key-decision_instruction_export [data-testid="stHorizontalBlock"],
      .st-key-sandbox_instruction_export [data-testid="stHorizontalBlock"] {flex-direction:column;}
      .st-key-decision_instruction_export [data-testid="stColumn"],
      .st-key-sandbox_instruction_export [data-testid="stColumn"] {width:100%;flex:1 1 100%;}
    }
    </style>''')
    with st.container(key=f'{scope}_instruction_export'):
        columns = st.columns([1, 2], vertical_alignment='bottom')
        with columns[0]:
            export_format = st.selectbox('导出格式', options=list(EXPORT_FORMATS),
                format_func=lambda value: EXPORT_FORMATS[value][0], key=f'{scope}_instruction_format')
        with columns[1]:
            st.download_button('📥 导出本月调仓指令单',
                data=build_instruction_export(snapshot, export_format=export_format, **options),
                file_name=instruction_filename(snapshot['code'], today, export_format),
                mime=EXPORT_FORMATS[export_format][1], key=f'{scope}_trade_instructions_{export_format}',
                on_click='ignore', width='stretch')
