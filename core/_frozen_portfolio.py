"""Cash/share ledger: yesterday's stored target, today's open fills, then close NAV."""
from dataclasses import dataclass
import json

import numpy as np
import pandas as pd

from ._execution import plan_targets
from ._risk_gate import Account, EntryLot, Position


@dataclass(frozen=True)
class PortfolioCosts:
    slippage_round_trip: float = .004
    commission_per_side: float = .00025
    minimum_commission: float = 5.0

    def __post_init__(self):
        if (not all(np.isfinite(x) for x in (self.slippage_round_trip, self.commission_per_side, self.minimum_commission))
            or not 0 <= self.slippage_round_trip < 1 or not 0 <= self.commission_per_side < 1
            or self.minimum_commission < 0):
            raise ValueError('Invalid cost assumptions')

    def price(self, raw, side):
        return raw*(1+side*self.slippage_round_trip/2)

    def fee(self, notional):
        return max(self.minimum_commission, notional*self.commission_per_side) if notional else 0.0


@dataclass
class PortfolioResult:
    curve: pd.DataFrame
    orders: pd.DataFrame
    cycles: list
    gate_log: list
    open_cycles: list


def run(opens, closes, signals, start, costs=None, initial_cash=100000.0, lot_size=100, gate=None):
    costs = costs or PortfolioCosts()
    if not opens.index.equals(closes.index) or not opens.columns.equals(closes.columns):
        raise ValueError('Open/close alignment mismatch')
    if (not opens.index.is_monotonic_increasing or opens.index.has_duplicates
        or not 1 <= start < len(opens) or lot_size < 1
        or not np.isfinite(initial_cash) or initial_cash <= 0):
        raise ValueError('Invalid calendar, warmup, or capital')
    if not np.isfinite(opens.to_numpy()).all() or not np.isfinite(closes.to_numpy()).all() or (opens <= 0).any().any() or (closes <= 0).any().any():
        raise ValueError('Invalid executable prices')
    if not set(signals).issubset(set(opens.index[:-1])):
        raise ValueError('Signals outside executable calendar')
    symbols = list(opens.columns)
    cash = float(initial_cash)
    shares = {s:0 for s in symbols}
    lots = {s:[] for s in symbols}
    active = {}
    rows, orders, cycles, comparisons = [], [], [], []
    if gate:
        gate.observe(Account(str(opens.index[start-1].date()), cash))
    for i in range(start, len(opens)):
        day, signal_day = opens.index[i], opens.index[i-1]
        prices = opens.iloc[i].to_dict()
        equity_open = cash + sum(shares[s]*prices[s] for s in symbols)
        intended = plan_targets(signals[signal_day], prices, shares, equity_open, lot_size) if signal_day in signals else []
        positions = tuple(Position(s, shares[s], prices[s], sum(q*c for q,c in lots[s])/shares[s],
                                  tuple(EntryLot(int(q),c) for q,c in lots[s])) for s in symbols if shares[s])
        if gate:
            decision = gate.evaluate(intended, Account(str(day.date()), equity_open, positions))
            actual = sorted(decision.actual, key=lambda order:(order.quantity > 0, order.symbol))
            comparisons.append({'date':str(day.date()), 'signal_date':str(signal_day.date()),
                                'scheduled_rebalance':signal_day in signals, **decision.to_dict()})
        else:
            actual = intended
        turnover, fees, slippage = 0., 0., 0.
        for order in actual:
            s, signed = order.symbol, order.quantity
            side = 1 if signed > 0 else -1
            qty = abs(signed)
            price = costs.price(prices[s], side)
            if side > 0:
                # Opening gap can change affordable quantities, but never alters the stored signal.
                qty = min(qty, int(cash/price//lot_size)*lot_size)
                while qty > 0 and qty*price+costs.fee(qty*price) > cash+1e-8:
                    qty -= lot_size
            else:
                qty = min(qty, shares[s])
            if not qty:
                continue
            fee = costs.fee(qty*price)
            if side > 0:
                outlay = qty*price+fee
                if shares[s] == 0:
                    active[s] = {'symbol':s, 'entry_date':str(day.date()), 'entry_i':i,
                                 'invested':0., 'proceeds':0.}
                active[s]['invested'] += outlay
                cash -= outlay
                lots[s].append((qty, outlay/qty))
                shares[s] += qty
            else:
                proceeds = qty*price-fee
                cash += proceeds
                active[s]['proceeds'] += proceeds
                shares[s] -= qty
                remaining = qty
                while remaining:
                    quantity, unit = lots[s][0]
                    used = min(quantity, remaining)
                    remaining -= used
                    if used == quantity:
                        lots[s].pop(0)
                    else:
                        lots[s][0] = (quantity-used, unit)
                if shares[s] == 0:
                    cycle = active.pop(s)
                    cycle.update({'exit_date':str(day.date()), 'holding_trading_days':i-cycle.pop('entry_i'),
                                  'net_pnl':cycle['proceeds']-cycle['invested'],
                                  'net_return':cycle['proceeds']/cycle['invested']-1})
                    cycles.append(cycle)
            notional = qty*prices[s]
            slip = qty*abs(price-prices[s])
            turnover += notional/equity_open
            fees += fee
            slippage += slip
            orders.append({'date':str(day.date()), 'signal_date':str(signal_day.date()), 'symbol':s,
                'side':'BUY' if side > 0 else 'SELL', 'quantity':qty, 'raw_open':prices[s],
                'fill_price':price, 'commission':fee, 'slippage_cost':slip,
                'raw_notional':notional, 'equity_before_trades':equity_open, 'reason':order.reason})
        equity = cash + sum(shares[s]*float(closes.iloc[i][s]) for s in symbols)
        if cash < -1e-7 or not np.isfinite(equity) or equity <= 0:
            raise ArithmeticError('Account invariant failed')
        row = {'date':day, 'equity':equity, 'cash':cash, 'turnover_two_way':turnover,
               'commission':fees, 'slippage_cost':slippage}
        row.update({f'shares_{s}':shares[s] for s in symbols})
        row.update({f'weight_{s}':shares[s]*float(closes.iloc[i][s])/equity for s in symbols})
        rows.append(row)
        if gate:
            gate.observe(Account(str(day.date()), equity))
    curve = pd.DataFrame(rows).set_index('date')
    curve['nav'] = curve.equity/initial_cash
    curve['daily_return'] = curve.equity/curve.equity.shift(1, fill_value=initial_cash)-1
    return PortfolioResult(curve, pd.DataFrame(orders), cycles, comparisons,
                           [dict(value, current_quantity=shares[s]) for s,value in active.items()])
