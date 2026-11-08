"""Reproduce the frozen 5% baseline and the pruned engine, without retuning.

Run: python -m core.verify_survival
Artifacts go to outputs/strategy_pruning; previous research is never overwritten.
"""
import hashlib
import json
from pathlib import Path

import pandas as pd

from ._frozen_portfolio import run as original_run, PortfolioCosts
from ._strategy import signal_schedule
from ._metrics import calculate

from .backtest import run
from .allocation import UNIVERSE, is_rebalance_day

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT/'outputs/strategy_pruning'


def load_prices():
    folder = ROOT/'data/etf_v3'
    manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    frames = {}
    for code in UNIVERSE:
        path = folder/f'{code}.parquet'
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest['assets'][code]['sha256']:
            raise ValueError(f'{code}行情哈希不符')
        frames[code] = pd.read_parquet(path)
    master = frames[UNIVERSE[0]].index
    if any(not f.index.equals(master) for f in frames.values()):
        raise ValueError('资产交易日历不一致；不得交集裁切或前向填充')
    opens = pd.DataFrame({s: f.open for s, f in frames.items()})
    closes = pd.DataFrame({s: f.close for s, f in frames.items()})
    return opens, closes, manifest


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    protocol = json.loads((ROOT/'validation/v3_protocol.json').read_text(encoding='utf-8'))
    parameters = {**protocol['parameters'], 'target_volatility': .05}
    opens, closes, manifest = load_prices()
    start = protocol['train_days']
    costs = PortfolioCosts(slippage_round_trip=protocol['slippage_round_trip'],
        commission_per_side=protocol['commission_per_side'],
        minimum_commission=protocol['minimum_commission_per_order'])
    signals, diagnostics = signal_schedule(closes, start, parameters)
    original = original_run(opens, closes, signals, start, costs,
        protocol['initial_cash'], protocol['lot_size'])
    before = calculate(original)
    published = json.loads((ROOT/'reports/v5/V5-T02/walk_forward/metrics.json').read_text(encoding='utf-8'))
    for key in ('sharpe', 'max_drawdown', 'annual_turnover_two_way'):
        if abs(before[key]-published[key]) > 1e-10:
            raise AssertionError(f'Frozen 5% baseline mismatch: {key}')
    original.curve.to_csv(OUT/'before_curve.csv')
    original.orders.to_csv(OUT/'before_orders.csv', index=False)
    (OUT/'before_metrics.json').write_text(json.dumps(before, indent=2), encoding='utf-8')
    print('Frozen 5% baseline reproduced:', {k: before[k] for k in ('sharpe','max_drawdown','annual_turnover_two_way')}, flush=True)
    upgraded = run(opens, closes, {}, start, costs, protocol['initial_cash'],
        protocol['lot_size'], strategy_config={**parameters, 'portfolio_target_volatility': .05})
    after = calculate(upgraded)
    # Audit the entire execution ledger, not only aggregate turnover.
    for order in upgraded.orders.to_dict('records'):
        signal = pd.Timestamp(order['signal_date'])
        i = closes.index.get_loc(signal)
        assert is_rebalance_day(closes.index[:i+1]), order
        assert pd.Timestamp(order['date']) == closes.index[i+1], order
        assert order['quantity'] % protocol['lot_size'] == 0, order
    previous = json.loads((ROOT/'outputs/survival_upgrade/comparison.json').read_text(encoding='utf-8'))
    assert previous['start'] == str(closes.index[start].date())
    assert previous['end'] == str(closes.index[-1].date())
    assert previous['data_manifest'] == manifest
    upgraded.curve.to_csv(OUT/'after_curve.csv')
    upgraded.orders.to_csv(OUT/'after_orders.csv', index=False)
    (OUT/'after_signals.json').write_text(json.dumps(upgraded.gate_log, ensure_ascii=False, indent=2), encoding='utf-8')
    result = {'start': str(closes.index[start].date()), 'end': str(closes.index[-1].date()),
        'target_volatility': .05, 'before': before, 'previous_upgrade': previous['after'], 'after': after,
        'acceptance': {'turnover_below_300pct': after['annual_turnover_two_way'] < 3.,
                       'sharpe_above_one': after['sharpe'] > 1.,
                       'drawdown_target': 'approximately 6%; report the measured value without retuning'},
        'monthly_order_audit': {'passed': True, 'filled_orders': len(upgraded.orders),
            'signal_days': sum(r['rebalance'] for r in upgraded.gate_log),
            'first_signal_date': upgraded.orders.iloc[0]['signal_date'],
            'first_fill_date': upgraded.orders.iloc[0]['date']},
        'baseline_verified_against': 'reports/v5/V5-T02/walk_forward/metrics.json',
        'parameters': parameters, 'data_manifest': manifest,
        'execution': 'Close signal, next observed open; original 100-share lot rounding, 100000 CNY, 0.025% commission per side/minimum 5 CNY, 0.4% round-trip slippage; cash zero interest; no terminal liquidation.',
        'schedule': 'Only the first observed trading day close of each month. No midmonth initial-entry exception; the frozen baseline has an initial last-training-close entry.',
        'risk_gate': 'Same ungated strategy comparison as previous report. Independent RiskGate remains caller-injected and is tested separately; no internal account drawdown control.',
        'limitations': 'Previously exposed development data; unadjusted price returns exclude cash dividends. No final OOS claim. Target floors do not guarantee an executable 100-share minimum for every asset.'}
    (OUT/'comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Pruned 5%:', {k: after[k] for k in ('sharpe','max_drawdown','annual_turnover_two_way')}, flush=True)


if __name__ == '__main__':
    main()
