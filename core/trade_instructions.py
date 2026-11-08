"""Local, deterministic instructions for manual review; never submit orders."""
from copy import deepcopy
import csv
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
import io
import json
import math
import re
from zoneinfo import ZoneInfo

import pandas as pd

from .position_sizing import derive_position_range


def current_date():
    return datetime.now(ZoneInfo('Asia/Shanghai')).date()


EXPORT_FORMATS = {
    'md': ('Markdown (.md)', 'text/markdown'),
    'txt': ('纯文本 (.txt)', 'text/plain'),
    'csv': ('表格文件 (.csv)', 'text/csv'),
    'json': ('JSON (.json)', 'application/json'),
}


def instruction_filename(code, today, export_format='md'):
    if export_format not in EXPORT_FORMATS:
        raise ValueError('不支持的导出格式')
    return f'交易指令单_{code}_{today:%Y%m%d}.{export_format}'


def _money(value):
    return Decimal(str(max(0., value))).quantize(Decimal('.01'), rounding=ROUND_DOWN)


def _instruction_data(code, name, today, as_of, conclusion, trade_note, reasons, checks,
                      note='', trade=None, simulation=None):
    return dict(generated_date=today.isoformat(), timezone='Asia/Shanghai',
        fund_code=code, fund_name=name, data_as_of=as_of,
        action='buy' if conclusion == '建议买入' else 'sell' if conclusion == '建议减仓' else 'hold',
        amount=trade['amount'] if trade else None, currency='CNY', conclusion=conclusion,
        trade=trade, trade_note=trade_note, reasons=reasons, conditions=checks, note=note,
        simulation=simulation,
        execution_note='核对后在天天基金等平台手动操作；下载不代表已成交，也不会扣减账户资金。',
        disclaimer='本指令仅供参考，不构成投资建议。')


def _trade_fields(trade, *, simulated=False):
    fields = []
    if simulated:
        fields += [('模拟交易日期', trade['date']), ('信号截止日期', trade['signal_cutoff'])]
    fields += [('交易方向', '买入' if trade['direction'] == 'buy' else '卖出'),
               ('模拟金额' if simulated else '建议金额',
                f"{trade['amount']:.2f} 元" + trade.get('amount_note', '')),
               ('资金扣减说明', trade['funding_note'])]
    if trade.get('calculation_basis'):
        fields.append(('金额依据', trade['calculation_basis']))
    return fields


def _markdown_document(data):
    code, name, today, as_of = (data[key] for key in ('fund_code', 'fund_name', 'generated_date', 'data_as_of'))
    conclusion, reasons, note = (data[key] for key in ('conclusion', 'reasons', 'note'))
    checks = '；'.join(f'{key} {value}' for key, value in data['conditions'].items())
    trade = data['trade_note']
    if data['trade']:
        trade = '\n'.join(f'- {key}：{value}' for key, value in _trade_fields(data['trade']))
    if data['simulation']:
        trade += '\n\n### 历史模拟交易（仅供复盘）\n\n' + '\n'.join(
            f'- {key}：{value}' for key, value in _trade_fields(data['simulation'], simulated=True))
    lines = ['# 本月调仓指令单', '', f'- 生成日期：{today}（北京时间）',
             f'- 基金代码：{code}', f'- 基金名称：{name}', f'- 分析数据截止：{as_of}', '',
             f'## 操作结论\n\n{conclusion}', '', '## 交易指令', '', trade, '',
             '## 决策依据（摘要）', '', *[f'- {reason}' for reason in reasons], '',
             f'条件核对：{checks}', '']
    if note:
        lines += [note, '']
    lines += ['执行方式：' + data['execution_note'], '', '免责声明：' + data['disclaimer'], '']
    return '\n'.join(lines)


def serialize_instructions(data, export_format='md'):
    """Render one decision object; CSV alone is returned as UTF-8 BOM bytes."""
    if export_format == 'json':
        return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if export_format == 'md':
        return _markdown_document(data)
    if export_format == 'txt':
        text = _markdown_document(data)
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'^- ', '  ', text, flags=re.MULTILINE)
        return text.translate(str.maketrans('', '', '#*|'))
    if export_format != 'csv':
        raise ValueError('不支持的导出格式')
    rows = [('字段名', '具体数值'), ('生成日期', data['generated_date']), ('时区', data['timezone']),
            ('基金代码', data['fund_code']), ('基金名称', data['fund_name']),
            ('分析数据截止', data['data_as_of']), ('操作结论', data['conclusion']),
            ('操作方向', {'buy': '买入', 'sell': '卖出', 'hold': '观望'}[data['action']]),
            ('币种', data['currency'])]
    rows += _trade_fields(data['trade']) if data['trade'] else [('交易指令', data['trade_note']), ('建议金额', '未生成定额指令')]
    if data['simulation']:
        rows += [('记录类型', '历史模拟交易（仅供复盘）')]
        rows += [('历史模拟/' + key, value) for key, value in _trade_fields(data['simulation'], simulated=True)]
    rows += [(f'决策依据{i}', reason) for i, reason in enumerate(data['reasons'], 1)]
    rows += list(data['conditions'].items())
    rows += [('说明', data['note']), ('执行方式', data['execution_note']), ('免责声明', data['disclaimer'])]
    output = io.StringIO(newline='')
    csv.writer(output).writerows(rows)
    return output.getvalue().encode('utf-8-sig')


def _cash_purchase_check(report, amount):
    """Verify a cash-to-fund transfer, without treating it as extra capital."""
    portfolio = deepcopy(report['decision_portfolio'])
    weight = float(amount) / report['user_portfolio']['account_total']
    cash = next(h for h in portfolio['holdings'] if h['code'] == 'CASH')
    cash['weight'] = max(0., cash['weight'] - weight)
    held = next((h for h in portfolio['holdings'] if h['code'] == report['code']), None)
    if held is None:
        held = {'code': report['code'], 'weight': 0.}
        portfolio['holdings'].append(held)
    held['weight'] += weight
    sizing = derive_position_range(report['decision_fund'], portfolio, report['config'])
    return next((row for row in sizing['scan_table'] if row['weight'] == 0), None)


def build_trade_instruction_data(report, *, today=None, has_invested_this_month=False):
    today = today or current_date()
    buy = {row['id']: row for row in report['conditions']['buy']}
    sell = [row for row in report['conditions']['sell'] if row['status'] == '触发']
    account = report['user_portfolio']
    total = account['account_total']
    held = sum(h['amount'] for h in account['holdings'] if h['code'] == report['code'])
    cash = max(0., total - sum(h['amount'] for h in account['holdings']))
    as_of = report['metrics'].get('end')
    current = bool(as_of) and pd.Timestamp(as_of).strftime('%Y-%m') == today.strftime('%Y-%m')
    checks = {f'B{i}': buy.get(f'B{i}', {}).get('status', '无法判定') for i in range(1, 7)}
    relevant = [buy[key] for key in ('B2', 'B3', 'B4') if key in buy]
    conclusion, trade_note, trade = '建议观望', '无买卖指令；保留资金池余额。', None
    note = '金额口径：仅使用已保存账户的剩余现金作为本月闲置资金池；账户总额不变，不计入未登记的新增资金。'
    if not current:
        note += ' 当前结果不属于生成月份或缺少日期，请更新分析后再制定本月交易。'
    elif sell and held > 0:
        conclusion = '建议减仓'
        relevant = sell[:2] + [buy['B4']]
        rebalance = next((row for row in sell if row['id'] == 'S3'), None)
        target = rebalance['evidence'].get('target') if rebalance else None
        if target is not None and math.isfinite(target) and 0 <= target <= 1:
            amount = _money(min(held, max(0., held - target * total)))
            if amount > 0:
                trade = dict(direction='sell', amount=float(amount), amount_note='（估算市值）',
                    funding_note='从该基金现有持仓扣减，赎回到账后回补闲置资金池。',
                    calculation_basis=f'按 S3 目标权重 {target:.2%} 计算超配金额，并以现有持仓为上限。')
        trade_note = '' if trade else '无定额卖出指令：卖出条件已触发，但没有可量化的减仓目标，请人工确认金额。'
    elif not has_invested_this_month and all(
            buy.get(f'B{i}', {}).get('status') in (('满足', '不适用') if i in (2, 6) else ('满足',))
            for i in range(1, 7)) and not sell:
        target = buy['B5']['evidence'].get('target')
        cap = report['config']['single_fund_max_weight']
        if target is not None and math.isfinite(target) and 0 <= target <= 1:
            amount = _money(min(cash, max(0., min(target, cap) * total - held)))
            row = _cash_purchase_check(report, amount) if amount > 0 else None
            if row and row['within_budget']:
                conclusion = '建议买入'
                trade_note = ''
                trade = dict(direction='buy', amount=float(amount),
                    funding_note='从本月闲置资金池中扣除（已保存账户剩余现金）。',
                    calculation_basis='取现金余额、B5 目标缺口与单基金上限的最小值；'
                        f'按现金转持仓复核，历史回撤 {row["portfolio_max_drawdown"]:.2%}、'
                        f'下行风险 {row["portfolio_volatility"]:.2%}，均满足配置预算。')
            else:
                note += ' 资金不足或具体金额未通过历史回撤/波动预算复核，不生成买入指令。'
    if has_invested_this_month and conclusion == '建议观望':
        note += ' 本月买入计划已执行，不重复生成买入指令。'
    if conclusion == '建议观望' and current:
        blockers = [row for row in buy.values() if row['status'] not in ('满足', '不适用')]
        relevant = (blockers[:1] + relevant)[:3]
    reasons = [f"{row['id']} {row['label']}：{row['status']}；{row['reason']}" for row in relevant[:3]]
    return _instruction_data(report['code'], report['fund'].info.get('基金名称', '名称数据不可得'),
                     today, str(as_of) if as_of else '无法判定', conclusion, trade_note, reasons, checks, note, trade=trade)


def build_sandbox_instruction_data(payload, *, today=None):
    """Keep historical simulated orders distinct from this month's real orders."""
    result = payload['result']
    order = result.orders.iloc[-1]
    percentile, deviation = order['pe_percentile'], order['ma_deviation']
    pe = f'历史 PE 分位 {percentile:.1%}' if pd.notna(percentile) else '历史 PE 不可用，使用均线/基础定投规则'
    trend = f'200日均线偏离 {deviation:.2%}' if pd.notna(deviation) else '200日均线历史不足'
    amount = _money(order['smart_purchase'])
    pool = order['smart_purchase'] + order['smart_cash']
    reasons = [f'{pe}；{trend}。',
               f"模拟计划：基础金额 {result.monthly_amount:.2f} 元 × {order['multiplier']:g} 倍；"
               f'买入前资金池 {pool:.2f} 元，实际模拟买入以资金池为上限。',
               '沙盘未评估完整 B1–B6 及 B4 组合风险预算，不能验证本月真实调仓条件。']
    b2 = '无法判定' if pd.isna(percentile) else '满足' if percentile <= .3 else '不满足'
    checks = {f'B{i}': b2 if i == 2 else '未评估' for i in range(1, 7)}
    simulation = dict(date=order['date'].strftime('%Y-%m-%d'),
        signal_cutoff=order['signal_cutoff'].strftime('%Y-%m-%d'), direction='buy', amount=float(amount),
        funding_note='从该历史月份的模拟闲置资金池中扣除；不对应真实账户资金。')
    return _instruction_data(payload['code'], payload.get('name', '名称数据不可得'), today or current_date(),
                     f'{result.start_date:%Y-%m-%d} — {result.end_date:%Y-%m-%d}（历史沙盘）',
                     '建议观望（本月真实操作）；历史模拟记录仅供复盘。', '无本月真实买卖指令。', reasons, checks,
                     '以下为推演区间最后一期的模拟交易，不能使用当前或未来数据补齐历史条件；本模型未计申赎费用与确认延迟。',
                     simulation=simulation)


def build_trade_instructions(report, *, today=None, has_invested_this_month=False):
    return serialize_instructions(build_trade_instruction_data(report, today=today,
        has_invested_this_month=has_invested_this_month))


def build_sandbox_instructions(payload, *, today=None):
    return serialize_instructions(build_sandbox_instruction_data(payload, today=today))


def build_instruction_export(snapshot, *, export_format='md', sandbox=False, today=None,
                             has_invested_this_month=False):
    data = (build_sandbox_instruction_data(snapshot, today=today) if sandbox else
            build_trade_instruction_data(snapshot, today=today, has_invested_this_month=has_invested_this_month))
    return serialize_instructions(data, export_format)
